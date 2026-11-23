"""Temporary dock diagnostic tool window.

Opened from the Helpers card while investigating a multi-monitor dock bug.
It samples the toolbar, the collapsed pill and the Purchase Request window
(geometry, monitor, work area, DPI, mapping state) and writes a log under
``data/``. Remove this window and its catalogue entry once the bug is fixed.
"""

from __future__ import annotations

import os
import sys
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

from core.updater import APP_ROOT, read_version
from ui import ui_styles
from ui.custom_widgets import BaseToolWindow
from ui.monitor import (
    all_monitors,
    dpi_for_window,
    monitor_info_of_window,
    physical_monitor_dpi,
    physical_window_rect,
    process_awareness,
    system_dpi,
    thread_awareness,
    virtual_screen,
    work_area_of,
)
from ui.ui_styles import styled_button, styled_frame, styled_label

SAMPLE_MS = 250
MAX_SECONDS = 180


def _fmt_monitor(info: dict | None) -> str:
    if info is None:
        return "none"
    return (
        f"hmon={info['handle']} monitor={info['monitor']} "
        f"work={info['work']} primary={info['primary']} dpi={info['dpi']} "
        f"real_dpi={physical_monitor_dpi(info['handle'])}"
    )


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
        self._topmost_btn = None
        self._topmost_off = False
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
            ("Pin bar", self._pin_bar),
            ("Auto-hide", self._auto_hide),
        ):
            styled_button(buttons, text, command=command).pack(
                side="left", padx=(0, ui_styles.SPACING["tight"]))
        self._topmost_btn = styled_button(
            buttons, "Topmost off", command=self._toggle_topmost)
        self._topmost_btn.pack(
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

    def _pin_bar(self):
        dock = getattr(self.master, "dock", None)
        if dock is None or not dock.dock_enabled.get():
            self.report_status("Dock the toolbar first", "warning")
            return
        dock.autohide.set(False)
        dock.apply()
        dock._show_main(animate=False, focus=False)
        self.report_status("Dock bar pinned open", "info")

    def _auto_hide(self):
        dock = getattr(self.master, "dock", None)
        if dock is None:
            return
        dock.autohide.set(True)
        dock.apply()
        self.report_status("Dock auto-hide restored", "info")

    def _toggle_topmost(self):
        """Drop/restore the 2-second topmost watch while reproducing.

        The watch re-applies ``-topmost`` unconditionally; on a DPI-scaled
        monitor that SetWindowPos can reset the window's virtualized scale.
        Turning it off isolates that from the drag itself.
        """
        dock = getattr(self.master, "dock", None)
        if dock is None:
            return
        self._topmost_off = not self._topmost_off
        if self._topmost_off:
            dock._stop_topmost_watch()
            try:
                self.master.attributes("-topmost", False)
            except Exception:
                pass
            self._topmost_btn.configure(text="Topmost on")
            self._write("\n=== TOPMOST OFF ===")
            self.report_status("Topmost watch off - drag PRH now", "info")
        else:
            dock._start_topmost_watch()
            try:
                self.master.attributes("-topmost", True)
            except Exception:
                pass
            dock._assert_topmost()
            self._topmost_btn.configure(text="Topmost off")
            self._write("\n=== TOPMOST ON ===")
            self.report_status("Topmost watch restored", "info")
        self._sample()

    def _open_folder(self):
        path = self._log_path.parent if self._log_path else APP_ROOT / "data"
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)
        except Exception as exc:
            self.report_status(f"Could not open data folder: {exc}", "error")

    def _close(self):
        if self._topmost_off:
            dock = getattr(self.master, "dock", None)
            if dock is not None:
                dock._start_topmost_watch()
                dock._assert_topmost()
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
            f"process_awareness={process_awareness()} "
            f"thread_awareness={thread_awareness()} "
            f"system_dpi={system_dpi()} "
            f"tk_scaling={self.tk.call('tk', 'scaling')} "
            f"screen={self.winfo_screenwidth()}x{self.winfo_screenheight()}",
            f"virtual={virtual_screen()}",
            "monitors:",
            *[f"  {_fmt_monitor(info)}" for info in all_monitors()],
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
                    f"topmost={bool(self.master.attributes('-topmost'))} "
                    f"saved_geometry={dock._saved_geometry!r} "
                    f"saved_area={dock._saved_area!r}")
            except Exception as exc:
                lines.append(f"dock: error={exc}")
        try:
            lines.append(
                f"tk_scaling={root.tk.call('tk', 'scaling')} "
                f"screen={root.winfo_screenwidth()}x"
                f"{root.winfo_screenheight()} "
                f"awareness={process_awareness()} "
                f"thread={thread_awareness()} "
                f"system_dpi={system_dpi()}")
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
            monitor = _fmt_monitor(monitor_info_of_window(hwnd))
            window_dpi = dpi_for_window(hwnd)
            native = physical_window_rect(hwnd)
            phys = (
                "none" if native is None
                else f"{native[2]}x{native[3]}+{native[0]}+{native[1]} "
                     f"real_dpi={native[4]}"
            )
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
                f"phys={phys} area={work_area_of(window)} "
                f"{monitor}")
        except Exception as exc:
            return f"{name}: error={exc}"
