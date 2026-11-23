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

The ctypes prototypes live here and nowhere else. Several modules query
monitors; a second module setting its own ``GetMonitorInfoW.argtypes`` with
a different struct class makes every other call fail and silently fall back
to the primary screen. Use the helpers below instead.

Never raises: off Windows (or on any error) it falls back to the
primary screen dims at origin.
"""

from __future__ import annotations

import ctypes
import sys

_MONITOR_DEFAULTTONEAREST = 2
_MDT_EFFECTIVE_DPI = 0


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


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


if sys.platform == "win32":
    _user32 = ctypes.windll.user32
    _shcore = ctypes.windll.shcore
    _user32.MonitorFromWindow.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32]
    _user32.MonitorFromWindow.restype = ctypes.c_void_p
    _user32.MonitorFromPoint.argtypes = [_Point, ctypes.c_uint32]
    _user32.MonitorFromPoint.restype = ctypes.c_void_p
    _user32.GetMonitorInfoW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_MonitorInfo)]
    _user32.GetMonitorInfoW.restype = ctypes.c_int
    _user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
    _user32.GetDpiForWindow.restype = ctypes.c_uint
    _user32.GetDpiForSystem.restype = ctypes.c_uint
    _shcore.GetDpiForMonitor.argtypes = [
        ctypes.c_void_p, ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
    _shcore.GetDpiForMonitor.restype = ctypes.c_long
    _shcore.GetProcessDpiAwareness.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    _shcore.GetProcessDpiAwareness.restype = ctypes.c_long
else:
    _user32 = None
    _shcore = None


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
    if _user32 is None:
        return fallback
    try:
        area = _windows_work_area(widget)
        return area if area is not None else fallback
    except Exception:
        return fallback


def process_awareness() -> int:
    """PROCESS_DPI_AWARENESS (0 unaware, 1 system, 2 per-monitor)."""
    if _shcore is None:
        return 0
    try:
        value = ctypes.c_int()
        _shcore.GetProcessDpiAwareness(None, ctypes.byref(value))
        return value.value
    except Exception:
        return 0


def _dpi_scale(awareness: int, dpi: int) -> float:
    """Physical-to-Tk scaling, only when Tk is stuck at 96 DPI."""
    if awareness == 2:
        return (dpi or 96) / 96.0
    return 1.0


def dpi_for_window(hwnd) -> int:
    if _user32 is None:
        return 0
    try:
        return int(_user32.GetDpiForWindow(hwnd)) or 0
    except Exception:
        return 0


def system_dpi() -> int:
    if _user32 is None:
        return 0
    try:
        return int(_user32.GetDpiForSystem()) or 0
    except Exception:
        return 0


def monitor_of_window(hwnd) -> int:
    if _user32 is None:
        return 0
    try:
        return int(_user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)) or 0
    except Exception:
        return 0


def monitor_info(hmon) -> dict | None:
    """Raw monitor rectangle/work/DPI/primary flag for an HMONITOR."""
    if _user32 is None or not hmon:
        return None
    try:
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if not _user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return None
        dpi = 0
        try:
            dx = ctypes.c_uint()
            dy = ctypes.c_uint()
            _shcore.GetDpiForMonitor(
                hmon, _MDT_EFFECTIVE_DPI, ctypes.byref(dx), ctypes.byref(dy))
            dpi = int(dx.value)
        except Exception:
            pass
        return {
            "handle": int(hmon),
            "monitor": (
                info.rcMonitor.left, info.rcMonitor.top,
                info.rcMonitor.right, info.rcMonitor.bottom,
            ),
            "work": (
                info.rcWork.left, info.rcWork.top,
                info.rcWork.right, info.rcWork.bottom,
            ),
            "primary": bool(info.dwFlags & 1),
            "dpi": dpi,
        }
    except Exception:
        return None


def monitor_info_of_window(hwnd) -> dict | None:
    return monitor_info(monitor_of_window(hwnd))


def all_monitors() -> list[dict]:
    """Every monitor, found by sampling the virtual-screen rectangle."""
    if _user32 is None:
        return []
    try:
        vx = _user32.GetSystemMetrics(76)
        vy = _user32.GetSystemMetrics(77)
        vw = _user32.GetSystemMetrics(78)
        vh = _user32.GetSystemMetrics(79)
    except Exception:
        return []
    found: dict[int, dict] = {}
    for y in range(vy, vy + vh, 250):
        for x in range(vx, vx + vw, 250):
            hmon = _user32.MonitorFromPoint(_Point(x, y), 0)
            if hmon and hmon not in found:
                info = monitor_info(hmon)
                if info is not None:
                    found[hmon] = info
    return list(found.values())


def virtual_screen() -> tuple[int, int, int, int]:
    if _user32 is None:
        return (0, 0, 1920, 1080)
    try:
        return (
            _user32.GetSystemMetrics(76), _user32.GetSystemMetrics(77),
            _user32.GetSystemMetrics(78), _user32.GetSystemMetrics(79),
        )
    except Exception:
        return (0, 0, 1920, 1080)


def _windows_work_area(widget) -> tuple[int, int, int, int] | None:
    """ctypes route: MonitorFromWindow + GetMonitorInfo + per-window DPI."""
    hwnd = widget.winfo_id()
    info = monitor_info_of_window(hwnd)
    if info is None:
        return None
    dpi = dpi_for_window(hwnd) or 96
    scale = _dpi_scale(process_awareness(), dpi)
    left, top, right, bottom = info["work"]
    return (
        int(left / scale),
        int(top / scale),
        int((right - left) / scale),
        int((bottom - top) / scale),
    )
