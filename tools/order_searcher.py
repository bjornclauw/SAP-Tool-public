"""OrderSearcherWindow — reusable IW38 order search popup.

Opens as a modal from PurchaseRequestHelperWindow.
Uses the shared engine variable system — when user double-clicks a row,
sets {{order}} on the engine, which updates the PRH order field via
the bound variable wiring.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from tkinter import ttk

from tkcalendar import DateEntry

from core import config as cfg
from tools.grid_column_manager import GridColumnManager as GridManager
from ui import ui_styles
from ui.custom_widgets import BaseToolWindow, CustomButton, CustomEntry
from ui.ui_styles import styled_frame, styled_label


class AutoClosingDateEntry(DateEntry):
    """DateEntry that closes its dropdown on focus loss or outside click."""

    def _on_dropdown_open(self):
        super()._on_dropdown_open()
        self.after(50, self._post_open)

    def _post_open(self):
        try:
            cal = self._top_cal
            if not cal:
                return
            cal.grab_release()
            root = self.winfo_toplevel()
            self._close_bind_id = root.bind(
                "<Button-1>", self._on_root_click, "+"
            )
        except AttributeError:
            pass

    def _on_root_click(self, event):
        try:
            cal = self._top_cal
            if cal and event.widget.winfo_toplevel() is not cal:
                root = self.winfo_toplevel()
                root.unbind("<Button-1>", self._close_bind_id)
                self._on_dropdown_close()
        except AttributeError:
            pass


class OrderSearcherWindow(BaseToolWindow):
    """Search work orders via IW38 and return selected order."""

    EXPORT_FILENAME = "sap_iw38_export.txt"

    DATE_FORMAT = "dd.MM.yyyy"

    # Engine key holding the results grid across close/reopen (JSON).
    # Memory-only: an app restart gets a fresh engine, so the grid resets.
    GRID_STATE_VAR = "os_grid_state"

    # Every filter field, in config-key form. The engine outlives this
    # window, so entered values survive a close/reopen within the session
    # (restored in _build_filters); only an app restart clears them.
    FILTER_FIELDS = (
        "os_date_from_var",
        "os_date_to_var",
        "os_ingrp_var",
        "os_status_var",
        "os_workplace_var",
        "os_revnr_var",
        "os_tplnr_var",
        "os_text_var",
        "os_order_type_var",
        "os_eqfnr_var",
    )
    # Info-section fields as (config_key, attribute) for restore + clear.
    INFO_FILTER_ATTRS = (
        ("os_ingrp_var", "ingrp_var"),
        ("os_status_var", "status_var"),
        ("os_workplace_var", "workplace_var"),
        ("os_revnr_var", "revnr_var"),
        ("os_tplnr_var", "tplnr_var"),
        ("os_text_var", "text_var"),
        ("os_order_type_var", "order_type_var"),
        ("os_eqfnr_var", "eqfnr_var"),
    )

    def __init__(self, parent, engine):
        super().__init__(parent, engine, titlebar_color=ui_styles.TOOL_COLORS["order_searcher"],
                         modal=True)
        self.title("Order Search")
        self.geometry("1200x580")
        self.minsize(900, 480)

        self._init_export_path()
        self._build_filters()
        self._build_status_bar()
        self._build_treeview()

        self._show()

    def _init_export_path(self):
        base = os.path.dirname(os.path.dirname(__file__))
        self._export_path = os.path.join(base, "tempexport")
        os.makedirs(self._export_path, exist_ok=True)
        self._export_filepath = os.path.join(self._export_path, self.EXPORT_FILENAME)
        escaped = self._export_path.replace("\\", "\\\\")
        self.engine.set_variable("os_export_path", escaped)
        self.engine.set_variable("os_export_file", self.EXPORT_FILENAME)

    def _snapshot_filter_state(self):
        """Engine values for every filter field, taken BEFORE the first
        bind_entry pushes config defaults over them. Missing key = never
        searched this session; "" = explicitly cleared."""
        snap = {}
        for key in self.FILTER_FIELDS:
            var_name = cfg.get_textinput_cfg(self.config_data, key).get("var_name", key)
            snap[key] = self.engine.variables.get(var_name)
        return snap

    def _restore_filter_var(self, config_key, tk_var):
        """Re-apply the session value after binding (bind_entry reset it
        to the config default). First open in the session keeps the
        config default -- nothing to do."""
        prev = self._filter_snapshot.get(config_key)
        if prev is not None:
            tk_var.set(prev)

    def _build_filters(self):
        # Snapshot BEFORE any bind_entry: binding pushes the config
        # default into the engine, wiping the session value. Nothing
        # survives an app restart: the engine dies with the toolbar.
        self._filter_snapshot = self._snapshot_filter_state()

        outer = styled_frame(self)
        outer.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                   pady=(ui_styles.SPACING["comfortable"], 0))

        # ── Date Range ────────────────────────────────────────────────────
        date_frame = ttk.LabelFrame(outer, text="Basic Start Date", padding=8)
        date_frame.pack(fill="x", pady=(0, ui_styles.SPACING["normal"]))

        self.date_from_var = self.bind_entry("os_date_from_var")
        self.date_to_var = self.bind_entry("os_date_to_var")

        # NOTE: the session/default values are applied at the END of this
        # method, after the DateEntry widgets exist. DateEntry.__init__
        # unconditionally stamps today over a pre-filled textvariable
        # (tkcalendar's _set_text), so any .set() before construction is
        # paved over -- and the bind trace writes today back into the
        # engine. Plain entries don't do this; only the dates are order-
        # sensitive.

        date_style = "OS_Date.TEntry"
        style = ttk.Style()
        style.configure(date_style, font=ui_styles.FONTS["body"])

        styled_label(date_frame, "From:").grid(row=0, column=0, sticky="e", padx=(0, 4))
        self.date_from = AutoClosingDateEntry(
            date_frame, textvariable=self.date_from_var,
            date_pattern=self.DATE_FORMAT, width=14,
            style=date_style,
        )
        self.date_from.grid(row=0, column=1, sticky="w")

        styled_label(date_frame, "To:").grid(row=0, column=2, sticky="e", padx=(10, 4))
        self.date_to = AutoClosingDateEntry(
            date_frame, textvariable=self.date_to_var,
            date_pattern=self.DATE_FORMAT, width=14,
            style=date_style,
        )
        self.date_to.grid(row=0, column=3, sticky="w")

        clear_style = {"fg": ui_styles.COLORS["text_secondary"], "bd": 0,
                       "cursor": "hand2"}
        clear_from = styled_label(date_frame, " \u2716", **clear_style)
        clear_from.grid(row=0, column=4, padx=(4, 0), sticky="w")
        clear_from.bind("<Button-1>", lambda e: self.date_from_var.set(""))

        clear_to = styled_label(date_frame, " \u2716", **clear_style)
        clear_to.grid(row=0, column=5, padx=(2, 0), sticky="w")
        clear_to.bind("<Button-1>", lambda e: self.date_to_var.set(""))

        # ── Order Info ────────────────────────────────────────────────────
        info_frame = ttk.LabelFrame(outer, text="Order Info", padding=8)
        info_frame.pack(fill="x", pady=(0, ui_styles.SPACING["normal"]))

        self.text_var = self.bind_entry("os_text_var")
        self.workplace_var = self.bind_entry("os_workplace_var")
        self.status_var = self.bind_entry("os_status_var")
        self.ingrp_var = self.bind_entry("os_ingrp_var")
        self.tplnr_var = self.bind_entry("os_tplnr_var")
        self.revnr_var = self.bind_entry("os_revnr_var")
        self.order_type_var = self.bind_entry("os_order_type_var")
        self.eqfnr_var = self.bind_entry("os_eqfnr_var")

        # Row 0: Planner Group + Status
        styled_label(info_frame, "Planner Group:").grid(row=0, column=0, sticky="e", padx=(0, 4))
        CustomEntry(info_frame, "textinput", "os_ingrp_var",
                     textvariable=self.ingrp_var, width=16).grid(row=0, column=1, sticky="w")

        styled_label(info_frame, "Status:").grid(row=0, column=2, sticky="e", padx=(10, 4))
        CustomEntry(info_frame, "textinput", "os_status_var",
                     textvariable=self.status_var, width=16).grid(row=0, column=3, sticky="w")

        # Row 1: Workplace + Revision
        styled_label(info_frame, "Workplace:").grid(row=1, column=0, sticky="e", padx=(0, 4))
        CustomEntry(info_frame, "textinput", "os_workplace_var",
                     textvariable=self.workplace_var, width=16).grid(row=1, column=1, sticky="w")

        styled_label(info_frame, "Revision:").grid(row=1, column=2, sticky="e", padx=(10, 4))
        CustomEntry(info_frame, "textinput", "os_revnr_var",
                     textvariable=self.revnr_var, width=16).grid(row=1, column=3, sticky="w")

        # Row 2: Fun. Location (full width)
        styled_label(info_frame, "Fun. Location:").grid(row=2, column=0, sticky="e", padx=(0, 4))
        CustomEntry(info_frame, "textinput", "os_tplnr_var",
                     textvariable=self.tplnr_var, width=48).grid(row=2, column=1, columnspan=3, sticky="ew")

        # Row 3: Text (full width)
        styled_label(info_frame, "Text:").grid(row=3, column=0, sticky="e", padx=(0, 4))
        CustomEntry(info_frame, "textinput", "os_text_var",
                     textvariable=self.text_var, width=48).grid(row=3, column=1, columnspan=3, sticky="ew")

        # Row 4: Order Type + Sort Field
        styled_label(info_frame, "Order Type:").grid(row=4, column=0, sticky="e", padx=(0, 4))
        CustomEntry(info_frame, "textinput", "os_order_type_var",
                     textvariable=self.order_type_var, width=16).grid(row=4, column=1, sticky="w")

        styled_label(info_frame, "Sort Field:").grid(row=4, column=2, sticky="e", padx=(10, 4))
        CustomEntry(info_frame, "textinput", "os_eqfnr_var",
                     textvariable=self.eqfnr_var, width=16).grid(row=4, column=3, sticky="w")

        # Session values back over the config defaults bind_entry applied.
        for key, attr in self.INFO_FILTER_ATTRS:
            self._restore_filter_var(key, getattr(self, attr))

        info_frame.grid_columnconfigure(1, weight=1)
        info_frame.grid_columnconfigure(3, weight=1)

        # ── Search button ─────────────────────────────────────────────────
        btn_row = styled_frame(outer)
        btn_row.pack(fill="x", pady=(0, ui_styles.SPACING["normal"]))
        clear_btn = CustomButton(
            btn_row, "buttons", "os_clear_btn",
            text="Clear",
            command=self._clear_filters,
        )
        # Packed right together: Search claims the far edge first, Clear
        # lands directly left of it.
        search_btn = CustomButton(
            btn_row, "buttons", "os_search_btn",
            text="Search",
            command=self._run_search,
            bg=ui_styles.COLORS["primary"], fg="#ffffff",
            font=(ui_styles.FONTS["body"][0], ui_styles.FONTS["body"][1], "bold"),
        )
        search_btn.pack(side="right")
        clear_btn.pack(side="right", padx=(0, 8))

        # Dates LAST: the pickers now exist, so these .set() calls stick
        # (see NOTE above). Never searched this session -> today/tomorrow;
        # an explicit clear ("") is honored as-is.
        today = datetime.now()
        tomorrow = today + timedelta(days=1)
        prev_from = self._filter_snapshot["os_date_from_var"]
        prev_to = self._filter_snapshot["os_date_to_var"]
        self.date_from_var.set(
            prev_from if prev_from is not None else today.strftime("%d.%m.%Y")
        )
        self.date_to_var.set(
            prev_to if prev_to is not None else tomorrow.strftime("%d.%m.%Y")
        )

    def _build_treeview(self):
        style = ttk.Style()
        style.configure("OS_Treeview.Treeview", font=ui_styles.FONTS["body"])
        style.configure("OS_Treeview.Treeview", background=ui_styles.COLORS["surface"],
                        fieldbackground=ui_styles.COLORS["surface"])

        # Grid + its horizontal scrollbar share a container so the bar sits
        # directly under the grid. A manager-packed bar would land after the
        # status bar where the packer squeezes it out on short windows.
        grid_frame = styled_frame(self)
        grid_frame.pack(fill="both", expand=True, padx=ui_styles.SPACING["comfortable"],
                        pady=ui_styles.SPACING["comfortable"])

        self.tree = self.CustomTreeview(
            grid_frame,
            component_type="datagridview",
            button_id="os_results_grid",
            style="OS_Treeview.Treeview",
        )

        self.grid_manager = GridManager(
            self.tree, "os_results_grid", self.config_data, manage_scrollbar=False
        )
        self.grid_manager.status_label = self.status_label
        self.grid_manager.apply_config()

        self._restore_grid_state()

        self._grid_h_scroll = ttk.Scrollbar(
            grid_frame, orient="horizontal", command=self.tree.xview
        )
        # Bar packed FIRST at the bottom: it keeps its height while the
        # expanding tree absorbs any shortage (packing it last lets the
        # packer squeeze it to zero on short windows).
        self._grid_h_scroll.pack(side="bottom", fill="x")
        self.tree.pack(side="top", fill="both", expand=True)
        self.tree.configure(xscrollcommand=self._grid_h_scroll.set)

        self.tree.bind("<Double-1>", self._on_row_double_click)

    def _build_status_bar(self, text: str = "Ready"):
        bar, self.status_label = super()._build_status_bar(text)
        bar.pack(fill="x", padx=2, pady=2)
        return bar, self.status_label

    def _clear_filters(self):
        """Reset the form to its first-open state: dates to today/tomorrow,
        the rest to config defaults, results grid emptied. The bound traces
        push the cleared fields into the engine and the grid snapshot key
        is dropped, so a reopen shows the same cleared form."""
        today = datetime.now()
        tomorrow = today + timedelta(days=1)
        self.date_from_var.set(today.strftime("%d.%m.%Y"))
        self.date_to_var.set(tomorrow.strftime("%d.%m.%Y"))
        for key, attr in self.INFO_FILTER_ATTRS:
            default = cfg.get_textinput_cfg(self.config_data, key).get("default_value", "")
            getattr(self, attr).set(default)
        self.tree.delete(*self.tree.get_children())
        self.grid_manager.clear()
        self.engine.variables.pop(self.GRID_STATE_VAR, None)
        self.report_status("Search filters cleared", "info")

    def _run_search(self):
        """Run IW38 search via SAP script."""
        self._delete_export()
        self.engine.set_variable("export_done", "0")
        # Fresh single-hit decision each search: stale values would fake one.
        self.engine.set_variable("os_tcode", "")
        self.engine.set_variable("os_single_order", "")
        self.engine.set_variable("os_single_text", "")
        self.engine.set_variable("os_single_status", "")
        self._wildcard_text_filter()
        self.config(cursor="watch")
        self.report_status("Searching...", "info")
        self.update()
        self.run_script("os_search_btn", title="IW38 Order Search",
                        on_success=self._on_search_executed)

    def _on_search_executed(self):
        """Find out which screen the search landed on: a single hit opens
        IW32 detail instead of the results list, leaving nothing to export."""
        self._wait_sap_ready()
        self.run_script("os_read_tcode_btn", title="Check Results View",
                        on_success=self._on_tcode_done)

    def _wait_sap_ready(self, timeout: float = 10.0):
        """Poll session.Busy until SAP settles after the list->detail jump.
        A single hit opens IW32 while it is still loading, and the first
        probe then fails on controls that exist a moment later. Skipped in
        debug mode (stepping is manual there) and instant when already idle,
        so normal list searches pay nothing."""
        if self._toolbar.debug_mode.get():
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.engine.set_variable("os_sap_busy", "?")
            self.engine.run_immediate("_busy = session.Busy\n{{ os_sap_busy }} = _busy")
            if (self.engine.get_variable("os_sap_busy") or "").strip().lower() not in (
                "true",
                "1",
            ):
                return
            time.sleep(0.3)
            self.update()

    def _on_tcode_done(self):
        if (self.engine.get_variable("os_tcode") or "").strip().upper() == "IW32":
            # Single hit opened order detail -- read its fields one isolated
            # script at a time, so one wrong id can't block the others.
            self.run_script("os_read_order_btn", title="Read Single Number",
                            on_success=self._on_single_order_done)
        else:
            # Results list (or anything else) -- export as usual.
            self._export_results()

    def _on_single_order_done(self, _retry=True):
        if (self.engine.get_variable("os_single_order") or "").strip():
            self._read_single_texts()
        elif _retry:
            # tcode proved IW32, but its controls weren't servable yet (the
            # screen is only briefly there while loading). Settle, then read
            # the number once more. Never falls through to export: there is
            # no list on IW32, exporting there is meaningless.
            self._wait_sap_ready()
            self.run_script("os_read_order_btn", title="Read Single Number",
                            on_success=lambda: self._on_single_order_done(False))
        else:
            self._report_single_unreadable()

    def _report_single_unreadable(self):
        """Single hit opened IW32, but its fields stayed unreadable."""
        self.config(cursor="")
        last_err = getattr(self.engine, "_last_error", None) or ""
        err_txt = f" SAP error: {last_err}{self._failing_script_line()}" if last_err else ""
        self.report_status(
            f"Single order is open in IW32 but unreadable.{err_txt}",
            "error" if last_err else "warning",
        )

    def _read_single_texts(self):
        # Single hit opened order detail -- read its fields one isolated
        # script at a time, so one wrong id can't block the others.
        self.run_script("os_read_text_btn", title="Read Single Order",
                        on_success=self._on_single_text_done)

    def _on_single_text_done(self):
        self.run_script("os_read_status_btn", title="Read Single Status",
                        on_success=self._on_single_reads_done)

    def _on_single_reads_done(self):
        # Data decides, not errors: an order number means single-hit detail
        # (even a partial row beats "No orders found"). Empty here means the
        # number reads all failed -- report honestly instead of exporting,
        # since there is no list on IW32 to export from.
        if (self.engine.get_variable("os_single_order") or "").strip():
            self._show_single_result()
        else:
            self._report_single_unreadable()

    def _show_single_result(self):
        """Display the single-hit order as one grid row (no list to export).
        Maps the three known fields onto the visible columns by header name;
        unknown columns stay blank. Clears any stale export rows first."""
        order_nr = (self.engine.get_variable("os_single_order") or "").strip()
        if not order_nr:
            self.config(cursor="")
            self.report_status("No orders found", "info")
            return
        short_text = self.engine.get_variable("os_single_text") or ""
        status = self.engine.get_variable("os_single_status") or ""
        columns = list(self.tree["columns"])
        values = []
        for c in columns:
            key = c.strip().lower()
            if key in ("order", "aufnr"):
                values.append(order_nr)
            elif key in ("description", "beschrijving", "korte tekst",
                         "omschrijving", "omschr", "text"):
                values.append(short_text)
            elif key.replace(" ", "") in ("status", "systeemstatus",
                                          "systemstatus", "sysstat", "sttxt"):
                values.append(status)
            else:
                values.append("")
        self.tree.delete(*self.tree.get_children())
        if hasattr(self, "grid_manager"):
            self.grid_manager.clear()
        self.tree.insert("", "end", values=values)
        self.config(cursor="")
        self.report_status("Found 1 order", "success")

    def _export_results(self):
        self.run_script("os_export_list_btn", title="Export Results",
                        on_success=self._on_search_complete)

    def _wildcard_text_filter(self):
        """SAP select-options match a text field exactly unless the pattern
        contains '*'. Wrap a plain filter in wildcards so 'tekst' also
        matches 'URG| tekst'. Idempotent: input already containing '*' (or
        blank) is left alone. The wrapped value stays visible in the field,
        so the SAP syntax is no surprise."""
        text = (self.text_var.get() or "").strip()
        if text and "*" not in text:
            self.text_var.set(f"*{text}*")
        elif not text and self.text_var.get():
            self.text_var.set("")

    def _on_search_complete(self):
        self.config(cursor="")
        if self.engine.get_variable("export_done") == "1" and os.path.exists(self._export_filepath):
            self.grid_manager.populate_from_export(self._export_filepath)
            count = len(self.tree.get_children())
            self.report_status(f"Found {count} orders", "success" if count else "info")
        else:
            last_err = getattr(self.engine, "_last_error", None) or ""
            err_txt = f" SAP error: {last_err}{self._failing_script_line()}" if last_err else ""
            self.report_status(f"No orders found.{err_txt}", "error" if last_err else "info")

    def _failing_script_line(self) -> str:
        """Describe the script line the engine stopped on (it never advances
        the pointer past a failed step), so real-SAP ID errors pinpoint the
        exact control without needing Debug mode."""
        try:
            cmds = getattr(self.engine, "commands", [])
            ptr = getattr(self.engine, "pointer", 0)
            if 0 <= ptr < len(cmds):
                cmd = cmds[ptr]
                return f" [script line {cmd.line_no}: {cmd.raw.strip()[:160]}]"
        except Exception:
            pass
        return ""

    def _on_row_double_click(self, event):
        """Set the order number on the engine and close."""
        item = self.tree.identify_row(event.y)
        if not item:
            return
        values = self.tree.item(item, "values")
        if not values:
            return

        # Find which column holds the order number ("Order" or "AUFNR")
        columns = list(self.tree["columns"])
        order_idx = next(
            (i for i, c in enumerate(columns) if c.lower() in ("order", "aufnr")),
            None,
        )
        if order_idx is not None and order_idx < len(values):
            order_number = values[order_idx].strip()
        else:
            # Fallback: pick the first non-empty value
            order_number = next((v.strip() for v in values if v.strip()), "")

        if not order_number:
            return

        self.engine.set_variable("order", order_number)
        # Pass the system status through too -- PRH shows it and flags TAFS
        # (technisch afgesloten, needs reopen). Empty when the column is hidden.
        status_idx = next(
            (i for i, c in enumerate(columns)
             if c.lower().replace(" ", "") in ("systeemstatus", "systemstatus", "sysstat", "sttxt")),
            None,
        )
        self.engine.set_variable(
            "order_system_status",
            values[status_idx].strip() if status_idx is not None and status_idx < len(values) else "",
        )
        self.report_status(f"Order {order_number} selected", "success")
        self.after(200, self.destroy)

    def _snapshot_grid_state(self):
        """Freeze the results grid into the engine (session-only JSON), so
        reopening shows the grid as it was closed. Prefers the manager's
        full data (all headers, including hidden columns); falls back to
        the live tree for rows the manager doesn't hold (single-hit
        results bypass it). Empty grid drops the key: reopen starts fresh."""
        mgr = getattr(self, "grid_manager", None)
        tree = getattr(self, "tree", None)
        if tree is None or not tree.winfo_exists():
            return
        if mgr is not None and getattr(mgr, "_stored_rows", None):
            columns = list(mgr.available_columns)
            rows = [list(r) for r in mgr._stored_rows]
        else:
            live = list(tree.get_children())
            if not live:
                self.engine.variables.pop(self.GRID_STATE_VAR, None)
                return
            columns = list(tree["columns"])
            rows = [list(tree.item(i, "values")) for i in live]
        if not columns or not rows:
            self.engine.variables.pop(self.GRID_STATE_VAR, None)
            return
        self.engine.set_variable(
            self.GRID_STATE_VAR, json.dumps({"columns": columns, "rows": rows})
        )

    def _restore_grid_state(self):
        """Rebuild the grid from the session snapshot through the manager's
        own renderer (headings, order, autosize). Corrupt state falls back
        to the fresh empty grid."""
        raw = self.engine.variables.get(self.GRID_STATE_VAR)
        if not raw:
            return
        try:
            state = json.loads(raw)
            columns = state.get("columns") or []
            rows = state.get("rows") or []
            if not columns or not rows or not all(isinstance(r, list) for r in rows):
                raise ValueError("empty grid state")
        except (ValueError, TypeError, AttributeError):
            self.engine.variables.pop(self.GRID_STATE_VAR, None)
            return
        self.grid_manager.available_columns = list(columns)
        self.grid_manager._stored_rows = [list(r) for r in rows]
        self.grid_manager._apply_columns(list(columns))
        self.report_status(f"Found {len(rows)} orders", "success")

    def destroy(self):
        """Snapshot the results grid before teardown (see
        _snapshot_grid_state), then run the standard unbind + destroy."""
        try:
            self._snapshot_grid_state()
        except Exception:
            pass
        super().destroy()

    def _delete_export(self):
        if os.path.exists(self._export_filepath):
            os.remove(self._export_filepath)
