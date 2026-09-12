"""Spoken key presses: "hed, enter", "hed, command c", "hed, backspace three times".

Parses the words after the wake word into a key, its modifiers and a repeat
count. Platform-neutral names only; ostext_mac turns them into key codes.

The Chrome extension must leave these phrases alone (otherwise "hed, down"
would press the arrow *and* scroll the page), so isKeyPhrase() in
chrome_extension/commands.js mirrors KEYS, MODIFIERS and the grammar here.
Change one, change both.
"""

import re

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
