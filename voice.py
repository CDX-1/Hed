"""Voice for the island overlay: listening, audio levels, and typing into any app.

The recognizer itself is the hed-speech hub (speech_mac/), which this module
starts if nobody else has. The Chrome extension attaches to the same hub, so
there is one microphone, one transcript, and three consumers that must not step
on each other:

  - Voice typing (here): final transcripts are typed into whatever text field
    has focus, in any app.
  - Wake commands (the extension): "hey hed, new tab" runs in the browser and
    must not also be typed. The extension announces every command it runs as
    "consumed"; typing holds back anything that looks like one until it knows.
  - In-page dictation (the extension): stands down while voice typing is on,
    because we already cover browser text fields.

UI-agnostic: the overlay polls snapshot() on its own timer. Everything that can
block (the socket, Accessibility queries, typing) runs off the main thread.
"""

import collections
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

import hedstate
import voicekeys

_HERE = os.path.dirname(os.path.abspath(__file__))
SPEECH_DIR = os.path.join(_HERE, "speech_mac")
BINARY = os.path.join(SPEECH_DIR, "build", "hed-speech")
SOCKET_PATH = os.path.expanduser("~/Library/Application Support/Hed/speech.sock")
LOG_PATH = os.path.expanduser("~/Library/Logs/Hed/voice.log")

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
_log_file = open(LOG_PATH, "a", buffering=1)


def log(*parts):
    """Voice fails quietly from the user's side (nothing gets typed), so say
    what happened somewhere: ~/Library/Logs/Hed/voice.log, hub output included."""
    _log_file.write(time.strftime("%H:%M:%S ") + " ".join(str(p) for p in parts) + "\n")

# Mirrors findWake() in chrome_extension/commands.js - only as much as typing
# needs to know: is this utterance addressed to Hed rather than dictated?
_HEY_WAKE = re.compile(
    r"\b(?:hey|hay|hi|okay|ok)[\s,.!]+(?:hed|head|hedd|hedge|heads|heddy|hedy|hedz|edd|ed|ted|had)\b[\s,.:;!?-]*",
    re.I)
_BARE_WAKE = re.compile(r"^[\s,.!?-]*(?:hed|head|hedd|heddy|hedy|hedz)\b[\s,.:;!?-]*", re.I)
_STOP_PHRASES = {"stop listening", "stop dictation", "stop dictating", "stop voice typing",
                 "stop typing", "voice off"}
_NEWLINE_PHRASES = {"new line": 1, "newline": 1, "new paragraph": 2, "press enter": 1,
                    "press return": 1}

# How long to wait for the extension to claim an utterance as a command.
CLAIM_WAIT = 0.7
# A bare "hed" makes the next utterance a probable command for this long.
AWAIT_COMMAND = 6.0
# Text held back because focus was not a text field is dropped after this.
PENDING_TTL = 12.0


def _plain(text):
    return re.sub(r"[^\w\s']", "", text).lower().strip()


# Contractions the recognizer sometimes emits without an apostrophe. Only the
# bare forms with no plausible other meaning - "its", "were", "well", "wed",
# "lets", "id", "ill", "cant", "wont" are all real words and stay untouched.
_CONTRACTIONS = {
    "im": "I'm", "ive": "I've",
    "dont": "don't", "doesnt": "doesn't", "didnt": "didn't",
    "couldnt": "couldn't", "wouldnt": "wouldn't", "shouldnt": "shouldn't",
    "isnt": "isn't", "arent": "aren't", "wasnt": "wasn't", "werent": "weren't",
    "hasnt": "hasn't", "havent": "haven't", "hadnt": "hadn't",
    "youre": "you're", "youve": "you've", "youll": "you'll", "youd": "you'd",
    "theyre": "they're", "theyve": "they've", "theyll": "they'll", "theyd": "they'd",
    "weve": "we've", "shes": "she's", "hes": "he's",
    "thats": "that's", "whats": "what's", "hows": "how's",
}
# The recognizer sometimes drops a space before a comma or period, or leaves
# no space after one. Neither trip up a reader in isolation, but stacked over
# a whole paragraph the result reads like machine output.
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([,.!?;:])")
_SPACE_AFTER_PUNCT = re.compile(r"([,.!?;:])([A-Za-z])")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")


