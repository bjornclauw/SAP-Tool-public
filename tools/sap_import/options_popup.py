"""Manage-values window for the Import grid's flat dropdown lists.

Only the flat columns (PG, WP, PRIOR, USTATUS, WO) live here. The WO_TYPE ->
WO_KEY hierarchy has its own master-detail editor (``wo_type_popup``), so the
two are not mixed into one control. The tree is preserved on save.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ui import ui_styles
from ui.ui_styles import (
    primary_button,
    styled_button,
    styled_entry,
    styled_frame,
    styled_label,
)
from ui.window_chrome import show_centered

from .options import (
    OPTION_KEYS,
    add_option,
    load_options,
    load_wo_tree,
    remove_option,
    rename_option,
    save_all,
)


class OptionsPopup(tk.Toplevel):
    """Add, rename and remove the flat options behind one constrained column."""

    def __init__(self, parent, key: str | None = None, on_close=None):
        super().__init__(parent)
        self.withdraw()
        self.title("Manage values")
        self.transient(parent)
        self.grab_set()
        self.config(bg=ui_styles.COLORS["bg"])
        self.resizable(False, False)
        self._on_close = on_close
        self._options = load_options()
        self._key = key if key in OPTION_KEYS else OPTION_KEYS[0]

        hdr = styled_frame(self)
        hdr.pack(fill="x", padx=12, pady=(12, 4))
        styled_label(hdr, "Dropdown values", font=ui_styles.FONTS["title"]).pack(anchor="w")
        styled_label(hdr, "These fill the grid's in-cell pickers. WO_TYPE / WO_KEY "
                          "are managed separately.",
                     font=(ui_styles.FONTS["body"][0], 7),
                     fg=ui_styles.COLORS["text_secondary"]).pack(anchor="w")

        pick = styled_frame(self)
        pick.pack(fill="x", padx=12, pady=(4, 8))
        styled_label(pick, "Column:", font=ui_styles.FONTS["body"]).pack(side="left")
        self._key_var = tk.StringVar(value=self._key)
        combo = ttk.Combobox(pick, textvariable=self._key_var, values=list(OPTION_KEYS),
                             state="readonly", width=18, font=ui_styles.FONTS["body"])
        combo.pack(side="left", padx=(6, 0))
        combo.bind("<<ComboboxSelected>>", lambda _e: self._reload())

        self._listbox = tk.Listbox(self, height=10, activestyle="none",
                                   bg=ui_styles.COLORS["surface"],
                                   fg=ui_styles.COLORS["text"],
                                   relief="solid", bd=1, highlightthickness=0)
        self._listbox.pack(fill="x", padx=12)

        entry_row = styled_frame(self)
        entry_row.pack(fill="x", padx=12, pady=(8, 0))
        self._entry_var = tk.StringVar()
        styled_entry(entry_row, textvariable=self._entry_var, width=24).pack(
            side="left", fill="x", expand=True)
        for text, cmd in (("Add", self._add), ("Rename", self._rename),
                          ("Remove", self._remove)):
            styled_button(entry_row, text, command=cmd, padx=10, pady=3,
                          cursor="hand2").pack(side="left", padx=(6, 0))

        btns = styled_frame(self)
        btns.pack(fill="x", padx=12, pady=12)
        primary_button(btns, "Save", command=self._save, padx=12, pady=4,
                       cursor="hand2").pack(side="left")
        styled_button(btns, "Cancel", command=self.destroy, padx=12, pady=4).pack(
            side="left", padx=8)

        self.geometry("400x340")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._reload()
        show_centered(self, ui_styles.SUPPORT_COLOR)

    def values(self) -> list[str]:
        return list(self._options.get(self._key_var.get(), []))

    def _reload(self):
        self._key = self._key_var.get()
        self._listbox.delete(0, "end")
        for value in self.values():
            self._listbox.insert("end", value)

    def _selected(self) -> str | None:
        sel = self._listbox.curselection()
        return self._listbox.get(sel[0]) if sel else None

    def _add(self):
        if add_option(self._options, self._key_var.get(), self._entry_var.get()):
            self._entry_var.set("")
            self._reload()

    def _rename(self):
        old = self._selected()
        if old is not None and rename_option(self._options, self._key_var.get(),
                                             old, self._entry_var.get()):
            self._entry_var.set("")
            self._reload()

    def _remove(self):
        old = self._selected()
        if old is not None and remove_option(self._options, self._key_var.get(), old):
            self._reload()

    def _save(self):
        save_all(self._options, load_wo_tree())
        if self._on_close:
            self._on_close()
        self.destroy()
