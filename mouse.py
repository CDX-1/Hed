#!/usr/bin/env python3
"""Steer the macOS cursor from head orientation.

Direction only, never distance: once yaw (or pitch) passes the deadzone the
cursor slides at one fixed speed, and looking further does not make it faster.
So the head works like a d-pad around wherever it was pointing at startup -
look a little left and the cursor walks left until you come back to centre.

Clicks come off roll, the one axis the cursor does not use: tip your head over
towards a shoulder and back. Right shoulder is a left click, left shoulder is a
right click. Each tilt fires exactly once however long you hold it, and the
cursor freezes for the duration, so the gesture cannot drag the pointer off
whatever you were aiming at.

Movement runs on its own clock rather than on the sensor's, so the speed stays
the same whether samples arrive at 100 Hz or stutter over USB.

The centre is not fixed, because yaw is not: a 6-axis IMU has no compass, so
heading creeps however good the filter is, and a fixed centre would slowly
become "cursor drifts left forever". So the origin leaks towards wherever you
are currently looking - quickly while you are inside the deadzone, where the
cursor is not moving anyway and re-centring costs nothing, and slowly while you
are outside it, purely as a backstop for drift wider than the deadzone. Both
are far slower than a deliberate glance, so steering is unaffected; what gets
absorbed is the creep.

Posting cursor events needs Accessibility permission for whatever launched us
(Terminal, iTerm, ...): System Settings > Privacy & Security > Accessibility.
Without it the events are swallowed silently - nothing errors, the cursor just
never moves - so we check up front and say so.
"""

import collections
import ctypes
import ctypes.util
import math
import threading
import time

TICK = 1 / 120.0        # cursor updates per second; smooth without busy-spinning

_KCG_EVENT_MOUSE_MOVED = 5
_KCG_EVENT_LEFT_DOWN = 1
_KCG_EVENT_LEFT_UP = 2
_KCG_EVENT_RIGHT_DOWN = 3
_KCG_EVENT_RIGHT_UP = 4
_KCG_HID_EVENT_TAP = 0
_KCG_MOUSE_BUTTON_LEFT = 0
_KCG_MOUSE_BUTTON_RIGHT = 1
# Without a click count an app sees a press with no click, and plenty of them
# (Finder, most menus) just ignore it.
_KCG_MOUSE_EVENT_CLICK_STATE = 1

# Buttons, as (down event, up event, button number).
BUTTONS = {
    "left": (_KCG_EVENT_LEFT_DOWN, _KCG_EVENT_LEFT_UP, _KCG_MOUSE_BUTTON_LEFT),
    "right": (_KCG_EVENT_RIGHT_DOWN, _KCG_EVENT_RIGHT_UP, _KCG_MOUSE_BUTTON_RIGHT),
}
CLICK_HOLD = 0.02          # seconds the button stays down


class MouseError(Exception):
    pass


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _load():
    """Bind the handful of CoreGraphics calls we need, via ctypes.

    ctypes rather than pyobjc-framework-Quartz: it is four symbols, and it
    keeps the tracker's install down to pyserial for everyone not on --airpods.
    """
    app_path = ctypes.util.find_library("ApplicationServices")
    cf_path = ctypes.util.find_library("CoreFoundation")
    if not app_path or not cf_path:
        raise MouseError("--mouse needs macOS (CoreGraphics)")
    cg = ctypes.cdll.LoadLibrary(app_path)
    cf = ctypes.cdll.LoadLibrary(cf_path)

    cg.CGEventCreate.restype = ctypes.c_void_p
    cg.CGEventCreate.argtypes = [ctypes.c_void_p]
    cg.CGEventGetLocation.restype = _CGPoint
    cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                           _CGPoint, ctypes.c_uint32]
    cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    cg.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                               ctypes.c_int64]
    cg.AXIsProcessTrusted.restype = ctypes.c_bool
    cg.AXIsProcessTrusted.argtypes = []
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    return cg, cf


