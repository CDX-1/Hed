"""Shared state between the voice overlay and the head tracker.

Two processes, one JSON file: the overlay writes settings when you speak
("hed, game mode", "hed max"), and the tracker polls the file to pick them up.
Atomic writes (write-then-rename) so a mid-write read never sees half a file.

Anything the tracker cares about lives here rather than in per-feature files,
so a single poll picks up every change at once.
"""

import json
import os
import sys
import time


def state_dir():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Hed")
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Hed")
    return os.path.expanduser("~/.hed")


STATE_PATH = os.path.join(state_dir(), "state.json")

# Mouse speed multiplier presets. Applied on top of --mouse-speed, so the
# baseline the user launched with stays the reference "reset" position.
MOUSE_SCALE_MIN = 0.35
MOUSE_SCALE_DEFAULT = 1.0
MOUSE_SCALE_MAX = 2.5

DEFAULT = {
    "mode": "casual",
    "mouse_scale": MOUSE_SCALE_DEFAULT,
}


def load():
    try:
        with open(STATE_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return dict(DEFAULT)
    if not isinstance(data, dict):
        return dict(DEFAULT)
    out = dict(DEFAULT)
    for key, default in DEFAULT.items():
        value = data.get(key, default)
        if isinstance(default, (int, float)) and isinstance(value, (int, float)):
            out[key] = float(value)
        elif isinstance(default, str) and isinstance(value, str):
            out[key] = value
    return out


def save(**patch):
    """Merge `patch` into the state file. Unknown keys pass through."""
    os.makedirs(state_dir(), exist_ok=True)
    try:
        with open(STATE_PATH) as f:
            data = json.load(f)
            if not isinstance(data, dict):
                data = {}
    except (OSError, ValueError):
        data = {}
    data.update(patch)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, STATE_PATH)
    return data


class Watcher:
    """Polls STATE_PATH; on any change, calls on_change(new_state)."""

    _POLL = 0.2

    def __init__(self, on_change=None):
        self.state = dict(DEFAULT)
        self.on_change = on_change
        self._mtime = None
        self._at = 0.0
        # Prime immediately so a lingering state from a previous session
        # takes effect on the very first sample.
        self.refresh(force=True)

    def refresh(self, now=None, force=False):
        now = now if now is not None else time.monotonic()
        if not force and now - self._at < self._POLL:
            return False
        self._at = now
        try:
            mtime = os.stat(STATE_PATH).st_mtime
        except OSError:
            mtime = None
        if not force and mtime == self._mtime:
            return False
        self._mtime = mtime
        new_state = load()
        if new_state == self.state and not force:
            return False
        old = self.state
        self.state = new_state
        if self.on_change and (new_state != old or force):
            try:
                self.on_change(new_state)
            except Exception:
                pass
        return True


def clamp_mouse_scale(value):
    return max(MOUSE_SCALE_MIN, min(MOUSE_SCALE_MAX, float(value)))
