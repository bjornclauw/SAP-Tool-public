"""Week popup (L3-L7 checkboxes) + revisions register editor."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ui import ui_styles
from ui.ui_styles import styled_frame, styled_label
from ui.window_chrome import show_centered

from .revisions import (
    downtime_prefix,
    get_checkboxes,
    get_revisions,
    parse_week_key,
    set_checkboxes,
    set_revisions,
    week_key,
)


class WeekRevisionPopup(tk.Toplevel):
    """Small popup from toolbar weeknumber — shows week/year + L3-L7 checkboxes."""

    def __init__(self, parent):
        super().__init__(parent)
        self.withdraw()
        self.title("Week Revisions")
        self.transient(parent)
        self.grab_set()
        self.config(bg=ui_styles.COLORS["bg"])
        self.resizable(False, False)
        self._wk = week_key()
        yr, wn = parse_week_key(self._wk)
        self._yr = yr
        checks = get_checkboxes(self._wk)

        # header
        hdr = styled_frame(self)
        hdr.pack(fill="x", padx=12, pady=(12, 8))
        styled_label(hdr, f"Week {wn} — {yr}  ({self._wk})", font=ui_styles.FONTS["title"]).pack(anchor="w")
        styled_label(hdr, "Generate revisions from levels:", font=ui_styles.FONTS["body"],
                     fg=ui_styles.COLORS["text_secondary"]).pack(anchor="w", pady=(4, 0))

        # checkboxes
        self.vars: dict[str, tk.BooleanVar] = {}
        cb_frame = styled_frame(self)
        cb_frame.pack(fill="x", padx=12, pady=4)
        for key, desc in [("L3", "L3"), ("L4", "L4"), ("L5", "L5"), ("L6", "L6"), ("L7", "L7")]:
            var = tk.BooleanVar(value=checks.get(key, False))
            self.vars[key] = var
            ttk.Checkbutton(cb_frame, text=f"{key} — {downtime_prefix(wn, yr)}{key}", variable=var).pack(anchor="w", pady=2)

        hint = tk.Label(self, text="L3+L4 → WWyyL3L4 single  •  L5+L6 → WWyyL5L6  •  L7 separate",
                        bg=ui_styles.COLORS["bg"], fg=ui_styles.COLORS["text_secondary"],
                        font=(ui_styles.FONTS["body"][0], 7))
        hint.pack(anchor="w", padx=12, pady=(0, 8))

        # preview
        self.preview_var = tk.StringVar(value=self._preview_text(wn, checks))
        tk.Label(self, textvariable=self.preview_var, bg=ui_styles.COLORS["bg"],
                 fg=ui_styles.COLORS["primary"], font=(ui_styles.FONTS["body"][0], 8, "bold")).pack(anchor="w", padx=12)
        for var in self.vars.values():
            var.trace_add("write", lambda *_: self.preview_var.set(self._preview_text(wn, {k: v.get() for k, v in self.vars.items()})))

        # buttons
        btns = styled_frame(self)
        btns.pack(fill="x", padx=12, pady=12)
        tk.Button(btns, text="Save & Generate", command=self._save,
                  bg=ui_styles.COLORS["primary"], fg="white", relief="solid", bd=1, padx=12, pady=4, cursor="hand2").pack(side="left")
        tk.Button(btns, text="Cancel", command=self.destroy,
                  bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"], relief="solid", bd=1, padx=12, pady=4).pack(side="left", padx=8)
        tk.Button(btns, text="Edit register…", command=lambda: (self.destroy(), RevisionsRegisterPopup(parent, week=self._wk)),
                  bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["primary"], relief="solid", bd=1, padx=12, pady=4).pack(side="right")

        self.geometry("360x320")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        show_centered(self, ui_styles.SUPPORT_COLOR)

    def _preview_text(self, wn: int, checks: dict) -> str:
        from .revisions import generate_from_checkboxes
        revs = generate_from_checkboxes(wn, checks, year=self._yr)
        return "Preview: " + (", ".join(revs) if revs else "(none — check L3-L7)")

    def _save(self):
        checks = {k: v.get() for k, v in self.vars.items()}
        set_checkboxes(checks, self._wk)
        self.destroy()


class RevisionsRegisterPopup(tk.Toplevel):
    """Editable register for one week — override generated revisions (Tools menu)."""

    def __init__(self, parent, week: str | None = None):
        super().__init__(parent)
        self.withdraw()
        self.title("Revisions Register")
        self.transient(parent)
        self.grab_set()
        self.config(bg=ui_styles.COLORS["bg"])
        self.resizable(False, False)
        self._wk = week or week_key()
        yr, wn = parse_week_key(self._wk)
        revs = get_revisions(self._wk)
        # pad to 5 rows for editing
        revs = (revs + [""] * 5)[:5]

        hdr = styled_frame(self)
        hdr.pack(fill="x", padx=12, pady=(12, 4))
        styled_label(hdr, f"Register {self._wk} — Week {wn} {yr}", font=ui_styles.FONTS["title"]).pack(anchor="w")
        styled_label(hdr, "MAX 5 — edit overrides; new week auto-clears (new W-key).",
                     font=(ui_styles.FONTS["body"][0], 7), fg=ui_styles.COLORS["text_secondary"]).pack(anchor="w")

        self.entries: list[tk.StringVar] = []
        form = styled_frame(self)
        form.pack(fill="x", padx=12, pady=8)
        for i in range(5):
            row = styled_frame(form)
            row.pack(fill="x", pady=2)
            styled_label(row, f"{i+1}.", width=3, anchor="w").pack(side="left")
            var = tk.StringVar(value=revs[i])
            self.entries.append(var)
            tk.Entry(row, textvariable=var, width=24, bg="white", fg="#212529",
                     relief="solid", bd=1).pack(side="left", fill="x", expand=True, padx=(4, 0))

        btns = styled_frame(self)
        btns.pack(fill="x", padx=12, pady=12)
        tk.Button(btns, text="Save", command=self._save,
                  bg=ui_styles.COLORS["primary"], fg="white", relief="solid", bd=1, padx=12, pady=4).pack(side="left")
        tk.Button(btns, text="Cancel", command=self.destroy,
                  bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"], relief="solid", bd=1, padx=12, pady=4).pack(side="left", padx=8)
        tk.Button(btns, text="Clear", command=self._clear,
                  bg="#e74c3c", fg="white", relief="solid", bd=1, padx=12, pady=4).pack(side="right")
        self.geometry("380x320")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        show_centered(self, ui_styles.SUPPORT_COLOR)

    def _save(self):
        vals = [v.get().strip() for v in self.entries]
        set_revisions(vals, self._wk)
        self.destroy()

    def _clear(self):
        for v in self.entries:
            v.set("")
