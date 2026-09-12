"""Spoken key presses, shortcuts, mode toggles and mouse sensitivity.

Four kinds of thing the words after the wake word can turn into:

  - a keypress    ("hed, enter", "hed, command c", "hed, backspace twice")
  - a shortcut    ("hed, copy", "hed, delete all") - one or more keypresses
  - a mode toggle ("hed, game mode", "hed, casual mode")
  - a sensitivity ("hed, min", "hed, max", "hed, reset")

Each is a small dictionary and a parse function. Platform-neutral names only;
ostext_mac turns them into key codes.

The Chrome extension must leave all of these alone (otherwise "hed, down"
would press the arrow *and* scroll the page, "hed, copy" would duplicate a
tab, and so on). chrome_extension/commands.js has parallel isKeyPhrase(),
isShortcutPhrase(), isModePhrase() and isSensitivityPhrase() helpers that
mirror the vocabulary here. Change one, change both.
"""

import re
import sys

MODIFIERS = {
    "command": "cmd", "cmd": "cmd", "comand": "cmd",
    "control": "ctrl", "ctrl": "ctrl",
    "option": "alt", "alt": "alt",
    "shift": "shift",
}

# Spoken name -> key. Multi-word names are matched before single words.
KEYS = {
    "enter": "return", "return": "return",
    "tab": "tab",
    "space": "space", "space bar": "space", "spacebar": "space",
    "backspace": "backspace", "back space": "backspace", "delete": "backspace",
    "forward delete": "forward-delete",
    "escape": "escape", "esc": "escape",
    "up": "up", "up arrow": "up", "arrow up": "up",
    "down": "down", "down arrow": "down", "arrow down": "down",
    "left": "left", "left arrow": "left", "arrow left": "left",
    "right": "right", "right arrow": "right", "arrow right": "right",
    "home": "home", "end": "end",
    "page up": "page-up", "page down": "page-down",
    **{f"f{i}": f"f{i}" for i in range(1, 13)},
}

# Letters and digits are only keys alongside a modifier ("command c"): alone,
# "hed, a" is far more likely a misheard sentence than a request to type "a".
LETTER_NAMES = {
    "a": "a", "ay": "a", "b": "b", "be": "b", "bee": "b", "c": "c", "see": "c",
    "sea": "c", "d": "d", "dee": "d", "e": "e", "ee": "e", "f": "f", "ef": "f",
    "g": "g", "gee": "g", "h": "h", "aitch": "h", "i": "i", "eye": "i",
    "j": "j", "jay": "j", "k": "k", "kay": "k", "l": "l", "el": "l",
    "m": "m", "em": "m", "n": "n", "en": "n", "o": "o", "oh": "o",
    "p": "p", "pee": "p", "q": "q", "queue": "q", "cue": "q", "r": "r",
    "are": "r", "s": "s", "es": "s", "t": "t", "tee": "t", "tea": "t",
    "u": "u", "you": "u", "v": "v", "vee": "v", "w": "w", "double u": "w",
    "x": "x", "ex": "x", "y": "y", "why": "y", "z": "z", "zee": "z", "zed": "z",
}
DIGITS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
    **{str(i): str(i) for i in range(10)},
}
COUNTS = {
    "once": 1, "twice": 2, "thrice": 3, "one": 1, "two": 2, "to": 2, "too": 2,
    "three": 3, "four": 4, "for": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10,
}
MAX_REPEAT = 20

_LEAD = ("press", "hit", "tap", "push", "type", "key")


class KeyPress:
    def __init__(self, key, modifiers=(), times=1):
        self.key = key
        self.modifiers = tuple(modifiers)
        self.times = times

    @property
    def label(self):
        names = {"cmd": "⌘", "ctrl": "⌃", "alt": "⌥", "shift": "⇧"}
        key = self.key.replace("-", " ").title() if len(self.key) > 1 else self.key.upper()
        text = "".join(names[m] for m in self.modifiers) + key
        return text + (f" ×{self.times}" if self.times > 1 else "")

    def __eq__(self, other):
        return isinstance(other, KeyPress) and (self.key, self.modifiers, self.times) == \
            (other.key, other.modifiers, other.times)

    def __repr__(self):
        return f"KeyPress({self.label})"


