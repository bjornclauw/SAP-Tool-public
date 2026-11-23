"""One DPI mode for the whole process, chosen before the first Tk window.

Tk 8.6 has no per-monitor DPI support. If a tool window or one of its
libraries switches the process to per-monitor awareness, Windows sends DPI
changes for monitor moves and the whole app rescales -- a helper dragged to
another screen can resize the docked toolbar. Keep the process DPI-unaware by
default: Windows then bitmap-scales the app instead of changing window sizes.

The temporary DPI Test tool window can restart the app in per-monitor V2 to
check whether a mixed-DPI desk behaves better that way. It records the choice
in ``data/dpi_test_mode.txt``; delete that file to return to the default.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

DPI_UNAWARE = 0
DPI_SYSTEM = 1
DPI_PER_MONITOR = 2
_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

# Temporary: written by the DPI Test tool window, read at startup.
TEST_MODE_FILE = Path(__file__).resolve().parent.parent / "data" / "dpi_test_mode.txt"

_AWARE_MODES = ("aware", "aware2", "per-monitor", "per_monitor", "permonitor")


def _normalise_mode(mode: str) -> str:
    return "aware2" if str(mode or "").strip().lower() in _AWARE_MODES else "unaware"


def read_startup_mode(path: Path | None = None) -> str:
    """The DPI mode the next launch should use; ``"unaware"`` when unset."""
    try:
        text = (path or TEST_MODE_FILE).read_text(encoding="utf-8")
    except OSError:
        return "unaware"
    return _normalise_mode(text)


def write_startup_mode(mode: str, path: Path | None = None) -> None:
    """Persist the mode used by the next launch (temporary test setting)."""
    target = path or TEST_MODE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_normalise_mode(mode), encoding="utf-8")


def clear_startup_mode(path: Path | None = None) -> None:
    """Remove the test setting, restoring the DPI-unaware default."""
    try:
        (path or TEST_MODE_FILE).unlink()
    except OSError:
        pass


def pin_dpi_mode(mode: str = "unaware") -> None:
    """Pin the process DPI mode. Must run before any Tk window exists.

    ``mode`` is ``"unaware"`` (default) or ``"aware2"``. A launcher manifest
    or compatibility override may already have chosen a mode; Windows then
    refuses the change and the call is simply a no-op.
    """
    if sys.platform != "win32":
        return
    try:
        if _normalise_mode(mode) == "aware2":
            user32 = ctypes.windll.user32
            try:
                user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                user32.SetProcessDpiAwarenessContext.restype = ctypes.c_int
                if user32.SetProcessDpiAwarenessContext(
                        _PER_MONITOR_AWARE_V2):
                    return
            except Exception:
                pass
            ctypes.windll.shcore.SetProcessDpiAwareness(DPI_PER_MONITOR)
            return
        ctypes.windll.shcore.SetProcessDpiAwareness(DPI_UNAWARE)
    except Exception:
        pass


def pin_dpi_unaware() -> None:
    """Kept for the existing call sites and tests."""
    pin_dpi_mode("unaware")