def _polish(text):
    """Tidy a transcribed utterance so it reads like written text.

    Only fixes things the recognizer routinely gets wrong: whitespace around
    punctuation, standalone lowercase "i" and its contractions. It leaves
    capitalization and sentence structure alone - Apple's on-device model is
    already reasonable, and this is the wrong place to rewrite what someone
    said.
    """
    text = text.strip()
    if not text:
        return text
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = _SPACE_AFTER_PUNCT.sub(r"\1 \2", text)
    text = _MULTI_SPACE.sub(" ", text)

    def fix_word(match):
        word = match.group(0)
        lower = word.lower()
        if lower == "i":
            return "I"
        if lower in _CONTRACTIONS:
            # Preserve original capitalization of the first letter.
            fixed = _CONTRACTIONS[lower]
            return fixed if word[0].islower() or fixed[0].isupper() else fixed[0].upper() + fixed[1:]
        return word

    text = re.sub(r"\b[A-Za-z]+(?:'[A-Za-z]+)?\b", fix_word, text)
    return text


class SpeechLink:
    """A connection to the hub, starting the hub if there is none."""

    def __init__(self, on_message, on_state):
        self.on_message = on_message
        self.on_state = on_state
        self.sock = None
        self.hub = None
        self.lock = threading.Lock()
        self.closing = False
        self.hello = []        # messages to resend after every (re)connect

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def send(self, msg, sticky=False):
        data = (json.dumps(msg) + "\n").encode()
        with self.lock:
            if sticky:
                self.hello = [m for m in self.hello if m["type"] != msg["type"]
                              and not (msg["type"] in ("listen", "stop") and m["type"] in ("listen", "stop"))]
                self.hello.append(msg)
            sock = self.sock
        if sock:
            try:
                sock.sendall(data)
            except OSError:
                pass

    def close(self):
        self.closing = True
        with self.lock:
            sock, self.sock = self.sock, None
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        if self.hub and self.hub.poll() is None:
            self.hub.stdin.close()   # the hub exits when its stdin closes

    def _run(self):
        backoff = 0.5
        while not self.closing:
            try:
                sock = self._connect()
            except RuntimeError as e:
                self.on_state("error", str(e))
                return
            if not sock:
                self.on_state("error", "speech engine did not start")
                time.sleep(min(backoff, 5))
                backoff *= 2
                continue
            backoff = 0.5
            with self.lock:
                self.sock = sock
                hello = list(self.hello)
            self.on_state("connected", None)
            for m in hello:
                self.send(m)
            self._read(sock)
            with self.lock:
                self.sock = None
            if not self.closing:
                self.on_state("disconnected", None)
                time.sleep(0.5)

    def _read(self, sock):
        buf = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    self.on_message(json.loads(line))
                except ValueError:
                    pass

    def _try_connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(SOCKET_PATH)
            return s
        except OSError:
            s.close()
            return None

    def _connect(self):
        sock = self._try_connect()
        if sock:
            return sock
        if not os.path.exists(BINARY):
            self._build()
        if self.hub is None or self.hub.poll() is not None:
            log("starting hub", BINARY)
            self.hub = subprocess.Popen([BINARY, "--hub"], stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL, stderr=_log_file)
        for _ in range(60):
            time.sleep(0.05)
            sock = self._try_connect()
            if sock:
                return sock
        return None

    def _build(self):
        if sys.platform != "darwin":
            raise RuntimeError("voice needs macOS 26+")
        self.on_state("building", None)
        os.makedirs(os.path.dirname(BINARY), exist_ok=True)
        r = subprocess.run(["xcrun", "swiftc", "-O", "-swift-version", "5",
                            os.path.join(SPEECH_DIR, "HedSpeech.swift"), "-o", BINARY],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("could not build the speech engine - run speech_mac/install.sh")


class VoiceTyping:
    """Voice mode for the whole OS. Toggle it; poll snapshot() to draw it."""

    def __init__(self, typer, lang="en-US"):
        self.typer = typer
        self.lang = lang
        self.link = SpeechLink(self._on_message, self._on_link)

        self.active = False
        self.listening = False        # the recognizer is running, for anyone
        self.levels = collections.deque([0.0] * 40, maxlen=40)
        self.last_level_at = 0.0
        self.interim = ""
        self.interim_at = 0.0
        self.notice = ""              # status / error shown instead of text
        self.notice_at = 0.0
        self.typed = ""               # last thing typed, flashed briefly
        self.typed_at = 0.0
        self.focus = None
        self.trusted = True

        self._last_consumed = 0.0
        self._claimed = 0.0           # the consumed signal already matched up
        self._await_until = 0.0
        self._pending = []            # (text, at) waiting for a text field
        self._last_typed_pid = None
        self._last_typed_char = ""
        self._jobs = collections.deque()
        self._wake = threading.Event()

    # -- control -------------------------------------------------------------

    def start(self):
        self.link.start()
        self.link.send({"type": "monitor"}, sticky=True)
        threading.Thread(target=self._worker, daemon=True).start()

    def close(self):
        if self.active:
            self.set_active(False)
        self.link.close()

    def toggle(self):
        self.set_active(not self.active)

    def set_active(self, on):
        if on == self.active:
            return
        log("voice typing", "on" if on else "off")
        if on and not self.typer.is_trusted():
            self.typer.request_trust()
            self._say("Allow Accessibility for Hed to type")
        self.active = on
        self._pending.clear()
        if on:
            self.link.send({"type": "listen", "lang": self.lang, "levels": True,
                            "hints": ["hey hed", "Hed"]}, sticky=True)
        else:
            self.link.send({"type": "stop"}, sticky=True)
            self.interim = ""
        self.link.send({"type": "typing", "active": on}, sticky=True)
        self._wake.set()

    # -- what the UI draws ---------------------------------------------------

    def snapshot(self):
        now = time.monotonic()
        hearing = now - self.last_level_at < 0.6
        if self.notice and now - self.notice_at < 6.0:
            caption, tone = self.notice, "notice"
        elif self.active and not self.trusted:
            caption, tone = "Allow Accessibility for this app to type (System Settings)", "notice"
        elif self.interim and now - self.interim_at < 2.0:
            caption, tone = self.interim, "interim"
        elif self.typed and now - self.typed_at < 1.5:
            caption, tone = self.typed, "typed"
        elif self._pending:
            caption, tone = "Click into a text field to type", "notice"
        elif self.active and self.focus is not None and self.focus.kind == "other":
            caption, tone = "Click into a text field", "hint"
        elif self.active:
            caption, tone = "Listening…", "hint"
        else:
            caption, tone = "", "hint"
        return {
            "active": self.active,
            "hearing": hearing,             # the mic is live for somebody
            "levels": list(self.levels),
            "caption": caption,
            "tone": tone,
        }

    # -- hub -----------------------------------------------------------------

    def _say(self, text):
        self.notice, self.notice_at = text, time.monotonic()

    def _on_link(self, state, detail):
        log("link", state, detail or "")
        if state == "building":
            self._say("Building speech engine…")
        elif state == "error":
            self._say(detail or "Speech engine unavailable")
        elif state == "disconnected":
            self.listening = False

    def _on_message(self, m):
        kind = m.get("type")
        if kind != "level" and not (kind == "result" and not m.get("final")):
            log("hub:", json.dumps(m))
        now = time.monotonic()
        if kind == "level":
            self.levels.append(float(m.get("level", 0)))
            self.last_level_at = now
        elif kind == "status":
            status = m.get("status")
            self.listening = status == "listening"
            if status == "downloading":
                self._say("Downloading speech model…")
            elif status == "listening" and self.notice.startswith("Downloading"):
                self.notice = ""
        elif kind == "error":
            friendly = {
                "not-allowed": "Microphone access denied - allow it in System Settings",
                "language-not-supported": "No on-device speech model for " + self.lang,
                "audio-capture": "No microphone available",
            }
            self._say(friendly.get(m.get("error")) or "Voice: " + (m.get("detail") or m.get("error") or "error"))
            if self.active and m.get("error") in ("not-allowed", "language-not-supported",
                                                  "audio-capture"):
                self.set_active(False)
        elif kind == "consumed":
            self._last_consumed = now
            self.interim = ""
        elif kind == "result" and self.active:
            text = (m.get("text") or "").strip()
            if not m.get("final"):
                if text:
                    self.interim, self.interim_at = text, now
                return
            self.interim = ""
            if text:
                self._jobs.append(("final", text, now))
                self._wake.set()

    # -- typing (worker thread) ------------------------------------------------

    def _worker(self):
        while True:
            self._wake.wait(0.35)
            self._wake.clear()
            if not self.active:
                self._jobs.clear()
                continue
            try:
                self.trusted = self.typer.is_trusted()
                self.focus = self.typer.focused()
            except Exception as e:
                log("focus lookup failed:", e)
                self.focus = None
            while self._jobs:
                _, text, at = self._jobs.popleft()
                self._handle_final(text, at)
            self._flush_pending()

    def _handle_final(self, text, at):
        plain = _plain(text)
        bare = _BARE_WAKE.match(text)
        rest_text = text[bare.end():] if bare else text
        rest = _plain(rest_text)
        if plain in _STOP_PHRASES or rest in _STOP_PHRASES:
            self.set_active(False)
            self._say("Voice typing off")
            return

        hey = _HEY_WAKE.search(text)
        if hey and _plain(text[hey.end():]) in _STOP_PHRASES:
            self.set_active(False)
            self._say("Voice typing off")
            return
        if hey:
            # Addressed to Hed outright: a command, never dictation. If nothing
            # runs it, the extension shows its own "didn't understand".
            after = text[hey.end():]
            if not _plain(after):
                self._await_until = at + AWAIT_COMMAND
                return
            self._run_overlay_command(after)
            return
        if bare and not rest:
            self._await_until = at + AWAIT_COMMAND
            return

        if bare or at < self._await_until:
            # Overlay commands (keys, shortcuts, mode toggle, sensitivity) fire
            # straight away - the extension knows to leave these alone, so
            # nothing else will claim them.
            if self._run_overlay_command(rest_text):
                self._await_until = 0
                return
            # Probably a browser command ("hed, new tab" or the words after a
            # lone "hed"), but "head of sales said…" is dictation. Give whoever
            # handles commands a moment to claim it.
            def claimed():
                c = self._last_consumed
                return c > self._claimed and c >= at - 2.5
            deadline = at + CLAIM_WAIT
            while time.monotonic() < deadline and not claimed():
                time.sleep(0.05)
            self._await_until = 0
            if claimed():
                # Each command claims one utterance, not everything after it.
                self._claimed = self._last_consumed
                return

        if plain in _NEWLINE_PHRASES:
            for _ in range(_NEWLINE_PHRASES[plain]):
                self.typer.press_return()
            self._last_typed_char = "\n"
            self.typed, self.typed_at = "↵", time.monotonic()
            return

        self._deliver(text, at)

    def _deliver(self, text, at):
        focus = self.focus
        if focus is not None and focus.kind == "other":
            self._pending.append((text, at))
            return
        self._type(text, focus)

    def _flush_pending(self):
        now = time.monotonic()
        self._pending = [(t, at) for t, at in self._pending if now - at < PENDING_TTL]
        if self._pending and self.focus is not None and self.focus.kind != "other":
            pending, self._pending = self._pending, []
            for text, _ in pending:
                self._type(text, self.focus)

    def _press_key(self, kp):
        log("press", kp)
        try:
            self.typer.press_key(kp.key, kp.modifiers, kp.times)
        except Exception as e:
            log("press_key failed:", e)
            return
        self.typed, self.typed_at = kp.label, time.monotonic()
        # Anything half-heard from this utterance is stale now.
        self.interim = ""
        # Reset the caret-continuation guess: a keypress can move focus, split
        # sentences, or dismiss the field entirely - the next dictated phrase
        # should start clean.
        self._last_typed_pid = None
        self._last_typed_char = ""

    def _run_overlay_command(self, text):
        """Try each overlay-owned command in turn. Returns True if one matched.

        Order matters when phrases could overlap: a single-key phrase wins
        over a shortcut ("hed, c" vs "hed, copy" - the parsers cover disjoint
        vocabularies, but the check runs cheaply either way).
        """
        kp = voicekeys.parse(text)
        if kp:
            self._press_key(kp)
            return True
        steps = voicekeys.parse_shortcut(text)
        if steps:
            self._run_shortcut(text, steps)
            return True
        mode = voicekeys.parse_mode(text)
        if mode:
            self._set_game_mode(mode)
            return True
        sens = voicekeys.parse_sensitivity(text)
        if sens:
            self._set_sensitivity(sens)
            return True
        return False

    def _run_shortcut(self, name, steps):
        log("shortcut", name.strip(), steps)
        for step in steps:
            try:
                self.typer.press_key(step.key, step.modifiers, step.times)
            except Exception as e:
                log("shortcut step failed:", e)
                return
            time.sleep(0.02)          # small gap so apps see distinct events
        self.typed, self.typed_at = " ".join(s.label for s in steps), time.monotonic()
        self.interim = ""
        self._last_typed_pid = None
        self._last_typed_char = ""

    def _set_game_mode(self, mode):
        """Persist the mode; the tracker polls the state file and holds
        arrow keys while it says "game"."""
        log("game mode", mode)
        try:
            hedstate.save(mode=mode)
        except OSError as e:
            log("save state failed:", e)
            self._say("Could not switch mode")
            return
        self._say("Game mode on" if mode == "game" else "Casual mode")
        self.interim = ""

    def _set_sensitivity(self, preset):
        """Persist a mouse-speed preset; the tracker scales the cursor speed."""
        scale = {"min": hedstate.MOUSE_SCALE_MIN,
                 "max": hedstate.MOUSE_SCALE_MAX,
                 "reset": hedstate.MOUSE_SCALE_DEFAULT}[preset]
        log("mouse sensitivity", preset, scale)
        try:
            hedstate.save(mouse_scale=scale)
        except OSError as e:
            log("save state failed:", e)
            self._say("Could not change sensitivity")
            return
        self._say({"min": "Mouse slow", "max": "Mouse fast",
                   "reset": "Mouse reset"}[preset])
        self.interim = ""

    def _type(self, text, focus):
        before = focus.before if focus is not None else None
        pid = focus.pid if focus is not None else None
        if before is None:
            # No caret info: assume we are continuing our own last phrase if it
            # went to the same app.
            before = self._last_typed_char if pid == self._last_typed_pid else ""

        text = _polish(text)
        if not text:
            return

        if before and not before.isspace() and before not in "([{\"'“‘/":
            text = " " + text
            # The recognizer capitalizes every phrase; mid-sentence, undo that.
            if before not in ".!?…" and len(text) > 2 and text[1].isupper() \
                    and not text[2].isupper() and not re.match(r" I\b", text):
                text = " " + text[1].lower() + text[2:]

        log("type", repr(text), "focus:", focus)
        self.typer.type_text(text)
        self._last_typed_pid = pid
        self._last_typed_char = text[-1]
        self.typed, self.typed_at = text.strip(), time.monotonic()
