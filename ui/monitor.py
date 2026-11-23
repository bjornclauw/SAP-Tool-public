"""Per-monitor work-area lookup -- stdlib ctypes, no new dependency.

tkinter's winfo_screenwidth/height describe the PRIMARY monitor only, so
anything positioned from them (dock centering, new-window placement)
lands wrong on multi-monitor setups. This resolves the work area
(monitor minus taskbar) of the monitor nearest a widget.

DPI note: GetMonitorInfo already returns rectangles in the process's own
coordinate space -- virtualized (96-DPI) for an unaware process, physical
pixels for a system- or per-monitor-aware one. Tk 8.6 uses that same space
for geometry, so the rectangle is used as-is; dividing by the monitor DPI
would shrink the work area and move windows to the wrong monitor.

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
from contextlib import contextmanager

_MONITOR_DEFAULTTONEAREST = 2
_MDT_EFFECTIVE_DPI = 0
_GA_ROOT = 2
_GWL_EXSTYLE = -20
_WS_EX_TOPMOST = 0x00000008
# DPI_AWARENESS_CONTEXT values: used for short read-only probes and to put the
# thread back in the process's 96-DPI context around window API calls.
_DPI_CONTEXT_UNAWARE = ctypes.c_void_p(-1)
_DPI_CONTEXT_PER_MONITOR_V2 = ctypes.c_void_p(-4)
_DPI_CONTEXT_NAMES = (
    (-1, "unaware"),
    (-2, "system"),
    (-3, "per-monitor"),
    (-4, "per-monitor-v2"),
    (-5, "unaware-gdiscaled"),
)


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
    _user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Rect)]
    _user32.GetWindowRect.restype = ctypes.c_int
    try:
        _user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        _user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        _user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        _user32.AreDpiAwarenessContextsEqual.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p]
        _user32.AreDpiAwarenessContextsEqual.restype = ctypes.c_int
    except AttributeError:
        pass  # pre-Windows 10 1703
    _user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _user32.GetAncestor.restype = ctypes.c_void_p
    try:
        _user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        _get_window_long = _user32.GetWindowLongPtrW
    except AttributeError:  # 32-bit Windows
        _user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _user32.GetWindowLongW.restype = ctypes.c_long
        _get_window_long = _user32.GetWindowLongW
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
    _get_window_long = None


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


def thread_awareness() -> str:
    """The calling thread's current DPI awareness context, as a name.

    Tk can leave the thread in the context of the last window that received a
    message, so this can differ from ``process_awareness()``.
    """
    if _user32 is None:
        return "unknown"
    try:
        context = _user32.GetThreadDpiAwarenessContext()
    except Exception:
        return "unknown"
    for value, name in _DPI_CONTEXT_NAMES:
        try:
            if _user32.AreDpiAwarenessContextsEqual(
                    context, ctypes.c_void_p(value)):
                return name
        except Exception:
            break
    return "other"


@contextmanager
def unaware_thread():
    """Run window API calls in the process's 96-DPI context.

    A message dispatched to a window can leave the thread in that window's
    DPI context. Geometry and z-order calls made afterwards then run in that
    context, which can reset the virtualized scale of an unaware window on a
    scaled monitor. Restores the previous context on exit.
    """
    if _user32 is None:
        yield
        return
    previous = None
    try:
        previous = _user32.SetThreadDpiAwarenessContext(_DPI_CONTEXT_UNAWARE)
    except Exception:
        pass
    try:
        yield
    finally:
        if previous:
            try:
                _user32.SetThreadDpiAwarenessContext(previous)
            except Exception:
                pass


def is_topmost(hwnd) -> bool:
    """Whether the window behind ``hwnd`` is in the topmost band.

    Tk's ``winfo_id`` is a child of the native top-level, so walk to the root
    first. Reading the ex-style avoids calling ``attributes("-topmost", True)``
    when it is already set: that SetWindowPos resets the virtualized scale of
    a DPI-unaware window on a scaled monitor.
    """
    if _user32 is None or _get_window_long is None or not hwnd:
        return False
    try:
        root = _user32.GetAncestor(hwnd, _GA_ROOT) or hwnd
        return bool(_get_window_long(root, _GWL_EXSTYLE) & _WS_EX_TOPMOST)
    except Exception:
        return False


def physical_window_rect(hwnd) -> tuple[int, int, int, int, int] | None:
    """(x, y, w, h, monitor_dpi) in real pixels, bypassing virtualization.

    An unaware process is always told 96 DPI and gets virtualized rectangles.
    This reads the real values from a temporary per-monitor-V2 thread context
    and restores the caller's context immediately. Read-only; returns None
    when unavailable.
    """
    if _user32 is None or not hwnd:
        return None
    try:
        previous = _user32.SetThreadDpiAwarenessContext(
            _DPI_CONTEXT_PER_MONITOR_V2)
    except Exception:
        return None
    try:
        rect = _Rect()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        info = monitor_info_of_window(hwnd)
        return (
            rect.left, rect.top, rect.right - rect.left,
            rect.bottom - rect.top, (info or {}).get("dpi", 0),
        )
    except Exception:
        return None
    finally:
        try:
            _user32.SetThreadDpiAwarenessContext(previous)
        except Exception:
            pass


def physical_monitor_dpi(hmon) -> int:
    """The monitor's real DPI, even from an unaware process."""
    if _user32 is None or not hmon:
        return 0
    try:
        previous = _user32.SetThreadDpiAwarenessContext(
            _DPI_CONTEXT_PER_MONITOR_V2)
    except Exception:
        return 0
    try:
        info = monitor_info(hmon)
        return (info or {}).get("dpi", 0)
    finally:
        try:
            _user32.SetThreadDpiAwarenessContext(previous)
        except Exception:
            pass


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
    left, top, right, bottom = info["work"]
    return (left, top, right - left, bottom - top)