class Cursor:
    """The cursor, moved at a velocity someone else keeps setting."""

    # Seconds for the origin to close most of the gap to where you are looking
    # while the cursor is moving. Deliberate glances last a second or two, so
    # at this rate steering barely notices; drift, which takes minutes, does.
    RECENTER_MOVING = 30.0

    def __init__(self, speed=350.0, deadzone=6.0, invert_y=False, recenter=2.0,
                 click_angle=20.0, swap_clicks=False):
        self.cg, self.cf = _load()
        self.speed = speed
        self.deadzone = deadzone
        self.invert_y = invert_y
        self.recenter = recenter
        self.click_angle = click_angle
        # Tilting right is the easy one for most people, and left-click is the
        # one you do a hundred times an hour, so they are paired up.
        self.buttons = ("right", "left") if swap_clicks else ("left", "right")
        self.origin = None          # (yaw, pitch, roll) angles are measured from
        self._dir = (0.0, 0.0)      # -1, 0 or +1 per axis
        self._armed = True          # a tilt only fires on the way past, once
        self._last_click = ""
        self._last_aim = None
        self._clicks = collections.deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    @property
    def release_angle(self):
        """Come back inside this to re-arm - and the cursor unfreezes here too."""
        return self.click_angle * 0.5

    # -- geometry ---------------------------------------------------------

    def aim(self, yaw, pitch, roll):
        """Take a head angle and turn it into a direction, and maybe a click."""
        now = time.monotonic()
        if self.origin is None:
            # Where the head sat when tracking started is the centre: the
            # sensor's own zero is wherever the board happens to be mounted.
            self.origin = (yaw, pitch, roll)
            self._last_aim = now
        dt = min(now - self._last_aim, 0.5)   # a stall should not snap the origin
        self._last_aim = now

        dy_ang = _wrap(yaw - self.origin[0])
        dp_ang = pitch - self.origin[1]
        dr_ang = _wrap(roll - self.origin[2])
        x = _step(dy_ang, self.deadzone)
        y = _step(dp_ang, self.deadzone)

        tilted = self.click_angle > 0 and abs(dr_ang) >= self.release_angle
        if self.click_angle > 0:
            self._click_gesture(dr_ang)
        if tilted:
            # Head tilt bleeds a little into yaw and pitch, and a click that
            # slides the pointer off its target is a miss. Hold still instead.
            x = y = 0.0

        # Per axis, because one of them is often parked while the other steers.
        # Roll is the exception: gravity keeps it honest, so it has no drift to
        # chase, and its centre only ever follows a change of posture. Letting
        # it creep onto a held tilt would re-arm the click while the head was
        # still over, and firing the opposite button on the way back up.
        if self.recenter > 0:
            roll_leak = 0.0 if tilted else self._leak(False, dt)
            self.origin = (
                _wrap(self.origin[0] + dy_ang * self._leak(x, dt)),
                self.origin[1] + dp_ang * self._leak(y, dt),
                _wrap(self.origin[2] + dr_ang * roll_leak),
            )
        # Turning left is +yaw, and left on screen is -x. Looking up is +pitch,
        # and up the screen is -y (Quartz counts down from the top).
        if not self.invert_y:
            y = -y
        with self._lock:
            self._dir = (-x, y)

    def _leak(self, direction, dt):
        """Fraction of the way the origin slides towards you this sample."""
        tau = self.RECENTER_MOVING if direction else self.recenter
        return 1.0 - math.exp(-dt / tau)

    def _click_gesture(self, angle):
        """Edge-trigger a click on the way out past the threshold.

        One tilt, one click, no matter how long it is held - and nothing fires
        again until the head comes back inside the release angle. That gap is
        what stops a head resting near the threshold from machine-gunning
        clicks as it wobbles across it.
        """
        if self._armed:
            if abs(angle) < self.click_angle:
                return
            # +roll is a tilt towards the right shoulder.
            button = self.buttons[0] if angle > 0 else self.buttons[1]
            self._clicks.append(button)
            self._last_click = button
            self._armed = False
        elif abs(angle) < self.release_angle:
            self._armed = True

    @property
    def click_state(self):
        """Short description of the click gesture, for the dashboard."""
        if self.click_angle <= 0:
            return "clicks off"
        if not self._armed:
            return f"{self._last_click} click"
        return "ready"

    def zero(self):
        """Forget the origin; the next sample becomes the new centre."""
        self.origin = None

    @property
    def direction(self):
        with self._lock:
            return self._dir

    # -- the mover --------------------------------------------------------

    def trusted(self):
        return bool(self.cg.AXIsProcessTrusted())

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _location(self):
        ev = self.cg.CGEventCreate(None)
        p = self.cg.CGEventGetLocation(ev)
        self.cf.CFRelease(ev)
        return p.x, p.y

    def _move_to(self, x, y):
        ev = self.cg.CGEventCreateMouseEvent(
            None, _KCG_EVENT_MOUSE_MOVED, _CGPoint(x, y), _KCG_MOUSE_BUTTON_LEFT)
        self.cg.CGEventPost(_KCG_HID_EVENT_TAP, ev)
        self.cf.CFRelease(ev)

    def _click(self, button):
        """Press and release, where the cursor is now."""
        down, up, number = BUTTONS[button]
        x, y = self._location()
        for kind in (down, up):
            ev = self.cg.CGEventCreateMouseEvent(
                None, kind, _CGPoint(x, y), number)
            self.cg.CGEventSetIntegerValueField(
                ev, _KCG_MOUSE_EVENT_CLICK_STATE, 1)
            self.cg.CGEventPost(_KCG_HID_EVENT_TAP, ev)
            self.cf.CFRelease(ev)
            if kind is down:
                time.sleep(CLICK_HOLD)

    def _run(self):
        last = time.monotonic()
        carry_x = carry_y = 0.0
        while not self._stop.is_set():
            time.sleep(TICK)
            now = time.monotonic()
            dt = min(now - last, 0.1)     # a stalled thread should not teleport
            last = now
            # Clicks are posted from here rather than from aim(), so the button
            # hold does not stall the thread reading the serial port.
            while self._clicks:
                self._click(self._clicks.popleft())
            dx_dir, dy_dir = self.direction
            if dx_dir == 0.0 and dy_dir == 0.0:
                carry_x = carry_y = 0.0
                continue
            # Read the live position every tick instead of tracking our own:
            # the screen edges clamp it for us, and the trackpad still works
            # while the head is steering.
            x, y = self._location()
            carry_x += dx_dir * self.speed * dt
            carry_y += dy_dir * self.speed * dt
            step_x, carry_x = _split(carry_x)
            step_y, carry_y = _split(carry_y)
            if step_x or step_y:
                self._move_to(x + step_x, y + step_y)


def _wrap(deg):
    """Shortest way round: yaw lives on a circle, and origin may be near 180."""
    return (deg + 180.0) % 360.0 - 180.0


def _step(angle, deadzone):
    if angle > deadzone:
        return 1.0
    if angle < -deadzone:
        return -1.0
    return 0.0


def _split(v):
    """Whole pixels to move now, and the fraction to carry into the next tick."""
    whole = int(v)
    return whole, v - whole
