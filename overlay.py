#!/usr/bin/env python3
"""Launch the island overlay for whichever desktop this is.

The two implementations have nothing in common below the surface - Win32 clips
a Tk window to a rounded region, macOS draws into a transparent NSWindow - so
they live in separate modules and this picks one. Importing either on the wrong
platform fails (tkinter's win32 calls, or AppKit), which is why the import is
inside the branch.
"""

import os
import sys


def build():
    if os.name == "nt":
        from overlay_win import Island
        return Island()
    if sys.platform == "darwin":
        from overlay_mac import Island
        return Island()
    raise RuntimeError(
        "the island overlay needs Windows or macOS; on Linux use --3d instead")


if __name__ == "__main__":
    try:
        build().run()
    except RuntimeError as e:
        sys.exit(f"  {e}")
