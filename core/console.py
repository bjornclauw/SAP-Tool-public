"""Windows console management.

The app is launched via pythonw.exe (scripts/START.bat), so no terminal
window appears by default. This module backs the toolbar's "Debug Console"
checkbox: it can spawn a real console for engine-log debugging
(AllocConsole) and tear it back down (FreeConsole), keeping sys.stdout /
sys.stderr attached to whichever console exists so logging and print() never
write to a dead handle.

Every call degrades to a no-op on non-Windows platforms and when the Win32
API is unavailable, so importing this module is always safe.
"""

from __future__ import annotations

import os
import sys

_kernel32 = None
if sys.platform == "win32":
    try:
        import ctypes

        _kernel32 = ctypes.windll.kernel32
    except (AttributeError, OSError):
        _kernel32 = None


def has_console() -> bool:
    """True if the process is currently attached to a console window."""
    if _kernel32 is None:
        return False
    try:
        return bool(_kernel32.GetConsoleWindow())
    except Exception:
        return False


def _console_stream():
    """A text stream bound to the Windows console (CONOUT$)."""
    try:
        return open("CONOUT$", "w", encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        fd = os.open("CONOUT$", os.O_WRONLY)
        return os.fdopen(fd, "w", encoding="utf-8", errors="replace")


def show_console() -> bool:
    """Spawn a console window (or reuse the existing one) and point
    sys.stdout/stderr at it. Returns False if that fails."""
    if _kernel32 is None:
        return False
    if not has_console():
        if not _kernel32.AllocConsole():
            return False
    try:
        sys.stdout = _console_stream()
        sys.stderr = _console_stream()
    except (OSError, ValueError):
        return False
    return True


def hide_console() -> None:
    """Detach streams first (so nothing writes to a dead handle), then free
    the console window if we have one."""
    if _kernel32 is None:
        return
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    if has_console():
        _kernel32.FreeConsole()
