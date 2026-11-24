"""Per-monitor work-area lookup -- stdlib ctypes, no new dependency.

tkinter's winfo_screenwidth/height describe the PRIMARY monitor only, so
anything positioned from them (dock centering, new-window placement)
lands wrong on multi-monitor setups. This resolves the work area
(monitor minus taskbar) of the monitor nearest a widget.

DPI note: ctypes reports PHYSICAL pixels while Tk geometry uses LOGICAL
ones (at 150% scaling, 1920 physical == 1280 logical). The divisor is
the window's own DPI via GetDpiForWindow (/96), which is correct
per-monitor even with mixed-DPI setups.

Never raises: off Windows (or on any error) it falls back to the
primary screen dims at origin.
"""

from __future__ import annotations

import ctypes
import sys

_MONITOR_DEFAULTTONEAREST = 2


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _Rect),
        ("rcWork", _Rect),
        ("dwFlags", ctypes.c_ulong),
    ]


def work_area_of(widget) -> tuple[int, int, int, int]:
    """(x, y, w, h) work area of the monitor nearest `widget`.

    Tk-logical coordinates on the virtual screen: secondary monitors to
    the left/above the primary have NEGATIVE origins -- keep the sign,
    geometry() understands it. Never raises.
    """
    try:
        sw, sh = widget.winfo_screenwidth(), widget.winfo_screenheight()
    except Exception:
        return (0, 0, 1920, 1080)
    fallback = (0, 0, int(sw), int(sh))
    if sys.platform != "win32":
        return fallback
    try:
        area = _windows_work_area(widget)
        return area if area is not None else fallback
    except Exception:
        return fallback


def _windows_work_area(widget) -> tuple[int, int, int, int] | None:
    """ctypes route: MonitorFromWindow + GetMonitorInfo + per-window DPI."""
    user32 = ctypes.windll.user32
    hwnd = widget.winfo_id()
    hmon = user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
    if not hmon:
        return None
    mi = _MonitorInfo()
    mi.cbSize = ctypes.sizeof(_MonitorInfo)
    if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return None
    try:
        dpi = int(user32.GetDpiForWindow(hwnd)) or 96
    except Exception:
        dpi = 96
    scale = dpi / 96.0
    if not scale:
        scale = 1.0
    left, top = mi.rcWork.left, mi.rcWork.top
    return (
        int(left / scale),
        int(top / scale),
        int((mi.rcWork.right - left) / scale),
        int((mi.rcWork.bottom - top) / scale),
    )
