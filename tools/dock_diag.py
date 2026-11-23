"""Temporary dock diagnostic tool window.

Opened from the Helpers card while investigating a multi-monitor dock bug.
It samples the toolbar, the collapsed pill and the Purchase Request window
(geometry, monitor, work area, DPI, mapping state) and writes a log under
``data/``. Remove this window and its catalogue entry once the bug is fixed.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

from core.updater import APP_ROOT, read_version
from ui import ui_styles
from ui.custom_widgets import BaseToolWindow
from ui.monitor import work_area_of
from ui.ui_styles import styled_button, styled_frame, styled_label

SAMPLE_MS = 250
MAX_SECONDS = 180
MONITOR_STEP = 250


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_ulong),
    ]


_user32 = ctypes.windll.user32
_shcore = ctypes.windll.shcore
_user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
_user32.MonitorFromWindow.restype = ctypes.c_void_p
_user32.MonitorFromPoint.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
_user32.MonitorFromPoint.restype = ctypes.c_void_p
_user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)]
_user32.GetMonitorInfoW.restype = ctypes.c_int
_user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
_user32.GetDpiForWindow.restype = ctypes.c_uint
_user32.GetDpiForSystem.restype = ctypes.c_uint
_shcore.GetDpiForMonitor.argtypes = [
    ctypes.c_void_p, ctypes.c_int,
    ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
_shcore.GetDpiForMonitor.restype = ctypes.c_long
_shcore.GetProcessDpiAwareness.argtypes = [ctypes.c_void_p,
                                           ctypes.POINTER(ctypes.c_int)]
_shcore.GetProcessDpiAwareness.restype = ctypes.c_long


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_user32.MonitorFromPoint.argtypes = [_Point, ctypes.c_uint32]


def _awareness() -> int:
    try:
        value = ctypes.c_int()
        _shcore.GetProcessDpiAwareness(None, ctypes.byref(value))
        return value.value
    except Exception:
        return -1


def _monitor_info(hmon) -> str:
    if not hmon:
        return "none"
    try:
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not _user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return f"hmon={hmon} GetMonitorInfo failed"
        dx = ctypes.c_uint()
        dy = ctypes.c_uint()
        _shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy))
        return (
            f"hmon={hmon} monitor=({info.rcMonitor.left},{info.rcMonitor.top},"
            f"{info.rcMonitor.right},{info.rcMonitor.bottom}) "
            f"work=({info.rcWork.left},{info.rcWork.top},"
            f"{info.rcWork.right},{info.rcWork.bottom}) "
            f"primary={bool(info.dwFlags & 1)} dpi={dx.value}"
        )
    except Exception as exc:
        return f"hmon={hmon} error={exc}"


def _monitors() -> str:
    if sys.platform != "win32":
        return "non-Windows"
    try:
        vx = _user32.GetSystemMetrics(76)
        vy = _user32.GetSystemMetrics(77)
        vw = _user32.GetSystemMetrics(78)
        vh = _user32.GetSystemMetrics(79)
    except Exception as exc:
        return f"error={exc}"
    seen = {}
    for y in range(vy, vy + vh, MONITOR_STEP):
        for x in range(vx, vx + vw, MONITOR_STEP):
            hmon = _user32.MonitorFromPoint(_Point(x, y), 0)
            if hmon and hmon not in seen:
                seen[hmon] = _monitor_info(hmon)
    lines = [f"virtual=({vx},{vy},{vw},{vh})"]
    lines.extend(f"  {text}" for text in seen.values())
    return "\n".join(lines)


class DockDiagWindow(BaseToolWindow):
    def __init__(self, parent, engine):
        super().__init__(
            parent, engine,
            titlebar_color=ui_styles.TOOL_COLORS.get(
                "dock_diag", ui_styles.SUPPORT_COLOR))
        self.title("Dock Diagnostic (temporary)")
        self.geometry("980x640")
        self.minsize(760, 480)
        self._lines: list[str] = []
        self._sample_id = None
        self._started = 0.0
        self._log_path: Path | None = None
        self._text = None
        self._path_label = None
        self._build()
        self._build_status_bar()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._show()
        self.after(300, self._start)

    def _build(self):
        outer = styled_frame(self)
        outer.pack(fill="both", expand=True)

        intro = styled_frame(outer)
        intro.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                   pady=(ui_styles.SPACING["comfortable"],
                         ui_styles.SPACING["tight"]))
        styled_label(
            intro, "Temporary diagnostic",
            font=ui_styles.FONTS["title"],
        ).pack(anchor="w")
        styled_label(
            intro,
            "Reproduce the dock bug on your monitors, then press Stop. "
            "Copy the log or open the data folder and send the file.",
            font=ui_styles.FONTS["body"],
            fg=ui_styles.COLORS["text_secondary"],
        ).pack(anchor="w", pady=(2, 0))
        self._path_label = styled_label(
            intro, "Not recording",
            font=ui_styles.FONTS["small"],
            fg=ui_styles.COLORS["text_secondary"],
        )
        self._path_label.pack(anchor="w", pady=(4, 0))

        buttons = styled_frame(outer)
        buttons.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                     pady=(0, ui_styles.SPACING["tight"]))
        for text, command in (
            ("Start", self._start),
            ("Mark", self._mark),
            ("Stop", self._stop),
            ("Copy log", self._copy),
            ("Open data folder", self._open_folder),
        ):
            styled_button(buttons, text, command=command).pack(
                side="left", padx=(0, ui_styles.SPACING["tight"]))
        styled_button(buttons, "Close", command=self._close).pack(side="right")

        text_frame = tk.Frame(
            outer, bg=ui_styles.COLORS["surface"],
            highlightbackground=ui_styles.COLORS["border"],
            highlightthickness=1,
        )
        text_frame.pack(fill="both", expand=True,
                        padx=ui_styles.SPACING["comfortable"],
                        pady=(0, ui_styles.SPACING["comfortable"]))
        self._text = tk.Text(
            text_frame, wrap="none", font=ui_styles.FONTS["mono"],
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
            relief="flat", bd=0, highlightthickness=0,
            padx=6, pady=6, state="disabled", cursor="arrow",
        )
        vscroll = tk.Scrollbar(
            text_frame, orient="vertical", command=self._text.yview)
        self._text.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side="right", fill="y")
        self._text.pack(side="left", fill="both", expand=True)

    # ---- sampling -------------------------------------------------------
    def _start(self):
        if self._sample_id is not None:
            return
        if self._log_path is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._log_path = APP_ROOT / "data" / f"dock_diag_{stamp}.txt"
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._path_label.config(
                text=f"Recording to {self._log_path}")
            self._write(self._header())
        self._started = time.monotonic()
        self._write(f"=== START {datetime.now():%Y-%m-%d %H:%M:%S} ===")
        self._sample()
        self.report_status("Recording dock diagnostic", "info")
        self._sample_id = self.after(SAMPLE_MS, self._tick)

    def _sample(self):
        self._write(self._snapshot("sample"))

    def _tick(self):
        self._sample_id = None
        if time.monotonic() - self._started > MAX_SECONDS:
            self._stop()
            return
        self._sample()
        try:
            self._sample_id = self.after(SAMPLE_MS, self._tick)
        except tk.TclError:
            self._sample_id = None

    def _stop(self):
        if self._sample_id is not None:
            try:
                self.after_cancel(self._sample_id)
            except tk.TclError:
                pass
            self._sample_id = None
            self._write("=== STOP ===")
            self.report_status("Dock diagnostic stopped", "info")

    def _mark(self):
        self._write("\n=== MARK ===\n" + self._snapshot("mark"))

    def _copy(self):
        try:
            self.clipboard_clear()
            self.clipboard_append("\n".join(self._lines))
            self.report_status("Dock diagnostic copied", "success")
        except tk.TclError:
            pass

    def _open_folder(self):
        path = self._log_path.parent if self._log_path else APP_ROOT / "data"
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)
        except Exception as exc:
            self.report_status(f"Could not open data folder: {exc}", "error")

    def _close(self):
        self._stop()
        self.destroy()

    # ---- log ------------------------------------------------------------
    def _header(self) -> str:
        try:
            version = read_version(APP_ROOT)
        except Exception:
            version = "?"
        return "\n".join([
            f"Dock diagnostic  {datetime.now():%Y-%m-%d %H:%M:%S}",
            f"version={version} python={sys.version.split()[0]}",
            f"process_awareness={_awareness()} "
            f"system_dpi={_user32.GetDpiForSystem()} "
            f"tk_scaling={self.tk.call('tk', 'scaling')} "
            f"screen={self.winfo_screenwidth()}x{self.winfo_screenheight()}",
            "monitors:",
            _monitors(),
            "",
        ])

    def _write(self, text: str):
        self._lines.append(text)
        if self._log_path is not None:
            try:
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(text + "\n")
            except OSError as exc:
                self.report_status(f"Could not write log: {exc}", "error")
        if self._text is not None:
            try:
                self._text.config(state="normal")
                self._text.insert("end", text + "\n")
                line_count = int(self._text.index("end-1c").split(".")[0])
                if line_count > 600:
                    self._text.delete("1.0", "100.0")
                self._text.see("end")
                self._text.config(state="disabled")
            except tk.TclError:
                pass

    def _snapshot(self, label: str) -> str:
        root = self.master
        dock = getattr(root, "dock", None)
        pill = getattr(dock, "_peek", None) if dock is not None else None
        prh = None
        try:
            prh = root.tool_windows.get("purchase_request")
        except Exception:
            pass
        lines = [f"--- {label} {datetime.now():%H:%M:%S.%f} ---"]
        for name, window in (
            ("diag", self), ("root", root), ("pill", pill), ("prh", prh),
        ):
            lines.append(self._window_line(name, window))
        if dock is not None:
            try:
                lines.append(
                    "dock: enabled="
                    f"{bool(dock.dock_enabled.get())} "
                    f"autohide={bool(dock.autohide.get())} "
                    f"hidden={dock._is_hidden()} "
                    f"saved_geometry={dock._saved_geometry!r} "
                    f"saved_area={dock._saved_area!r}")
            except Exception as exc:
                lines.append(f"dock: error={exc}")
        try:
            lines.append(
                f"tk_scaling={root.tk.call('tk', 'scaling')} "
                f"screen={root.winfo_screenwidth()}x"
                f"{root.winfo_screenheight()} "
                f"awareness={_awareness()} "
                f"system_dpi={_user32.GetDpiForSystem()}")
        except Exception as exc:
            lines.append(f"scaling: error={exc}")
        return "\n".join(lines)

    def _window_line(self, name: str, window) -> str:
        if window is None:
            return f"{name}: none"
        try:
            if not window.winfo_exists():
                return f"{name}: destroyed"
            hwnd = window.winfo_id()
            monitor = _monitor_info(
                _user32.MonitorFromWindow(hwnd, 2))
            try:
                window_dpi = int(_user32.GetDpiForWindow(hwnd)) or 0
            except Exception:
                window_dpi = 0
            try:
                over = window.overrideredirect()
            except Exception:
                over = "-"
            return (
                f"{name}: geom={window.geometry()} "
                f"size={window.winfo_width()}x{window.winfo_height()} "
                f"req={window.winfo_reqwidth()}x{window.winfo_reqheight()} "
                f"at={window.winfo_rootx()},{window.winfo_rooty()} "
                f"mapped={bool(window.winfo_ismapped())} "
                f"visible={bool(window.winfo_viewable())} "
                f"overrideredirect={over} "
                f"hwnd={hwnd} window_dpi={window_dpi} "
                f"area={work_area_of(window)} "
                f"{monitor}")
        except Exception as exc:
            return f"{name}: error={exc}"
