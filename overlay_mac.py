#!/usr/bin/env python3
"""The macOS island overlay.

The Windows version clips a Tk window to a rounded region with SetWindowRgn,
because Win32 has no real per-window transparency to lean on. macOS does, so
this is a plain borderless panel with a clear background and the pill drawn
into it - no region surgery, and the rounded edge is antialiased.

Details the Windows code has no equivalent for:

  - Cocoa measures windows from the bottom-left, so the "10 px from the top"
    placement has to be converted rather than used directly.
  - The top-centre of a modern Mac screen is the menu bar, and on a MacBook it
    is also the notch - a window placed there is simply not visible. The gap
    between the screen frame and its visible frame gives both at once, so the
    island sits just below whatever is up there instead of behind it.
  - Clicking an ordinary window activates its app, which would pull keyboard
    focus out of the text field voice typing is aimed at. The island is a
    non-activating panel, so VOICE can be clicked without moving focus.

The island is also where voice lives (voice.py). Its shapes, from quiet to busy:

  idle        a grey dash
  hearing     a small grey waveform - the mic is live for somebody (the Chrome
              extension's wake word, say), but nothing is being typed
  voice on    a wider pill: red dot, live waveform, and what you are saying
  hover       the full button row; VOICE toggles typing into the focused app
"""

import signal
import sys

from AppKit import (
    NSApplication, NSApplicationActivationPolicyAccessory, NSBackingStoreBuffered,
    NSBezierPath, NSCenterTextAlignment, NSColor, NSFont, NSFontAttributeName,
    NSForegroundColorAttributeName, NSLeftTextAlignment, NSLineBreakByTruncatingHead,
    NSMakeRect, NSMutableParagraphStyle, NSPanel, NSParagraphStyleAttributeName,
    NSScreen, NSStatusWindowLevel, NSTrackingActiveAlways, NSTrackingArea,
    NSTrackingInVisibleRect, NSTrackingMouseEnteredAndExited, NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary, NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSString, NSTimer

import ostext_mac
from voice import VoiceTyping

RED = "#ff453a"


def _color(spec, alpha=1.0):
    h = spec.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, alpha)


def _text(s, cx, cy, font, color):
    """Draw `s` centred on (cx, cy). The view is flipped, so y grows downward."""
    style = NSMutableParagraphStyle.alloc().init()
    style.setAlignment_(NSCenterTextAlignment)
    attrs = {
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: color,
        NSParagraphStyleAttributeName: style,
    }
    text = NSString.stringWithString_(s)
    size = text.sizeWithAttributes_(attrs)
    text.drawInRect_withAttributes_(
        NSMakeRect(cx - size.width / 2, cy - size.height / 2,
                   size.width, size.height),
        attrs,
    )


def _caption(s, x, cy, width, font, color):
    """Left-aligned, single line; long text keeps its end, which is the part
    you just said."""
    style = NSMutableParagraphStyle.alloc().init()
    style.setAlignment_(NSLeftTextAlignment)
    style.setLineBreakMode_(NSLineBreakByTruncatingHead)
    attrs = {
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: color,
        NSParagraphStyleAttributeName: style,
    }
    height = font.ascender() - font.descender() + 2
    NSString.stringWithString_(s).drawInRect_withAttributes_(
        NSMakeRect(x, cy - height / 2, width, height), attrs)


def _waves(levels, x, cy, width, max_h, color, bars):
    """Rounded bars, newest on the right, each a smoothed slice of level history."""
    if bars <= 0 or width <= 0:
        return
    step = width / bars
    bar_w = max(2.0, step * 0.55)
    recent = levels[-bars:]
    recent = [0.0] * (bars - len(recent)) + recent
    color.setFill()
    for i, level in enumerate(recent):
        prev = recent[i - 1] if i else level
        h = max(3.0, min(1.0, 0.65 * level + 0.35 * prev) * max_h)
        bx = x + i * step + (step - bar_w) / 2
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(bx, cy - h / 2, bar_w, h), bar_w / 2, bar_w / 2).fill()


class _DryRunTyper:
    """--dry-run: find focus and log what would be typed, but type nothing."""

    def __getattr__(self, name):
        return getattr(ostext_mac, name)

    def type_text(self, text):
        from voice import log
        log("dry-run, not typing", repr(text))

    def press_return(self):
        from voice import log
        log("dry-run, not pressing return")


