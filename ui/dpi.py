"""One DPI mode for the whole process, chosen before the first Tk window.

Tk 8.6 has no per-monitor DPI support. If a tool window or one of its
libraries switches the process to per-monitor awareness, Windows sends DPI
changes for monitor moves and the whole app rescales -- a helper dragged to
another screen can resize the docked toolbar. Keep the process DPI-unaware:
Windows then bitmap-scales the app instead of changing window sizes.
"""

from __future__ import annotations

import sys

DPI_UNAWARE = 0


def pin_dpi_unaware() -> None:
    """Pin the process to DPI-unaware. Must run before any Tk window exists.

    A launcher manifest or compatibility override may already have chosen a
    mode; Windows then refuses the change and the call is simply a no-op.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(DPI_UNAWARE)
    except Exception:
        pass
