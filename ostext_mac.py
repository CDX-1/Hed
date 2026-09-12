"""Type into whatever text field has focus, in any macOS app.

Two jobs, both through the Accessibility API and CoreGraphics:

  - Find out what has keyboard focus, and whether it takes text. Typing into a
    focused button or a web page body is worse than typing nowhere: single
    letters are shortcuts in Gmail, Finder, Slack and friends. So a focus that
    is positively not a text field holds the text back instead.
  - Type the text as synthetic keyboard events carrying Unicode strings, which
    every app accepts - unlike setting AXValue, which web views and Electron
    ignore, or pasting, which clobbers the clipboard.

ctypes rather than pyobjc-framework-Quartz / ApplicationServices, for the same
reason as mouse.py: a dozen symbols do not justify two more dependencies.

Both need Accessibility permission for whatever launched us (Terminal, iTerm,
HeadTrack.app): System Settings > Privacy & Security > Accessibility.
"""

import ctypes
import ctypes.util
import threading
import time

from AppKit import NSWorkspace

_AS = ctypes.cdll.LoadLibrary(ctypes.util.find_library("ApplicationServices"))
_CF = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))

_UTF8 = 0x08000100
_AX_VALUE_CFRANGE = 4
_HID_TAP = 0


class _CFRange(ctypes.Structure):
    _fields_ = [("location", ctypes.c_long), ("length", ctypes.c_long)]


def _bind(lib, name, restype, *argtypes):
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


_vp = ctypes.c_void_p
_CFRelease = _bind(_CF, "CFRelease", None, _vp)
_CFStringCreateWithCString = _bind(_CF, "CFStringCreateWithCString", _vp, _vp, ctypes.c_char_p, ctypes.c_uint32)
_CFStringGetLength = _bind(_CF, "CFStringGetLength", ctypes.c_long, _vp)
_CFStringGetCString = _bind(_CF, "CFStringGetCString", ctypes.c_bool, _vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32)
_CFGetTypeID = _bind(_CF, "CFGetTypeID", ctypes.c_ulong, _vp)
_CFStringGetTypeID = _bind(_CF, "CFStringGetTypeID", ctypes.c_ulong)
_CFDictionaryCreate = _bind(_CF, "CFDictionaryCreate", _vp, _vp, ctypes.POINTER(_vp), ctypes.POINTER(_vp), ctypes.c_long, _vp, _vp)

_AXIsProcessTrusted = _bind(_AS, "AXIsProcessTrusted", ctypes.c_bool)
_AXIsProcessTrustedWithOptions = _bind(_AS, "AXIsProcessTrustedWithOptions", ctypes.c_bool, _vp)
_AXUIElementCreateSystemWide = _bind(_AS, "AXUIElementCreateSystemWide", _vp)
_AXUIElementCreateApplication = _bind(_AS, "AXUIElementCreateApplication", _vp, ctypes.c_int32)
_AXUIElementCopyAttributeValue = _bind(_AS, "AXUIElementCopyAttributeValue", ctypes.c_int32, _vp, _vp, ctypes.POINTER(_vp))
_AXUIElementSetAttributeValue = _bind(_AS, "AXUIElementSetAttributeValue", ctypes.c_int32, _vp, _vp, _vp)
_AXUIElementGetPid = _bind(_AS, "AXUIElementGetPid", ctypes.c_int32, _vp, ctypes.POINTER(ctypes.c_int32))
_AXUIElementSetMessagingTimeout = _bind(_AS, "AXUIElementSetMessagingTimeout", ctypes.c_int32, _vp, ctypes.c_float)
_AXValueGetValue = _bind(_AS, "AXValueGetValue", ctypes.c_bool, _vp, ctypes.c_uint32, _vp)

_CGEventCreateKeyboardEvent = _bind(_AS, "CGEventCreateKeyboardEvent", _vp, _vp, ctypes.c_uint16, ctypes.c_bool)
_CGEventKeyboardSetUnicodeString = _bind(_AS, "CGEventKeyboardSetUnicodeString", None, _vp, ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint16))
_CGEventSetFlags = _bind(_AS, "CGEventSetFlags", None, _vp, ctypes.c_uint64)
_CGEventPost = _bind(_AS, "CGEventPost", None, ctypes.c_uint32, _vp)

