"""Detect a protruding tongue and hold W while Minecraft is focused."""

import ctypes
import os
import threading
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

import cv2
import numpy as np


_KEYEVENTF_KEYUP = 0x0002
_VK_W = 0x57
_MODEL_URL = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
_MODEL_PATH = Path(__file__).with_name(".face_landmarker.task")


class TongueError(Exception):
    pass


class _Keyboard:
    """Small Windows-only keyboard driver with foreground-window gating."""

    def __init__(self):
        if os.name != "nt":
            raise TongueError("--tongue currently needs Windows")
        self.user32 = ctypes.windll.user32
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE,
                                            wintypes.DWORD, ctypes.c_ulong]
        self.down = False

    def minecraft_focused(self):
        hwnd = self.user32.GetForegroundWindow()
        if not hwnd:
            return False
        length = self.user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(hwnd, title, len(title))
        class_name = ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, class_name, len(class_name))
        return "minecraft" in title.value.lower() or "minecraft" in class_name.value.lower()

    def set_down(self, down):
        if down == self.down:
            return
        self.user32.keybd_event(_VK_W, 0, 0 if down else _KEYEVENTF_KEYUP, 0)
        self.down = down

    def release(self):
        self.set_down(False)


class TongueDetector:
    """Camera worker using MediaPipe face landmarks and OpenCV frames."""

    def __init__(self, camera=0, threshold=0.08, preview=False, on_error=None):
        self.camera = camera
        self.threshold = threshold
        self.preview = preview
        self.on_error = on_error
        self._stop = threading.Event()
        self._thread = None
        self._keyboard = _Keyboard()
        self.state = "starting"
        self.score = 0.0
        self.mouth_open = 0.0

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._keyboard.release()

    def _run(self):
        capture = cv2.VideoCapture(self.camera, cv2.CAP_DSHOW)
        if not capture.isOpened():
            self._fail(f"could not open camera {self.camera}")
            return
        self.state = "inside"
        try:
            import mediapipe as mp
            from mediapipe.tasks import python
            from mediapipe.tasks.python import vision
            model_path = _ensure_model()
            options = vision.FaceLandmarkerOptions(
                base_options=python.BaseOptions(model_asset_path=model_path),
                running_mode=vision.RunningMode.VIDEO,
                num_faces=1,
                min_face_detection_confidence=.6,
                min_face_presence_confidence=.6,
                min_tracking_confidence=.6)
            landmarker = vision.FaceLandmarker.create_from_options(options)
        except Exception as error:
            capture.release()
            self._fail(f"MediaPipe setup failed: {error}")
            return
        out_count = in_count = 0
        timestamp = 0
        try:
            with landmarker:
                while not self._stop.is_set():
                    ok, frame = capture.read()
                    if not ok:
                        time.sleep(.05)
                        continue
                    timestamp += 33
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    result = landmarker.detect_for_video(
                        mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), timestamp)
                    tongue_out = self._tongue_out(frame, result)
                    out_count = out_count + 1 if tongue_out else 0
                    in_count = in_count + 1 if not tongue_out else 0
                    if out_count >= 3:
                        self.state = "OUT"
                    elif in_count >= 5:
                        self.state = "inside"
                    if out_count >= 3:
                        self._keyboard.set_down(self._keyboard.minecraft_focused())
                    elif in_count >= 5 or not self._keyboard.minecraft_focused():
                        self._keyboard.release()
                    if self.preview:
                        cv2.putText(frame, f"{self.state}  tongue {self.score:.3f}  open {self.mouth_open:.3f}",
                                    (12, 28), cv2.FONT_HERSHEY_SIMPLEX, .7,
                                    (0, 255, 0) if self.state == "OUT" else (255, 255, 255), 2)
                        cv2.imshow("Tongue detector - press Q to close", frame)
                        if cv2.waitKey(1) & 0xFF == ord("q"):
                            self._stop.set()
        except Exception as error:
            self._fail(f"{type(error).__name__}: {error}")
        finally:
            capture.release()
            if self.preview:
                cv2.destroyWindow("Tongue detector - press Q to close")
            self._keyboard.release()

    def _tongue_out(self, frame, result):
        if not result.face_landmarks:
            self.score = self.mouth_open = 0.0
            return False
        landmarks = result.face_landmarks[0]
        height, width = frame.shape[:2]

        def point(index):
            landmark = landmarks[index]
            return int(landmark.x * width), int(landmark.y * height)

        left = point(78)
        right = point(308)
        upper = point(13)
        lower = point(14)
        outer_lower = point(17)
        mouth_width = max(1.0, abs(right[0] - left[0]))
        self.mouth_open = abs(lower[1] - upper[1]) / mouth_width
        if self.preview:
            contour = np.array([left, upper, right, outer_lower], dtype=np.int32)
            cv2.polylines(frame, [contour], True, (0, 180, 255), 2)

        # Only inspect the lower inner-mouth area. This excludes cheeks and
        # most skin, which was the source of the old always-OUT false positives.
        roi = np.zeros(frame.shape[:2], dtype=np.uint8)
        tongue_region = [left, upper, right, lower, outer_lower]
        cv2.fillPoly(roi, [np.array(tongue_region, dtype=np.int32)], 255)
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        pink = cv2.inRange(hsv, np.array((0, 45, 55)), np.array((20, 255, 255)))
        pink |= cv2.inRange(hsv, np.array((160, 45, 55)), np.array((179, 255, 255)))
        visible = cv2.countNonZero(cv2.bitwise_and(pink, roi))
        area = cv2.countNonZero(roi)
        self.score = visible / area if area else 0.0
        return self.mouth_open >= .055 and self.score >= self.threshold

    def _fail(self, message):
        self.state = f"error: {message}"
        self._keyboard.release()
        if self.on_error:
            self.on_error(message)


def _ensure_model():
    if _MODEL_PATH.exists():
        return str(_MODEL_PATH)
    try:
        urllib.request.urlretrieve(_MODEL_URL, _MODEL_PATH)
    except Exception as error:
        raise TongueError(f"could not download Face Landmarker model: {error}") from error
    return str(_MODEL_PATH)
