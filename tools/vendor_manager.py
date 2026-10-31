"""VendorManagerWindow -- CRUD for the PRH vendor list.

The vendor list is the tab-separated `private/suppliers.txt` dump (see
tools/supplier_lookup.py for the format). This window edits that same file
directly -- no overlay, no second source of truth -- so whatever is saved
here is what the PRH vendor combobox offers after the change callback fires.

Layout: live search on top, vendor tree in the middle (capped at 500 shown
rows -- the file holds 14k lines and a full rebuild per keystroke would
stutter), Name/Code/City form + Add/Update/Delete below, status bar last.

Opened modeless from the PRH Tools menu (single instance, lifts when open)
so a code can be copied over from SAP while this window stays up. Parent
PRH reloads its combobox through on_changed after every mutation.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, ttk

from tools.supplier_lookup import (
    CITY_COL,
    CODE_COL,
    NAME_COL,
    read_table,
    validate_vendor,
    write_table,
)
from ui import ui_styles
from ui.custom_widgets import BaseToolWindow
from ui.ui_styles import styled_frame, styled_label

_MAX_SHOWN = 500


class VendorManagerWindow(BaseToolWindow):
    """CRUD window over private/suppliers.txt. Pack itself, modeless."""

    def __init__(self, parent, on_changed: Callable[[], None] | None = None):
        super().__init__(parent, parent.engine,
                         titlebar_color=ui_styles.TOOL_COLORS["vendor_manager"])
        self.title("Vendors")
        self.geometry("720x560")
        self.minsize(620, 440)
        self.transient(parent)

        self._on_changed = on_changed
        self._newline, self._head, self._rows = read_table()
        self._shown: list[int] = []  # table row indexes currently in the tree

        self._build_search()
        self._build_tree()
        self._build_form()
        self._build_status_bar()

        self._refresh_tree()
        self._show()

    # ---- layout ------------------------------------------------------
    def _build_search(self):
        bar = styled_frame(self)
        bar.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                 pady=(ui_styles.SPACING["comfortable"], ui_styles.SPACING["tight"]))
        styled_label(bar, "Search:").pack(side="left", padx=(0, 4))
        self._search_var = tk.StringVar()
        search = tk.Entry(bar, textvariable=self._search_var,
                          font=ui_styles.FONTS["body"], width=30)
        search.pack(side="left", fill="x", expand=True)
        search.bind("<KeyRelease>", lambda e: self._refresh_tree())
        self._count_label = styled_label(bar, "", fg=ui_styles.COLORS["text_secondary"])
        self._count_label.pack(side="left", padx=(8, 0))

    def _build_tree(self):
        style = ttk.Style()
        style.configure(
            "VendorTreeview.Treeview",
            font=ui_styles.FONTS["body"],
            background=ui_styles.COLORS["surface"],
            fieldbackground=ui_styles.COLORS["surface"],
        )
        body = styled_frame(self)
        body.pack(fill="both", expand=True,
                  padx=ui_styles.SPACING["comfortable"], pady=0)
        self.tree = ttk.Treeview(
            body,
            columns=("name", "code", "city", "note"),
            show="headings",
            style="VendorTreeview.Treeview",
        )
        self.tree.heading("name", text="Name")
        self.tree.heading("code", text="Code")
        self.tree.heading("city", text="City")
        self.tree.heading("note", text="Picker note")
        self.tree.column("name", width=240, minwidth=140, stretch=True)
        self.tree.column("code", width=90, minwidth=70, stretch=False)
        self.tree.column("city", width=150, minwidth=100, stretch=True)
        # The note explains rows the PRH picker can never show (bad code,
        # empty name) -- otherwise CRUD and picker silently disagree about
        # the count. Duplicate names carry no note: they ARE pickable via
        # their city suffix.
        self.tree.column("note", width=170, minwidth=80, stretch=False)
        vscroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        hscroll = ttk.Scrollbar(body, orient="horizontal", command=self.tree.xview)
        vscroll.pack(side="right", fill="y")
        hscroll.pack(side="bottom", fill="x")
        self.tree.pack(side="top", fill="both", expand=True)
        self.tree.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_row_selected)

    def _build_form(self):
        form = styled_frame(self)
        form.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                  pady=ui_styles.SPACING["normal"])

        self._name_var = tk.StringVar()
        self._code_var = tk.StringVar()
        self._city_var = tk.StringVar()

        styled_label(form, "Name:").grid(row=0, column=0, sticky="e", padx=(0, 4))
        tk.Entry(form, textvariable=self._name_var,
                 font=ui_styles.FONTS["body"], width=34).grid(
            row=0, column=1, sticky="ew", padx=(0, 12))
        styled_label(form, "Code:").grid(row=0, column=2, sticky="e", padx=(0, 4))
        tk.Entry(form, textvariable=self._code_var,
                 font=ui_styles.FONTS["body"], width=10).grid(row=0, column=3, sticky="w")
        styled_label(form, "City:").grid(row=1, column=0, sticky="e", padx=(0, 4),
                                        pady=(ui_styles.SPACING["tight"], 0))
        tk.Entry(form, textvariable=self._city_var,
                 font=ui_styles.FONTS["body"], width=34).grid(
            row=1, column=1, sticky="ew", padx=(0, 12),
            pady=(ui_styles.SPACING["tight"], 0))
        form.grid_columnconfigure(1, weight=1)

        btns = styled_frame(form)
        btns.grid(row=2, column=0, columnspan=4, sticky="w",
                  pady=(ui_styles.SPACING["normal"], 0))
        ui_styles.styled_button(btns, "Add", width=10,
                                command=self._on_add).pack(side="left", padx=(0, 6))
        ui_styles.styled_button(btns, "Update", width=10,
                                command=self._on_update).pack(side="left", padx=(0, 6))
        ui_styles.styled_button(btns, "Delete", width=10,
                                command=self._on_delete).pack(side="left", padx=(0, 6))
        ui_styles.styled_button(btns, "Clear", width=10,
                                command=self._clear_form).pack(side="left")

    # ---- view --------------------------------------------------------
    def _refresh_tree(self, select_row: int | None = None):
        """Rebuild the tree from the table, applying the search filter.

        Capped at _MAX_SHOWN rows: the file holds ~14k lines and the PRH
        combo narrows by typing anyway -- nobody scrolls fourteen thousand
        vendors, and an uncapped rebuild per keystroke stutters.
        """
        self.tree.delete(*self.tree.get_children())
        self._shown = []
        q = (self._search_var.get() or "").strip().lower()
        total = 0
        hidden = 0
        for idx, parts in enumerate(self._rows):
            name = parts[NAME_COL].strip() if len(parts) > NAME_COL else ""
            code = parts[CODE_COL].strip() if len(parts) > CODE_COL else ""
            city = parts[CITY_COL].strip() if len(parts) > CITY_COL else ""
            if not name:
                note = "empty name -- hidden from picker"
            elif validate_vendor("ok", code) is not None:
                note = "bad code -- hidden from picker"
            else:
                note = ""
            if q and q not in name.lower() and q not in code.lower() and q not in city.lower():
                continue
            total += 1
            if note:
                hidden += 1
            if len(self._shown) >= _MAX_SHOWN:
                continue
            iid = str(idx)
            self.tree.insert("", "end", iid=iid, values=(name, code, city, note))
            self._shown.append(idx)
        extra = f" (+{total - len(self._shown)} more -- refine search)" if total > len(self._shown) else ""
        hid = f", {hidden} hidden from picker" if hidden else ""
        self._count_label.config(text=f"{total} vendor(s){hid}{extra}")
        if select_row is not None and str(select_row) in {str(i) for i in self._shown}:
            self.tree.selection_set(str(select_row))
            self.tree.see(str(select_row))

    # ---- events ------------------------------------------------------
    def _selected_row(self) -> int | None:
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except ValueError:
            return None

    def _on_row_selected(self, event=None):
        idx = self._selected_row()
        if idx is None or idx >= len(self._rows):
            return
        parts = self._rows[idx]
        self._name_var.set(parts[NAME_COL].strip() if len(parts) > NAME_COL else "")
        self._code_var.set(parts[CODE_COL].strip() if len(parts) > CODE_COL else "")
        self._city_var.set(parts[CITY_COL].strip() if len(parts) > CITY_COL else "")

    def _clear_form(self):
        self.tree.selection_remove(self.tree.selection())
        self._name_var.set("")
        self._code_var.set("")
        self._city_var.set("")

    def _save(self):
        write_table(self._newline, self._head, self._rows)
        if self._on_changed is not None:
            self._on_changed()

    def _duplicate_of(self, name: str, skip: int | None = None) -> bool:
        want = name.strip().lower()
        return any(
            i != skip and len(r) > NAME_COL and r[NAME_COL].strip().lower() == want
            for i, r in enumerate(self._rows)
        )

    def _on_add(self):
        name = self._name_var.get().strip()
        code = self._code_var.get().strip()
        err = validate_vendor(name, code)
        if err:
            self.report_status(err, "error")
            return
        if self._duplicate_of(name):
            self.report_status(f'"{name}" already exists -- select it to Update', "warning")
            return
        city = self._city_var.get().strip()
        self._rows.append(["", "", "", "", city, name, code])
        self._save()
        self._refresh_tree(select_row=len(self._rows) - 1)
        self.report_status(f'Vendor "{name}" ({code}) added', "success")

    def _on_update(self):
        idx = self._selected_row()
        if idx is None:
            self.report_status("Select a vendor first", "warning")
            return
        name = self._name_var.get().strip()
        code = self._code_var.get().strip()
        err = validate_vendor(name, code)
        if err:
            self.report_status(err, "error")
            return
        if self._duplicate_of(name, skip=idx):
            self.report_status(f'"{name}" already exists -- pick another name', "warning")
            return
        parts = self._rows[idx]
        cur = (
            parts[CITY_COL].strip() if len(parts) > CITY_COL else "",
            parts[NAME_COL].strip() if len(parts) > NAME_COL else "",
            parts[CODE_COL].strip() if len(parts) > CODE_COL else "",
        )
        new = (self._city_var.get().strip(), name, code)
        if cur == new:
            self.report_status("No changes -- nothing saved", "info")
            return
        while len(parts) <= CODE_COL:
            parts.append("")
        parts[CITY_COL] = self._city_var.get().strip()
        parts[NAME_COL] = name
        parts[CODE_COL] = code
        self._save()
        self._refresh_tree(select_row=idx)
        self.report_status(f'Vendor "{name}" ({code}) updated', "success")

    def _on_delete(self):
        idx = self._selected_row()
        if idx is None:
            self.report_status("Select a vendor first", "warning")
            return
        parts = self._rows[idx]
        name = parts[NAME_COL].strip() if len(parts) > NAME_COL else f"row {idx}"
        if not messagebox.askyesno("Delete vendor",
                                    f'Delete "{name}" from the vendor list?',
                                    parent=self):
            return
        del self._rows[idx]
        self._save()
        self._clear_form()
        self._refresh_tree()
        self.report_status(f'Vendor "{name}" deleted', "success")
