#!/usr/bin/env python3
"""The Windows island overlay (see overlay.py, which picks a platform).

The overlay is a native window because a web page cannot be both above other
desktop applications and click-through outside its visible contents. Its window
region is the black pill itself; there is no full-screen transparent window to
intercept clicks.
"""

import ctypes
import tkinter as tk
from ctypes import wintypes


class Island:
    COMPACT = (68, 24)
    OPEN = (390, 58)
    TOP = 10

    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#000000")
        self.canvas = tk.Canvas(self.root, bg="#000000", highlightthickness=0,
                                bd=0, cursor="arrow")
        self.canvas.pack(fill="both", expand=True)
        self.open = False
        self.width, self.height = self.COMPACT
        self.target = self.COMPACT
        self.canvas.bind("<Enter>", lambda _event: self.set_open(True))
        self.canvas.bind("<Leave>", lambda _event: self.set_open(False))
        self.place()
        self.draw()

    def place(self):
        x = (self.root.winfo_screenwidth() - round(self.width)) // 2
        self.root.geometry(f"{round(self.width)}x{round(self.height)}+{x}+{self.TOP}")
        self.root.update_idletasks()
        # Restrict hit-testing to the rounded black shape. Everything else on
        # the screen belongs to the application underneath this overlay.
        gdi32, user32 = ctypes.windll.gdi32, ctypes.windll.user32
        gdi32.CreateRoundRectRgn.argtypes = (ctypes.c_int,) * 6
        gdi32.CreateRoundRectRgn.restype = wintypes.HANDLE
        user32.SetWindowRgn.argtypes = (wintypes.HWND, wintypes.HANDLE, wintypes.BOOL)
        user32.SetWindowRgn.restype = ctypes.c_int
        region = gdi32.CreateRoundRectRgn(0, 0, round(self.width) + 1,
                                          round(self.height) + 1,
                                          round(self.height), round(self.height))
        user32.SetWindowRgn(self.root.winfo_id(), region, True)

    def set_open(self, open_):
        self.open = open_
        self.target = self.OPEN if open_ else self.COMPACT
        self.animate()

    def animate(self):
        tw, th = self.target
        self.width += (tw - self.width) * .26
        self.height += (th - self.height) * .26
        if abs(tw - self.width) < .5 and abs(th - self.height) < .5:
            self.width, self.height = self.target
        self.place()
        self.draw()
        if (self.width, self.height) != self.target:
            self.root.after(16, self.animate)

    def draw(self):
        self.canvas.delete("all")
        self.canvas.config(width=round(self.width), height=round(self.height))
        if not self.open and self.width < 100:
            x, y = self.width / 2, self.height / 2
            self.canvas.create_line(x - 8, y, x + 8, y, fill="#343438", width=4,
                                    capstyle="round")
            return

        y = self.height / 2
        # These are intentionally presentation-only controls. The overlay is
        # kept inert so it never changes tracker state or takes over input.
        controls = ((48, "●", "VOICE"), (102, "◯", "HEAD"),
                    (190, "⌨", "KEYBOARD"), (344, "■", "STOP"))
        for x, icon, label in controls:
            if label == "KEYBOARD":
                self.canvas.create_rounded_rect(x - 48, y - 18, x + 48, y + 18,
                                                18, fill="#1d1d20", outline="#35353a")
                self.canvas.create_text(x - 29, y, text=icon, fill="#f3f3f5",
                                        font=("Segoe UI Symbol", 13))
                self.canvas.create_text(x + 10, y, text=label, fill="#f3f3f5",
                                        font=("Segoe UI", 8, "bold"))
            else:
                self.canvas.create_oval(x - 18, y - 18, x + 18, y + 18,
                                        fill="#1d1d20", outline="#35353a")
                color = "#ff8a8a" if label == "STOP" else "#f3f3f5"
                self.canvas.create_text(x, y, text=icon, fill=color,
                                        font=("Segoe UI Symbol", 13, "bold"))

    def run(self):
        self.root.mainloop()


def _rounded_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    points = (x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
              x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
              x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1)
    return canvas.create_polygon(points, smooth=True, **kwargs)


tk.Canvas.create_rounded_rect = _rounded_rect

if __name__ == "__main__":
    Island().run()