_strings = {}


def _cfstr(s):
    """A CFString for an attribute name, created once and kept for good."""
    ref = _strings.get(s)
    if ref is None:
        ref = _strings[s] = _CFStringCreateWithCString(None, s.encode(), _UTF8)
    return ref


def _to_str(ref):
    if not ref or _CFGetTypeID(ref) != _CFStringGetTypeID():
        return None
    size = _CFStringGetLength(ref) * 4 + 1
    buf = ctypes.create_string_buffer(size)
    if not _CFStringGetCString(ref, buf, size, _UTF8):
        return None
    return buf.value.decode("utf-8", "replace")


def _copy(element, attr):
    """Copy an attribute; the caller owns the result and must CFRelease it."""
    out = _vp()
    err = _AXUIElementCopyAttributeValue(element, _cfstr(attr), ctypes.byref(out))
    return out.value if err == 0 else None


def _copy_str(element, attr):
    ref = _copy(element, attr)
    try:
        return _to_str(ref)
    finally:
        if ref:
            _CFRelease(ref)


# -- permission --------------------------------------------------------------

def is_trusted():
    return bool(_AXIsProcessTrusted())


def request_trust():
    """Show the system "allow Accessibility" prompt if we are not trusted yet."""
    key = _vp.in_dll(_AS, "kAXTrustedCheckOptionPrompt")
    value = _vp.in_dll(_CF, "kCFBooleanTrue")
    keys = (_vp * 1)(key.value)
    values = (_vp * 1)(value.value)
    callbacks_k = ctypes.addressof(ctypes.c_char.in_dll(_CF, "kCFTypeDictionaryKeyCallBacks"))
    callbacks_v = ctypes.addressof(ctypes.c_char.in_dll(_CF, "kCFTypeDictionaryValueCallBacks"))
    opts = _CFDictionaryCreate(None, keys, values, 1, callbacks_k, callbacks_v)
    try:
        return bool(_AXIsProcessTrustedWithOptions(opts))
    finally:
        _CFRelease(opts)


# -- focus -------------------------------------------------------------------

TEXT_ROLES = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
# Focus that definitely does not take text. Anything else - a web area whose
# tree Chrome has not built yet, an app that exposes nothing - is "unknown",
# and gets the benefit of the doubt.
NON_TEXT_ROLES = {
    "AXButton", "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXMenuButton",
    "AXMenuItem", "AXMenu", "AXMenuBar", "AXMenuBarItem", "AXList", "AXOutline",
    "AXTable", "AXRow", "AXCell", "AXImage", "AXWindow", "AXApplication",
    "AXSlider", "AXTabGroup", "AXToolbar", "AXDisclosureTriangle", "AXLink",
    "AXStaticText", "AXBrowser", "AXColumn",
}


class Focus:
    """What had keyboard focus when we looked."""

    def __init__(self, kind, pid=None, role=None, before=None, secure=False):
        self.kind = kind          # "text" | "other" | "unknown"
        self.pid = pid
        self.role = role
        self.before = before      # the character just left of the caret, if known
        self.secure = secure

    def __repr__(self):
        return f"Focus({self.kind}, role={self.role}, pid={self.pid}, before={self.before!r})"


_system = None
_manual_ax_pids = set()


def _enable_app_accessibility(pid):
    """Chromium and Electron only build their accessibility tree for clients
    that ask for it; without this every web text field reads as "unknown"."""
    if pid in _manual_ax_pids:
        return
    _manual_ax_pids.add(pid)
    app = _AXUIElementCreateApplication(pid)
    if app:
        _AXUIElementSetAttributeValue(app, _cfstr("AXManualAccessibility"),
                                      _vp.in_dll(_CF, "kCFBooleanTrue"))
        _CFRelease(app)


