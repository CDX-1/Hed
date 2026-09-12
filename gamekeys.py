"""Game mode: hold arrow keys while your head is turned.

Hed's voice toggle ("hed, game mode" / "hed, casual mode") writes the current
mode to a small file; this reads it and, while the mode is "game", maps head
yaw and pitch to held arrow keys - so you can walk a character around by
looking around. Casual (the default, and whatever is written when the file is
missing or unreadable) leaves the keyboard alone.

Same one-fixed-direction rule as the mouse: once yaw crosses the deadzone, the
corresponding arrow key stays down until the head comes back inside. Looking
further does not push harder - the game has its own turn rate. Roll is ignored:
there is no fourth arrow to spend on it.

Cross-platform via ctypes (CoreGraphics on macOS, user32 on Windows), for the
same reason as mouse.py: the tracker runs on both, and this is a handful of
symbols that does not justify a heavier dependency. Voice is macOS-only, so on
Windows the mode file will simply never appear and the class stays quiet.
"""

import ctypes
import ctypes.util
import os
import sys
import threading
import time
from ctypes import wintypes


def state_dir():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Hed")
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Hed")
    return os.path.expanduser("~/.hed")


MODE_PATH = os.path.join(state_dir(), "mode")

# Sensors run at ~100 Hz; re-reading the file this often is smooth enough to
# feel instant without spending every sample on a stat call.
_MODE_POLL = 0.2

# macOS virtual key codes for the arrows, plus the function/keypad flags that
# some apps demand before they take synthetic arrow keys seriously.
_MAC_KEYCODES = {"up": 126, "down": 125, "left": 123, "right": 124}
_MAC_NAV_FLAGS = 0x800000 | 0x200000
_MAC_HID_TAP = 0

# Windows virtual key codes. Arrows are extended keys, and games notice.
_WIN_KEYCODES = {"up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27}
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_EXTENDEDKEY = 0x0001


def write_mode(mode):
    """Persist a mode choice. Called from the voice process."""
    mode = "game" if mode == "game" else "casual"
    os.makedirs(state_dir(), exist_ok=True)
    with open(MODE_PATH, "w") as f:
        f.write(mode)
    return mode


def read_mode():
    try:
        with open(MODE_PATH) as f:
            return "game" if f.read().strip().lower() == "game" else "casual"
    except OSError:
        return "casual"


class GameKeys:
    """Head yaw/pitch -> held arrow keys, gated by the mode file."""

    def __init__(self, deadzone=8.0, mode_path=MODE_PATH):
        self.deadzone = deadzone
        self.mode_path = mode_path
        self.mode = "casual"
        self.origin = None       # (yaw, pitch) - retaken each time we enter game mode
        self._held = set()
        self._lock = threading.Lock()
        self._mode_at = 0.0
        self._mode_mtime = None
        self._load_driver()
        # Pick up an already-written mode at startup so a lingering "game" from
        # a previous session takes effect immediately.
        self._refresh_mode(time.monotonic(), force=True)

    # -- driver setup ----------------------------------------------------

    def _load_driver(self):
        self.windows = os.name == "nt"
        self.cg = self.cf = None
        self.user32 = None
        if self.windows:
            self.user32 = ctypes.windll.user32
            self.user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE,
                                                wintypes.DWORD, ctypes.c_ulong]
            self.user32.keybd_event.restype = None
            return
        app_path = ctypes.util.find_library("ApplicationServices")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not app_path or not cf_path:
            return
        cg = ctypes.cdll.LoadLibrary(app_path)
        cf = ctypes.cdll.LoadLibrary(cf_path)
        cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        self.cg, self.cf = cg, cf

    # -- mode file -------------------------------------------------------

    def _refresh_mode(self, now, force=False):
        if not force and now - self._mode_at < _MODE_POLL:
            return
        self._mode_at = now
        try:
            st = os.stat(self.mode_path)
        except OSError:
            self._mode_mtime = None
            self._apply_mode("casual")
            return
        if not force and st.st_mtime == self._mode_mtime:
            return
        self._mode_mtime = st.st_mtime
        try:
            with open(self.mode_path) as f:
                value = f.read().strip().lower()
        except OSError:
            value = ""
        self._apply_mode("game" if value == "game" else "casual")

    def _apply_mode(self, mode):
        if mode == self.mode:
            return
        self.mode = mode
        # Every mode change starts from where the head currently sits, so the
        # arrows do not fire on whatever drift accumulated while we were off.
        self.origin = None
        self._release_all()

    # -- head aim --------------------------------------------------------

    def aim(self, yaw, pitch, roll=None):
        """One sample from the tracker. No-op unless game mode is on."""
        now = time.monotonic()
        self._refresh_mode(now)
        if self.mode != "game":
            return
        if self.origin is None:
            self.origin = (yaw, pitch)
            return
        dy = _wrap(yaw - self.origin[0])
        dp = pitch - self.origin[1]

        want = set()
        # +yaw is turning left; +pitch is looking up. Match a first-person game
        # where looking maps to walking, so looking left presses Left, etc.
        if dy > self.deadzone:
            want.add("left")
        elif dy < -self.deadzone:
            want.add("right")
        if dp > self.deadzone:
            want.add("up")
        elif dp < -self.deadzone:
            want.add("down")

        with self._lock:
            for key in self._held - want:
                self._set_key(key, False)
            for key in want - self._held:
                self._set_key(key, True)
            self._held = want

    # -- key event posting -----------------------------------------------

    def _set_key(self, key, down):
        if self.windows and self.user32 is not None:
            code = _WIN_KEYCODES[key]
            flags = _KEYEVENTF_EXTENDEDKEY | (0 if down else _KEYEVENTF_KEYUP)
            self.user32.keybd_event(code, 0, flags, 0)
            return
        if self.cg is None:
            return
        ev = self.cg.CGEventCreateKeyboardEvent(None, _MAC_KEYCODES[key], down)
        if not ev:
            return
        self.cg.CGEventSetFlags(ev, _MAC_NAV_FLAGS)
        self.cg.CGEventPost(_MAC_HID_TAP, ev)
        self.cf.CFRelease(ev)

    def _release_all(self):
        with self._lock:
            for key in list(self._held):
                self._set_key(key, False)
            self._held.clear()

    def stop(self):
        """Drop every held key on the way out - a stuck arrow ruins the game."""
        self._release_all()

    @property
    def held(self):
        with self._lock:
            return sorted(self._held)


def _wrap(deg):
    return (deg + 180.0) % 360.0 - 180.0