class IslandPanel(NSPanel):
    def canBecomeKeyWindow(self):
        # Never take keyboard focus - it belongs to the app being typed into.
        return False

    def canBecomeMainWindow(self):
        return False


class IslandView(NSView):
    """Draws the pill. Owns no state - the Island next door has all of it."""

    def isFlipped(self):
        # Match the Windows drawing code, which measures y from the top.
        return True

    def acceptsFirstMouse_(self, event):
        return True

    def viewDidMoveToWindow(self):
        # InVisibleRect keeps the tracking area correct as the pill resizes,
        # which it does on every animation frame.
        self.addTrackingArea_(NSTrackingArea.alloc()
            .initWithRect_options_owner_userInfo_(
                self.bounds(),
                NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways
                | NSTrackingInVisibleRect,
                self, None))

    def _path(self):
        b = self.bounds()
        radius = b.size.height / 2.0
        return NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            b, radius, radius)

    def hitTest_(self, point):
        # Only the pill itself is clickable; the four corner slivers outside it
        # belong to whatever is underneath.
        local = self.convertPoint_fromView_(point, self.superview())
        return self if self._path().containsPoint_(local) else None

    def mouseEntered_(self, event):
        self.island.set_open(True)

    def mouseExited_(self, event):
        self.island.set_open(False)

    def mouseDown_(self, event):
        p = self.convertPoint_fromView_(event.locationInWindow(), None)
        self.island.clicked(p.x, p.y)

    def drawRect_(self, rect):
        _color("#000000").setFill()
        self._path().fill()
        self.island.draw(self.bounds())


