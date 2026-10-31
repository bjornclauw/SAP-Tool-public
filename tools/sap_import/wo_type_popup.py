"""Manage WO types: a master-detail editor for the WO_TYPE -> WO_KEY tree.

Left pane: the WO_TYPEs (parents). Right pane: the WO_KEYs (children) of the
selected type. This follows the standard list-detail pattern for a one-to-many
relationship, so the hierarchy is not mixed into the flat dropdown lists.
"""

from __future__ import annotations

import tkinter as tk
from types import SimpleNamespace

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
    add_option,
    add_wo_type,
    load_options,
    load_wo_tree,
    remove_option,
    remove_wo_type,
    rename_option,
    rename_wo_type,
    save_all,
)


class WoTypePopup(tk.Toplevel):
    """Add, rename and remove WO_TYPEs and their WO_KEY children."""

    def __init__(self, parent, on_close=None):
        super().__init__(parent)
        self.withdraw()
        self.title("Manage WO types")
        self.transient(parent)
        self.grab_set()
        self.config(bg=ui_styles.COLORS["bg"])
        self.resizable(False, False)
        self._on_close = on_close
        self._tree = load_wo_tree()

        hdr = styled_frame(self)
        hdr.pack(fill="x", padx=12, pady=(12, 4))
        styled_label(hdr, "WO_TYPE \u2192 WO_KEY", font=ui_styles.FONTS["title"]).pack(anchor="w")
        styled_label(hdr, "Pick a WO_TYPE on the left; its WO_KEYs are on the "
                          "right.", font=(ui_styles.FONTS["body"][0], 7),
                     fg=ui_styles.COLORS["text_secondary"]).pack(anchor="w")

        panes = styled_frame(self)
        panes.pack(fill="both", expand=True, padx=12, pady=8)
        self._left_pane = self._build_pane(
            panes, "WO_TYPE", self._add_type, self._rename_type, self._remove_type)
        self._left_pane.frame.pack(side="left", fill="both", expand=True)
        # Selecting a WO_TYPE filters the children list on the right.
        self._left_pane.listbox.bind("<<ListboxSelect>>",
                                     lambda _e: self._on_type_selected())
        self._right_pane = self._build_pane(
            panes, "WO_KEY", self._add_key, self._rename_key, self._remove_key)
        self._right_pane.frame.pack(side="left", fill="both", expand=True, padx=(10, 0))

        btns = styled_frame(self)
        btns.pack(fill="x", padx=12, pady=(0, 12))
        primary_button(btns, "Save", command=self._save, padx=12, pady=4,
                       cursor="hand2").pack(side="left")
        styled_button(btns, "Cancel", command=self.destroy, padx=12, pady=4).pack(
            side="left", padx=8)

        self.geometry("520x380")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._reload_types()
        show_centered(self, ui_styles.SUPPORT_COLOR)

    # ---- panes ------------------------------------------------------------
    def _build_pane(self, parent, title, on_add, on_rename, on_remove):
        pane = styled_frame(parent)
        styled_label(pane, title, font=ui_styles.FONTS["header"]).pack(anchor="w")
        listbox = tk.Listbox(pane, height=10, activestyle="none",
                             bg=ui_styles.COLORS["surface"],
                             fg=ui_styles.COLORS["text"], relief="solid", bd=1,
                             highlightthickness=0, exportselection=False)
        listbox.pack(fill="both", expand=True, pady=(2, 6))
        entry_var = tk.StringVar()
        styled_entry(pane, textvariable=entry_var, width=16).pack(fill="x")
        buttons = styled_frame(pane)
        buttons.pack(fill="x", pady=(6, 0))
        for text, cmd in (("Add", on_add), ("Rename", on_rename), ("Remove", on_remove)):
            styled_button(buttons, text, command=cmd, padx=8, pady=3,
                          cursor="hand2").pack(side="left", padx=(0, 6))
        return SimpleNamespace(frame=pane, listbox=listbox, entry_var=entry_var)

    # ---- types (left) -----------------------------------------------------
    def _selected_type(self) -> str | None:
        sel = self._left_pane.listbox.curselection()
        return self._left_pane.listbox.get(sel[0]) if sel else None

    def _reload_types(self, select: str | None = None):
        self._left_pane.listbox.delete(0, "end")
        for name in self._tree:
            self._left_pane.listbox.insert("end", name)
        names = list(self._tree)
        if not names:
            self._reload_keys()
            return
        if select not in names:
            select = names[0]
        idx = names.index(select)
        self._left_pane.listbox.selection_clear(0, "end")
        self._left_pane.listbox.selection_set(idx)
        self._reload_keys()

    def _add_type(self):
        name = self._left_pane.entry_var.get().strip()
        if add_wo_type(self._tree, name):
            self._left_pane.entry_var.set("")
            self._reload_types(select=name)

    def _rename_type(self):
        old = self._selected_type()
        if old is None:
            return
        new = self._left_pane.entry_var.get().strip()
        if rename_wo_type(self._tree, old, new):
            self._left_pane.entry_var.set("")
            self._reload_types(select=new)

    def _remove_type(self):
        old = self._selected_type()
        if old is not None and remove_wo_type(self._tree, old):
            self._reload_types()

    # ---- keys (right) -----------------------------------------------------
    def _on_type_selected(self):
        """A WO_TYPE was picked: show only that type's WO_KEY children."""
        self._reload_keys()

    def _reload_keys(self):
        self._right_pane.listbox.delete(0, "end")
        for key in self._children():
            self._right_pane.listbox.insert("end", key)

    def _children(self) -> list[str]:
        name = self._selected_type()
        return list(self._tree.get(name, [])) if name is not None else []

    def _selected_key(self) -> str | None:
        sel = self._right_pane.listbox.curselection()
        return self._right_pane.listbox.get(sel[0]) if sel else None

    def _add_key(self):
        name = self._selected_type()
        if name is not None and add_option(self._tree, name,
                                           self._right_pane.entry_var.get()):
            self._right_pane.entry_var.set("")
            self._reload_keys()

    def _rename_key(self):
        name = self._selected_type()
        old = self._selected_key()
        if name is not None and old is not None and rename_option(
                self._tree, name, old, self._right_pane.entry_var.get()):
            self._right_pane.entry_var.set("")
            self._reload_keys()

    def _remove_key(self):
        name = self._selected_type()
        old = self._selected_key()
        if name is not None and old is not None and remove_option(self._tree, name, old):
            self._reload_keys()

    # ---- save -------------------------------------------------------------
    def _save(self):
        save_all(load_options(), self._tree)
        if self._on_close:
            self._on_close()
        self.destroy()
