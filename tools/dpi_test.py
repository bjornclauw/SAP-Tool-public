"""Temporary DPI-mode test tool window.

The docked toolbar visibly rescales when a helper is dragged between monitors.
The working theory is that the process DPI mode is the trigger: a DPI-unaware
process is bitmap-scaled by Windows, while a per-monitor-aware process gets
real DPI changes. This window restarts the app in either mode so the same drag
can be compared. Remove it, its catalogue entry and ``data/dpi_test_mode.txt``
once the dock bug is fixed.
"""

from __future__ import annotations

import subprocess
import sys
import tkinter as tk

from core.updater import APP_ROOT
from ui import dpi, ui_styles
from ui.custom_widgets import BaseToolWindow
from ui.monitor import (
    all_monitors,
    process_awareness,
    system_dpi,
    virtual_screen,
    work_area_of,
)
from ui.ui_styles import styled_button, styled_frame, styled_label

_AWARENESS_NAMES = {0: "unaware", 1: "system", 2: "per-monitor"}


class DpiTestWindow(BaseToolWindow):
    def __init__(self, parent, engine):
        super().__init__(
            parent, engine,
            titlebar_color=ui_styles.TOOL_COLORS.get(
                "dpi_test", ui_styles.SUPPORT_COLOR))
        self.title("DPI Mode Test (temporary)")
        self.geometry("820x560")
        self.minsize(680, 460)
        self._build()
        self._build_status_bar()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._show()
        self._refresh()

    def _build(self):
        outer = styled_frame(self)
        outer.pack(fill="both", expand=True)

        intro = styled_frame(outer)
        intro.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                   pady=(ui_styles.SPACING["comfortable"],
                         ui_styles.SPACING["tight"]))
        styled_label(
            intro, "DPI mode test (temporary)",
            font=ui_styles.FONTS["title"],
        ).pack(anchor="w")
        styled_label(
            intro,
            "Restart the app in one mode, then drag PRH between monitors and "
            "watch the docked toolbar. Switch modes and repeat.",
            font=ui_styles.FONTS["body"],
            fg=ui_styles.COLORS["text_secondary"],
        ).pack(anchor="w", pady=(2, 0))
        self._mode_label = styled_label(
            intro, "",
            font=ui_styles.FONTS["body"],
        )
        self._mode_label.pack(anchor="w", pady=(6, 0))

        buttons = styled_frame(outer)
        buttons.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                     pady=(0, ui_styles.SPACING["tight"]))
        for text, command in (
            ("Restart: DPI-unaware", lambda: self._restart("unaware")),
            ("Restart: per-monitor V2", lambda: self._restart("aware2")),
            ("Refresh", self._refresh),
            ("Open Dock Diagnostic", self._open_dock_diag),
        ):
            styled_button(buttons, text, command=command).pack(
                side="left", padx=(0, ui_styles.SPACING["tight"]))
        styled_button(buttons, "Close", command=self.destroy).pack(side="right")

        text_frame = tk.Frame(
            outer, bg=ui_styles.COLORS["surface"],
            highlightbackground=ui_styles.COLORS["border"],
            highlightthickness=1,
        )
        text_frame.pack(fill="both", expand=True,
                        padx=ui_styles.SPACING["comfortable"],
                        pady=(0, ui_styles.SPACING["tight"]))
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

        styled_label(
            outer,
            "Per-monitor V2 is crisp but smaller on a scaled monitor: Tk 8.6 "
            "does not scale itself. The choice is saved in "
            "data/dpi_test_mode.txt and applies to the next launch.",
            font=ui_styles.FONTS["small"],
            fg=ui_styles.COLORS["text_secondary"],
        ).pack(anchor="w", padx=ui_styles.SPACING["comfortable"],
               pady=(0, ui_styles.SPACING["comfortable"]))

    # ---- actions --------------------------------------------------------
    def _restart(self, mode: str):
        dpi.write_startup_mode(mode)
        try:
            subprocess.Popen(
                [sys.executable, str(APP_ROOT / "main.py")],
                cwd=str(APP_ROOT),
            )
        except OSError as exc:
            self.report_status(f"Could not restart: {exc}", "error")
            return
        self.report_status(f"Restarting in {mode} mode", "info")
        self.after(200, self._close_app)

    def _close_app(self):
        closer = getattr(self.master, "_on_close", None)
        if closer is not None:
            closer()
        else:
            self.master.destroy()

    def _open_dock_diag(self):
        opener = getattr(self.master, "_open_dock_diag", None)
        if opener is not None:
            opener()

    # ---- details --------------------------------------------------------
    def _refresh(self):
        mode = dpi.read_startup_mode()
        awareness = process_awareness()
        self._mode_label.config(
            text=(
                f"Next launch: {mode}   |   current process: "
                f"{awareness} ({_AWARENESS_NAMES.get(awareness, '?')})"
            )
        )
        self._text.config(state="normal")
        self._text.delete("1.0", "end")
        self._text.insert("end", "\n".join(self._details()))
        self._text.config(state="disabled")

    def _details(self) -> list[str]:
        lines = [
            f"startup mode (data/dpi_test_mode.txt) = {dpi.read_startup_mode()}",
            f"process awareness = {process_awareness()} "
            f"({_AWARENESS_NAMES.get(process_awareness(), '?')})",
            f"system dpi = {system_dpi()}   "
            f"tk scaling = {self.tk.call('tk', 'scaling')}",
            f"tk screen = {self.winfo_screenwidth()}x"
            f"{self.winfo_screenheight()}   virtual = {virtual_screen()}",
            "",
            "Monitors as this process sees them:",
        ]
        for info in all_monitors():
            lines.append(
                f"  monitor={info['monitor']} work={info['work']} "
                f"primary={info['primary']} dpi={info['dpi']}"
            )
        lines += [
            "",
            f"toolbar monitor work area = {work_area_of(self.master)}",
            f"this window work area     = {work_area_of(self)}",
            "",
            "An unaware process is always told 96 DPI: Windows bitmap-scales",
            "it on a monitor with a different scale. A per-monitor-aware",
            "process sees the real DPI and gets real DPI changes.",
        ]
        return lines