def parse(text):
    """The words after the wake word -> KeyPress, or None if they are not one."""
    words = re.sub(r"[^\w\s]", " ", text.lower()).split()
    while words and words[0] in _LEAD:
        words = words[1:]
    if words and words[-1] == "key":
        words = words[:-1]

    times = 1
    if len(words) >= 2 and words[-1] in ("times", "time"):
        n = words[-2]
        times = int(n) if n.isdigit() else COUNTS.get(n)
        if times is None:
            return None
        words = words[:-2]
        if words and words[-1] == "x":
            words = words[:-1]
    elif words and words[-1] in ("once", "twice", "thrice"):
        times = COUNTS[words[-1]]
        words = words[:-1]
    times = max(1, min(times, MAX_REPEAT))

    mods = []
    while words and words[0] in MODIFIERS:
        mod = MODIFIERS[words.pop(0)]
        if mod not in mods:
            mods.append(mod)
        if words and words[0] in ("plus", "and"):
            words.pop(0)
    if words and words[-1] == "key":
        words = words[:-1]
    if not words:
        return None

    name = " ".join(words)
    if name in KEYS:
        return KeyPress(KEYS[name], mods, times)
    if mods and (name in LETTER_NAMES or name in DIGITS):
        return KeyPress(LETTER_NAMES.get(name) or DIGITS[name], mods, times)
    return None


# "hed, game mode" flips the tracker into arrow-key mode (see gamekeys.py);
# "hed, casual mode" flips it back. Casual is the default at startup.
MODES = {
    "game mode": "game", "gamer mode": "game", "gaming mode": "game", "gaming": "game",
    "arrow mode": "game", "arrows": "game", "game": "game",
    "casual mode": "casual", "casual": "casual", "normal mode": "casual",
    "regular mode": "casual", "chat mode": "casual", "chill mode": "casual",
    "normal": "casual",
}
_MODE_LEAD = ("switch", "go", "enter", "enable", "turn", "activate", "start")
_MODE_GLUE = ("into", "to", "in", "on", "off", "the", "a", "into")


def parse_mode(text):
    """The words after the wake -> "game", "casual", or None."""
    words = re.sub(r"[^\w\s]", " ", text.lower()).split()
    while words and words[0] in _MODE_LEAD:
        words = words[1:]
    while words and words[0] in _MODE_GLUE:
        words = words[1:]
    while words and words[-1] in ("please", "now"):
        words = words[:-1]
    return MODES.get(" ".join(words))


# The clipboard-and-window shortcuts most editors share, keyed by spoken name.
# Each value is a list of (key, modifiers) pairs; they run in order.
# cmd on macOS is ctrl on other platforms (voice is macOS today - PLATFORM_MOD
# just future-proofs the mapping for when the tracker's platform matters).
PLATFORM_MOD = "cmd" if sys.platform == "darwin" else "ctrl"