class Island:
    COMPACT = (68.0, 24.0)
    HEARING = (104.0, 24.0)
    LIVE = (340.0, 40.0)
    OPEN = (390.0, 58.0)
    TOP = 10.0
    # Button row geometry, shared with the Windows build.
    VOICE_X = 48

    def __init__(self):
        self.app = NSApplication.sharedApplication()
        # Accessory: no Dock icon, and it never steals focus from what you are
        # actually working in - which is the whole point of an overlay.
        self.app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        self.width, self.height = self.COMPACT
        self.open = False
        self.snap = None

        self.voice = VoiceTyping(_DryRunTyper() if "--dry-run" in sys.argv else ostext_mac)

        frame = NSMakeRect(0, 0, *self.COMPACT)
        self.window = IslandPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(NSColor.clearColor())
        self.window.setLevel_(NSStatusWindowLevel)
        self.window.setHasShadow_(True)
        self.window.setHidesOnDeactivate_(False)
        self.window.setBecomesKeyOnlyIfNeeded_(True)
        self.window.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary)

        self.view = IslandView.alloc().initWithFrame_(frame)
        self.view.island = self
        self.window.setContentView_(self.view)
        self.place()
        self.window.orderFrontRegardless()

        # One clock for both the size animation and the waves.
        self.timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            1 / 30.0, True, self._tick)

    def place(self):
        screen = NSScreen.mainScreen()
        f, visible = screen.frame(), screen.visibleFrame()
        # Whatever the system keeps at the top - menu bar, notch, or both.
        gap = f.size.height - (visible.origin.y + visible.size.height)
        w, h = round(self.width), round(self.height)
        self.window.setFrame_display_(
            NSMakeRect(f.origin.x + (f.size.width - w) / 2,
                       f.origin.y + f.size.height - h - (gap + self.TOP),
                       w, h),
            True)

    def set_open(self, is_open):
        self.open = is_open

    def clicked(self, x, y):
        if not self.open:
            return
        cy = self.height / 2
        if abs(x - self.VOICE_X) <= 22 and abs(y - cy) <= 22:
            self.voice.toggle()

    def _target(self, snap):
        if self.open:
            return self.OPEN
        if snap["active"] or snap["tone"] == "notice":
            return self.LIVE
        if snap["hearing"]:
            return self.HEARING
        return self.COMPACT

    def _tick(self, timer):
        snap = self.voice.snapshot()
        tw, th = self._target(snap)
        moving = abs(tw - self.width) >= 0.5 or abs(th - self.height) >= 0.5
        if moving:
            self.width += (tw - self.width) * 0.4
            self.height += (th - self.height) * 0.4
            if abs(tw - self.width) < 0.5 and abs(th - self.height) < 0.5:
                self.width, self.height = tw, th
            self.place()
        animating = snap["hearing"] or snap["active"]
        if moving or animating or snap != self.snap:
            self.snap = snap
            self.view.setNeedsDisplay_(True)

    # -- drawing -------------------------------------------------------------

    def draw(self, bounds):
        snap = self.snap or self.voice.snapshot()
        width, height = bounds.size.width, bounds.size.height
        y = height / 2.0

        if self.open and width > 300:
            return self._draw_buttons(y, snap)

        if not snap["active"] and snap["tone"] == "notice" and width > 200:
            # Something went wrong or needs doing: say so, in words.
            _color("#ffd479").setFill()
            NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(16, y - 4, 8, 8)).fill()
            _caption(snap["caption"], 32, y, width - 32 - 18,
                     NSFont.systemFontOfSize_(12.0), _color("#ffd479"))
            return

        if snap["active"] and width > 200:
            # Red dot, waves, then what you are saying.
            _color(RED).setFill()
            NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(16, y - 4, 8, 8)).fill()
            _waves(snap["levels"], 32, y, 78, height - 14, _color("#f3f3f5"), 13)
            tone = snap["tone"]
            color = {"interim": "#f3f3f5", "typed": "#8ee29b", "notice": "#ffd479"}.get(tone, "#8e8e93")
            _caption(snap["caption"], 120, y, width - 120 - 20,
                     NSFont.systemFontOfSize_(12.0), _color(color))
            return

        if (snap["hearing"] or snap["active"]) and width > 80:
            _waves(snap["levels"], 18, y, width - 36, height - 10,
                   _color(RED if snap["active"] else "#8e8e93"), 12)
            return

        if width < 100:
            # The collapsed state is a single grey dash.
            line = NSBezierPath.bezierPath()
            line.setLineWidth_(4.0)
            line.setLineCapStyle_(1)                     # round
            line.moveToPoint_((width / 2 - 8, y))
            line.lineToPoint_((width / 2 + 8, y))
            _color("#343438").setStroke()
            line.stroke()

    def _draw_buttons(self, y, snap):
        icon_font = NSFont.systemFontOfSize_(13.0)
        label_font = NSFont.boldSystemFontOfSize_(8.0)
        fill, edge = _color("#1d1d20"), _color("#35353a")

        # Same geometry as the Windows build, so the two look like one product.
        for x, icon, label in ((self.VOICE_X, "●", "VOICE"), (102, "◯", "HEAD"),
                               (190, "⌨", "KEYBOARD"), (344, "■", "STOP")):
            if label == "KEYBOARD":
                shape = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(x - 48, y - 18, 96, 36), 18, 18)
                fill.setFill(); shape.fill()
                edge.setStroke(); shape.stroke()
                _text(icon, x - 29, y, icon_font, _color("#f3f3f5"))
                _text(label, x + 10, y, label_font, _color("#f3f3f5"))
            elif label == "VOICE":
                on = snap["active"]
                # The ring swells with your voice while typing is on.
                level = max(snap["levels"][-3:]) if on else 0.0
                r = 18 + 4 * level
                if on:
                    _color(RED, 0.25).setFill()
                    NSBezierPath.bezierPathWithOvalInRect_(
                        NSMakeRect(x - r, y - r, 2 * r, 2 * r)).fill()
                shape = NSBezierPath.bezierPathWithOvalInRect_(
                    NSMakeRect(x - 18, y - 18, 36, 36))
                (_color(RED) if on else fill).setFill(); shape.fill()
                (_color(RED) if on else edge).setStroke(); shape.stroke()
                _text(icon, x, y, NSFont.boldSystemFontOfSize_(13.0),
                      _color("#ffffff" if on else "#f3f3f5"))
            else:
                shape = NSBezierPath.bezierPathWithOvalInRect_(
                    NSMakeRect(x - 18, y - 18, 36, 36))
                fill.setFill(); shape.fill()
                edge.setStroke(); shape.stroke()
                _text(icon, x, y,
                      NSFont.boldSystemFontOfSize_(13.0),
                      _color("#ff8a8a" if label == "STOP" else "#f3f3f5"))

    def run(self):
        self.voice.start()
        if "--voice" in sys.argv:
            self.voice.set_active(True)
        # NSApp.run() never gives the interpreter a chance to raise
        # KeyboardInterrupt, so let the signals do their default job: the
        # tracker terminates this process on the way out. The hub notices its
        # stdin closing and exits with us.
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        self.app.run()


if __name__ == "__main__":
    Island().run()
