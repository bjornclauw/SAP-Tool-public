"""GridColumnManager -- single owner of grid column layout + configuration.

Replaces GridManager and the scattered column-config code in CustomTreeview
and tool windows. One instance binds to a ttk.Treeview and owns:
- stored rows + available columns (from SAP export)
- column visibility/order/display-name application + autosize + scrollbar
- the right-click "Configure Columns..." modal and its save->apply path
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable
from tkinter import ttk

from core import config as cfg
from tools.sap_export_parser import SapExportParser
from ui.edit_modal import EditModal


class GridColumnManager:
    status_label: tk.Label | None = None

    def __init__(self, treeview: ttk.Treeview, config_key: str, config_data: dict,
                 bind_right_click: bool = True, manage_scrollbar: bool = True):
        self.treeview = treeview
        self.config_key = config_key
        self.config_data = config_data
        self.available_columns: list[str] = []
        self._stored_rows: list[list[str]] = []
        self._parser = SapExportParser()
        self.last_warnings: list[str] = []
        self._apply_warnings: list[str] = []
        self.post_apply_callback: Callable | None = None
        self._scrollbar_packed = False
        self._manage_scrollbar = manage_scrollbar

        treeview._grid_manager = self  # type: ignore[attr-defined]
        if manage_scrollbar:
            self._h_scrollbar = ttk.Scrollbar(
                treeview.master, orient="horizontal", command=treeview.xview
            )
            treeview.configure(xscrollcommand=self._h_scrollbar.set)
        else:
            self._h_scrollbar = None

        if bind_right_click:
            # Right-click opens the column configuration modal.
            treeview.bind("<Button-3>", self._on_right_click)

    # ---- scrollbar -----------------------------------------------------
    def _ensure_scrollbar_packed(self):
        if self._scrollbar_packed or self._h_scrollbar is None:
            return
        try:
            self._h_scrollbar.pack(fill="x", padx=12, pady=(0, 4))
            self._scrollbar_packed = True
        except tk.TclError:
            self.treeview.after_idle(self._ensure_scrollbar_packed)

    @staticmethod
    def _heading_text(col_name: str, display_names: dict) -> str:
        return display_names.get(col_name, col_name)

    @staticmethod
    def _norm(name: str) -> str:
        """Header identity: case- and whitespace-insensitive."""
        return " ".join(str(name).split()).casefold()

    def _find_header(self, all_headers: list[str], name: str) -> str | None:
        if name in all_headers:
            return name
        target = self._norm(name)
        for header in all_headers:
            if self._norm(header) == target:
                return header
        return None

    # ---- population ----------------------------------------------------
    def populate_from_export(self, filepath: str, require_nonempty: str | None = None) -> None:
        self.last_warnings = []
        try:
            all_headers, data_rows, parse_warnings = self._parser.parse_with_warnings(filepath)
            key_header = None
            if require_nonempty:
                key_header = self._find_header(all_headers, require_nonempty)
                if key_header is None:
                    raise ValueError(
                        f"Export is missing the '{require_nonempty}' column")
        except (FileNotFoundError, ValueError) as e:
            self.available_columns = []
            self._stored_rows = []
            self.last_warnings = [str(e)]
            self.treeview.delete(*self.treeview.get_children())
            if self.status_label is not None:
                self.status_label.config(text=str(e))
            return

        if key_header is not None:
            key_idx = all_headers.index(key_header)
            data_rows = [r for r in data_rows if key_idx < len(r) and r[key_idx].strip()]

        self.available_columns = all_headers
        self._stored_rows = data_rows

        if data_rows:
            self._apply_columns(all_headers)
        else:
            self._apply_warnings = []
            self.treeview.delete(*self.treeview.get_children())

        self.last_warnings = list(parse_warnings) + list(self._apply_warnings)
        if self.status_label is not None:
            if data_rows:
                message = f"Loaded {len(data_rows)} rows from export"
            else:
                message = "Loaded 0 rows from export"
            if self.last_warnings:
                message += " — " + "; ".join(self.last_warnings[:4])
            self.status_label.config(text=message)

    # ---- apply ---------------------------------------------------------
    def add_row(self, values: dict) -> bool:
        """Append one row to the current export table.

        ``values`` is keyed by live or configured column name; unknown keys
        are ignored. Used to show a just-created ATB without re-running the
        SAP export. Returns False when no lookup has supplied columns yet.
        """
        if not self.available_columns:
            return False

        key_header = self._find_header(self.available_columns, "ATB")
        key_value = ""
        if key_header is not None:
            key_value = str(
                next(
                    (v for k, v in values.items()
                     if self._norm(k) == self._norm(key_header)),
                    "",
                )
            )
            if key_value:
                key_idx = self.available_columns.index(key_header)
                if any(
                    key_idx < len(row) and row[key_idx] == key_value
                    for row in self._stored_rows
                ):
                    return False

        norm_to_live = {self._norm(h): h for h in self.available_columns}
        display_names = cfg.get_grid_cfg(
            self.config_data, self.config_key
        ).get("column_display_names") or {}
        for name, label in display_names.items():
            live = norm_to_live.get(self._norm(name))
            if live is not None:
                norm_to_live.setdefault(self._norm(label), live)

        row = dict.fromkeys(self.available_columns, "")
        for name, value in values.items():
            live = norm_to_live.get(self._norm(name))
            if live is not None:
                row[live] = "" if value is None else str(value)

        self._stored_rows.append([row[h] for h in self.available_columns])
        self._apply_columns(self.available_columns)
        return True

    def _apply_columns(self, all_headers: list[str]) -> None:
        if not self._stored_rows or not all_headers:
            return

        grid_cfg = cfg.get_grid_cfg(self.config_data, self.config_key)
        configured_visible = grid_cfg.get("visible_columns", [])
        configured_order = grid_cfg.get("column_order", [])
        display_names = grid_cfg.get("column_display_names", {})
        self._apply_warnings = []

        # Resolve every saved column to a live header by identity, not by
        # position: exact name first, then case/whitespace-insensitive, then
        # the configured display name (a free alias table).
        live_set = set(all_headers)
        norm_to_live: dict[str, str] = {}
        for header in all_headers:
            key = self._norm(header)
            if key in norm_to_live:
                self._apply_warnings.append(
                    f"'{norm_to_live[key]}' and '{header}' look like the same column")
            else:
                norm_to_live[key] = header

        def resolve(name: str) -> str | None:
            if name in live_set:
                return name
            for candidate in (name, display_names.get(name)):
                if not candidate:
                    continue
                live = norm_to_live.get(self._norm(candidate))
                if live is not None:
                    return live
            return None

        resolved_visible: list[str] = []
        missing: list[str] = []
        for name in configured_visible:
            if name.startswith("_spacer_"):
                continue
            live = resolve(name)
            if live is None:
                missing.append(name)
            elif live not in resolved_visible:
                resolved_visible.append(live)
        if missing:
            self._apply_warnings.append(
                "missing columns: " + ", ".join(missing))

        resolved_order: list[str] = []
        for name in configured_order:
            live = resolve(name)
            if live is not None and live not in resolved_order:
                resolved_order.append(live)

        visible_set = set(resolved_visible)
        saved_order_set = set(resolved_order)

        if configured_visible and configured_order:
            ordered_visible: list[str] = []
            for header in resolved_order:
                if header in visible_set and header not in ordered_visible:
                    ordered_visible.append(header)
            for header in all_headers:
                if header.startswith("_spacer_") or header in ordered_visible:
                    continue
                if header in visible_set or header not in saved_order_set:
                    ordered_visible.append(header)
        else:
            ordered_visible = list(all_headers)

        resolved_display: dict[str, str] = {}
        for name, label in display_names.items():
            live = resolve(name)
            if live is not None and live not in resolved_display:
                resolved_display[live] = label
        for header in all_headers:
            resolved_display.setdefault(header, header)

        self.treeview["columns"] = ordered_visible
        self.treeview["show"] = "headings"
        for header in ordered_visible:
            is_spacer = header.startswith("_spacer_")
            self.treeview.heading(
                header,
                text="" if is_spacer else self._heading_text(header, resolved_display),
            )
            self.treeview.column(
                header, width=0 if is_spacer else 120, minwidth=0, anchor="w", stretch=False
            )

        header_to_idx = {h: i for i, h in enumerate(all_headers)}
        self.treeview.delete(*self.treeview.get_children())
        for row in self._stored_rows:
            ordered_vals = [
                row[header_to_idx[h]] if h in header_to_idx and header_to_idx[h] < len(row) else ""
                for h in ordered_visible
            ]
            self.treeview.insert("", "end", values=ordered_vals)

        self._auto_size_columns(ordered_visible, resolved_display, all_headers)
        self._ensure_scrollbar_packed()

        if self.post_apply_callback:
            self.post_apply_callback()

    def _auto_size_columns(self, ordered_visible, display_names, all_headers):
        font = tkfont.Font(font=("Segoe UI", 9))
        padding = 18
        min_w, max_w_limit = 40, 380
        header_to_idx = {h: i for i, h in enumerate(all_headers)}
        sample = self._stored_rows[:300]
        for header in ordered_visible:
            if header.startswith("_spacer_"):
                continue
            max_w = font.measure(display_names.get(header, header))
            idx = header_to_idx.get(header, -1)
            if idx >= 0:
                for row in sample:
                    if idx < len(row) and row[idx]:
                        w = font.measure(row[idx])
                        if w > max_w:
                            max_w = w
            width = min(max(max_w + padding, min_w), max_w_limit)
            self.treeview.column(header, width=width, minwidth=0, stretch=False)

    def clear(self) -> None:
        """Forget stored export data (e.g. after a failed lookup) so stale
        rows can't be mistaken for a fresh result. Leaves the treeview
        itself alone -- the caller owns what it shows."""
        self.available_columns = []
        self._stored_rows = []

    def reapply_config(self) -> None:
        if self._stored_rows:
            self._apply_columns(self.available_columns)

    def apply_config(self) -> None:
        grid_cfg = cfg.get_grid_cfg(self.config_data, self.config_key)
        configured_visible = grid_cfg.get("visible_columns", [])
        configured_order = grid_cfg.get("column_order", [])
        display_names = grid_cfg.get("column_display_names", {})

        if not configured_visible or not configured_order:
            self.treeview["columns"] = []
            self._ensure_scrollbar_packed()
            return

        visible_set = set(configured_visible)
        ordered_visible: list[str] = []
        for c in configured_order:
            if c in visible_set and c not in ordered_visible:
                ordered_visible.append(c)

        self.treeview["columns"] = ordered_visible
        self.treeview["show"] = "headings"
        for header in ordered_visible:
            is_spacer = header.startswith("_spacer_")
            self.treeview.heading(
                header, text="" if is_spacer else self._heading_text(header, display_names)
            )
            self.treeview.column(header, width=0 if is_spacer else 120, minwidth=0, anchor="w", stretch=False)
        self.treeview.delete(*self.treeview.get_children())
        self._ensure_scrollbar_packed()

    # ---- configure modal -----------------------------------------------
    def configure(self, on_apply=None) -> None:
        self._open_column_config(on_apply=on_apply)

    def _on_right_click(self, event):
        if not self._has_columns():
            return
        menu = tk.Menu(self.treeview, tearoff=0)
        menu.add_command(label="Configure Columns...", command=self.configure)
        menu.post(event.x_root, event.y_root)

    def _has_columns(self) -> bool:
        if self.available_columns:
            return True
        grid_cfg = cfg.get_grid_cfg(self.config_data, self.config_key)
        configured_order = grid_cfg.get("column_order", [])
        configured_visible = grid_cfg.get("visible_columns", [])
        return bool(configured_visible and configured_order)

    def _open_column_config(self, on_apply=None):
        if self.available_columns:
            available_cols = [c for c in self.available_columns if not c.startswith("_spacer_")]
        else:
            grid_cfg = cfg.get_grid_cfg(self.config_data, self.config_key)
            configured_order = grid_cfg.get("column_order", [])
            if configured_order:
                available_cols = [c for c in configured_order if not c.startswith("_spacer_")]
            else:
                if self.status_label is not None:
                    self.status_label.config(text="No columns available to configure")
                return

        if not available_cols:
            if self.status_label is not None:
                self.status_label.config(text="No columns available to configure")
            return

        grid_cfg = cfg.get_grid_cfg(self.config_data, self.config_key)
        visible_cols = grid_cfg.get("visible_columns", available_cols[:])
        col_order = grid_cfg.get("column_order", available_cols[:])
        display_names = grid_cfg.get("column_display_names", {})

        if not visible_cols:
            visible_cols = available_cols[:]
        if not col_order:
            col_order = available_cols[:]

        current_data = {
            "visible_columns": visible_cols,
            "column_order": col_order,
            "column_display_names": display_names,
        }

        parent = self.treeview.winfo_toplevel()

        def save_callback(new_data: dict):
            grid_cfg2 = cfg.get_grid_cfg(self.config_data, self.config_key)
            grid_cfg2.update(new_data)
            cfg.save_config(self.config_data)
            if on_apply is not None:
                on_apply()
            elif self._stored_rows:
                self.reapply_config()
            else:
                self.apply_config()

        modal = EditModal(
            parent,  # type: ignore[arg-type]
            "columnconfig",
            current_data,
            save_callback,
            title=self.config_key,
            available_columns=available_cols,
        )
        modal.transient(parent)
        modal.grab_set()
