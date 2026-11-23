"""Per-monitor work-area lookup -- stdlib ctypes, no new dependency.

tkinter's winfo_screenwidth/height describe the PRIMARY monitor only, so
anything positioned from them (dock centering, new-window placement)
lands wrong on multi-monitor setups. This resolves the work area
(monitor minus taskbar) of the monitor nearest a widget.

DPI note: only a per-monitor-aware process sees raw physical monitor
rectangles while Tk 8.6 keeps laying out in its 96-DPI space, so only
that mode divides by the window's DPI. An unaware process gets 96 from
GetDpiForWindow and a system-aware process gets virtualized, already
scaled rectangles; dividing those would shrink the work area.

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


def _process_awareness() -> int:
    """PROCESS_DPI_AWARENESS (0 unaware, 1 system, 2 per-monitor)."""
    try:
        value = ctypes.c_int()
        ctypes.windll.shcore.GetProcessDpiAwareness(
            None, ctypes.byref(value))
        return value.value
    except Exception:
        return 0


def _dpi_scale(awareness: int, dpi: int) -> float:
    """Physical-to-Tk scaling, only when Tk is stuck at 96 DPI."""
    if awareness == 2:
        return (dpi or 96) / 96.0
    return 1.0


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
    scale = _dpi_scale(_process_awareness(), dpi)
    left, top = mi.rcWork.left, mi.rcWork.top
    return (
        int(left / scale),
        int(top / scale),
        int((mi.rcWork.right - left) / scale),
        int((mi.rcWork.bottom - top) / scale),
    )