def focused():
    global _system
    if _system is None:
        _system = _AXUIElementCreateSystemWide()
        _AXUIElementSetMessagingTimeout(_system, 0.25)

    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    front_pid = front.processIdentifier() if front else None
    if front_pid:
        _enable_app_accessibility(front_pid)

    # The system-wide element is the documented way in, but it gives up with
    # kAXErrorCannotComplete when some apps are frontmost; asking the frontmost
    # app directly still works then.
    element = _copy(_system, "AXFocusedUIElement")
    if not element and front_pid:
        app = _AXUIElementCreateApplication(front_pid)
        if app:
            _AXUIElementSetMessagingTimeout(app, 0.25)
            element = _copy(app, "AXFocusedUIElement")
            _CFRelease(app)
    if not element:
        return Focus("unknown", front_pid)
    try:
        pid = ctypes.c_int32()
        _AXUIElementGetPid(element, ctypes.byref(pid))

        role = _copy_str(element, "AXRole")
        subrole = _copy_str(element, "AXSubrole")
        secure = subrole == "AXSecureTextField"

        # Rich web editors (contenteditable) show up as groups or web areas
        # that nevertheless have a caret.
        rng = _copy(element, "AXSelectedTextRange")

        if role in TEXT_ROLES or secure or rng:
            kind = "text"
        elif role in NON_TEXT_ROLES:
            kind = "other"
        else:
            kind = "unknown"

        before = None
        if rng and not secure:
            loc = _CFRange()
            if _AXValueGetValue(rng, _AX_VALUE_CFRANGE, ctypes.byref(loc)):
                value = _copy_str(element, "AXValue")
                if value is not None:
                    units = value.encode("utf-16-le")
                    at = max(0, min(loc.location, len(units) // 2))
                    before = units[:at * 2].decode("utf-16-le", "ignore")[-1:] or ""
        if rng:
            _CFRelease(rng)
        return Focus(kind, pid.value, role, before, secure)
    finally:
        _CFRelease(element)


# -- typing ------------------------------------------------------------------

_type_lock = threading.Lock()


def _post_unicode(units):
    buf = (ctypes.c_uint16 * len(units))(*units)
    for down in (True, False):
        ev = _CGEventCreateKeyboardEvent(None, 0, down)
        # Clear modifiers: a held Shift or Caps Lock would otherwise leak in.
        _CGEventSetFlags(ev, 0)
        _CGEventKeyboardSetUnicodeString(ev, len(units), buf)
        _CGEventPost(_HID_TAP, ev)
        _CFRelease(ev)


# macOS virtual key codes (ANSI layout) for the names voicekeys.py produces.
KEYCODES = {
    "return": 36, "tab": 48, "space": 49, "backspace": 51, "escape": 53,
    "forward-delete": 117, "home": 115, "end": 119, "page-up": 116, "page-down": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29, "o": 31,
    "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
}
_MODIFIER_FLAGS = {"shift": 0x20000, "ctrl": 0x40000, "alt": 0x80000, "cmd": 0x100000}
# A real keyboard marks arrows and the navigation block as function/keypad
# keys; some apps ignore synthetic arrows without these.
_NAV_FLAGS = 0x800000 | 0x200000
_NAV_KEYS = {"left", "right", "up", "down", "home", "end", "page-up", "page-down",
             "forward-delete"}


def press_key(key, modifiers=(), times=1):
    """Press `key` (a voicekeys name) with modifiers held, `times` times."""
    code = KEYCODES[key]
    flags = 0
    for m in modifiers:
        flags |= _MODIFIER_FLAGS[m]
    if key in _NAV_KEYS:
        flags |= _NAV_FLAGS
    with _type_lock:
        for _ in range(times):
            for down in (True, False):
                ev = _CGEventCreateKeyboardEvent(None, code, down)
                _CGEventSetFlags(ev, flags)
                _CGEventPost(_HID_TAP, ev)
                _CFRelease(ev)
            time.sleep(0.02)


def press_return():
    press_key("return")


def type_text(text):
    """Type `text` into whatever has focus, a few characters per event.

    Apps cap how much of a synthetic event's string they read (20 UTF-16 units
    is safe everywhere), and some drop events that arrive with no gap at all.
    """
    data = text.encode("utf-16-le")
    units = [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]
    with _type_lock:
        i = 0
        while i < len(units):
            j = min(i + 16, len(units))
            # Never split a surrogate pair across two events.
            if j < len(units) and 0xD800 <= units[j - 1] <= 0xDBFF:
                j -= 1
            _post_unicode(units[i:j])
            i = j
            time.sleep(0.004)


if __name__ == "__main__":
    print("trusted:", is_trusted())
    time.sleep(2)
    print(focused())