SHORTCUTS = {
    "copy": [("c", (PLATFORM_MOD,))],
    "cut": [("x", (PLATFORM_MOD,))],
    "paste": [("v", (PLATFORM_MOD,))],
    "select all": [("a", (PLATFORM_MOD,))],
    "delete all": [("a", (PLATFORM_MOD,)), ("backspace", ())],
    "clear all": [("a", (PLATFORM_MOD,)), ("backspace", ())],
    "clear": [("a", (PLATFORM_MOD,)), ("backspace", ())],
    "undo": [("z", (PLATFORM_MOD,))],
    "redo": [("z", (PLATFORM_MOD, "shift"))],
    "save": [("s", (PLATFORM_MOD,))],
    "save all": [("s", (PLATFORM_MOD, "alt"))],
    "print": [("p", (PLATFORM_MOD,))],
    "minimize": [("m", (PLATFORM_MOD,))],
    "bold": [("b", (PLATFORM_MOD,))],
    "italic": [("i", (PLATFORM_MOD,))],
    "italics": [("i", (PLATFORM_MOD,))],
    "underline": [("u", (PLATFORM_MOD,))],
}
_SHORTCUT_LEAD = ("please", "do")
# Glue words the recognizer inserts after the lead ("do a copy", "do the paste").
_SHORTCUT_GLUE = ("a", "an", "the")
# Common recognizer swaps that would otherwise miss.
_SHORTCUT_ALIAS = {
    "kopy": "copy", "copie": "copy", "cop": "copy",
    "paist": "paste", "past": "paste",
    "cutt": "cut",
    "sellect": "select",
    "cleer": "clear", "kleer": "clear",
    "undue": "undo", "un do": "undo",
    "re do": "redo",
    "italicize": "italic",
}


def parse_shortcut(text):
    """The words after the wake -> [KeyPress, ...], or None if not a shortcut.

    "hed, copy" -> one cmd+C; "hed, delete all" -> cmd+A then Backspace.
    """
    words = re.sub(r"[^\w\s]", " ", text.lower()).split()
    while words and words[0] in _SHORTCUT_LEAD:
        words = words[1:]
        while words and words[0] in _SHORTCUT_GLUE:
            words = words[1:]
    while words and words[-1] in ("please", "now"):
        words = words[:-1]
    # Alias substitution word-by-word: "cop that" -> "copy that", useless;
    # but "cop" alone -> "copy" is a common mis-hear worth catching.
    normalized = " ".join(_SHORTCUT_ALIAS.get(w, w) for w in words)
    steps = SHORTCUTS.get(normalized)
    if not steps:
        return None
    return [KeyPress(key, mods) for key, mods in steps]


# Mouse sensitivity presets - three points on a slider, not a step control.
# "min" makes the head-cursor slow enough to hit tiny buttons; "max" whips it
# across the screen for jumping between windows; "reset" restores the launch
# value. The scale is a multiplier on --mouse-speed, so relative to whatever
# you started the tracker with.
SENSITIVITY = {
    "min": "min", "minimum": "min", "slow": "min", "slower": "min",
    "slowest": "min", "precise": "min", "precision": "min", "fine": "min",
    "tiny": "min", "small": "min", "low": "min",
    "max": "max", "maximum": "max", "fast": "max", "faster": "max",
    "fastest": "max", "quick": "max", "quicker": "max", "big": "max",
    "large": "max", "high": "max",
    "reset": "reset", "default": "reset", "normal speed": "reset",
    "medium": "reset", "middle": "reset", "regular": "reset",
    "reset mouse": "reset", "reset sensitivity": "reset",
    "reset speed": "reset",
}
# Words that only describe *what* is being set ("mouse", "sensitivity", ...)
# or are grammatical glue ("the", "to"); they can appear anywhere in the
# phrase and never carry meaning here.
_SENS_FILLER = {
    "set", "make", "mouse", "cursor", "sensitivity", "speed", "the", "a",
    "to", "at", "go", "please", "now",
}


def parse_sensitivity(text):
    """The words after the wake -> "min" | "max" | "reset", or None.

    Filler words are stripped from anywhere in the phrase, so "reset the
    sensitivity" and "make the mouse min" and "min speed" all reduce to a
    single content word that must be in the SENSITIVITY table.
    """
    words = re.sub(r"[^\w\s]", " ", text.lower()).split()
    # Try the raw phrase first, in case a two-word key ("normal speed") is
    # the whole thing - filler stripping would collapse it to "normal".
    hit = SENSITIVITY.get(" ".join(words))
    if hit:
        return hit
    words = [w for w in words if w not in _SENS_FILLER]
    return SENSITIVITY.get(" ".join(words))
