"""EditModal -- a modal dialog for editing custom components.

Supports five component types:
- buttons: text, script, script_name
- textinput: var_name, default_value
- headers: text
datagridview: column_mapping (key-value pairs)
columnconfig: visible_columns and column_order (for Treeview column customization)

The modal adapts its form based on the component type and pre-fills values
from the provided data dict.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk
from typing import Any

from . import ui_styles
from .ui_styles import primary_button, styled_button, styled_entry, styled_frame, styled_label


class EditModal(tk.Toplevel):
    """Modal dialog for editing a custom component.

    Only one instance can be open at a time. Opening a new one will focus
    the existing instance or close it first.
    """

    _instance: EditModal | None = None  # Only one instance allowed

    @classmethod
    def _get_existing(cls) -> EditModal | None:
        """Get existing instance if any."""
        if cls._instance is not None and cls._instance.winfo_exists():
            return cls._instance
        cls._instance = None
        return None

    @classmethod
    def close_all(cls) -> None:
        """Destroy all open EditModal instances. Call on app shutdown."""
        if cls._instance is not None:
            try:
                cls._instance.destroy()
            except Exception:
                pass
        cls._instance = None

    def __init__(
        self,
        parent: tk.Misc,
        component_type: str,
        data: dict[str, Any],
        save_callback: Callable[[dict[str, Any]], None],
        title: str = "",
        titlebar_color=None,
        available_columns: list[str] | None = None,
    ):
        super().__init__(parent)
        self.title(f"Edit {component_type.title()}" if title else f"Edit {component_type.title()}")

        # Store available columns for columnconfig type
        self.available_columns = available_columns or []

        # Auto-shrink height for simpler forms
        if component_type == "headers":
            self.geometry("500x150")  # Only text field, wider
            self.resizable(False, False)
        elif component_type == "textinput":
            self.geometry("500x150")  # Two fields: var_name + default_value, wider
            self.resizable(False, False)
        elif component_type == "columnconfig":
            self.geometry("500x500")  # Column config: more height for list
            self.resizable(True, True)
        elif component_type == "buttons":
            self.geometry("600x500")  # Buttons: script field needs space
            self.resizable(True, True)
        else:
            self.geometry("500x350")  # Default: 3 fields, wider
            self.resizable(False, False)
        self.config(bg=ui_styles.COLORS["bg"])  # Match window background

        # Set title bar color (helper modals share the support color)
        if titlebar_color is None:
            titlebar_color = ui_styles.SUPPORT_COLOR
        self._titlebar_color = titlebar_color

        # Check for existing instance — close it first
        existing = EditModal._get_existing()
        if existing is not None:
            existing.destroy()

        self.withdraw()

        # Set component_type before building form
        self.component_type = component_type
        self.data = data
        self.save_callback = save_callback
        self.button_id = title  # Store button_id for display

        # Build the form while window is hidden
        self._build_form()
        self._build_buttons()

        # Force all widgets to realize their sizes
        self.update_idletasks()

        # Centre on the toolbar's monitor, then show (transparent first to
        # avoid flicker).
        from .window_chrome import (
            center_on_toolbar,
            correct_for_decorations,
            owner_window,
        )

        try:
            self.transient(owner_window(self))
        except Exception:
            pass
        center_on_toolbar(self)
        self.attributes("-alpha", 0.0)
        self.deiconify()
        self.lift()
        self.focus_force()
        correct_for_decorations(self)

        # Fade in over 100ms
        self.after(10, self._fade_in, titlebar_color)

        # Track as the single instance
        EditModal._instance = self

    def _fade_in(self, titlebar_color):
        """Fade window from transparent to opaque."""
        for alpha in [0.0, 0.3, 0.6, 1.0]:
            self.attributes("-alpha", alpha)
            self.update_idletasks()

        # Apply titlebar color after fade completes
        self._set_titlebar_color(titlebar_color)

    def _set_titlebar_color(self, color):
        """Set Windows title bar color."""
        from .window_chrome import apply_titlebar_color

        apply_titlebar_color(self, color)

    @classmethod
    def from_widget(
        cls,
        parent: tk.Widget,
        widget: tk.Widget,
        component_type: str,
        button_id: str,
        config_data: dict,
        save_callback: Callable[[dict[str, Any]], None],
    ):
        """Create an EditModal from a widget with stored metadata."""
        data = config_data.get("customcomponents", {}).get(component_type, {}).get(button_id, {})
        return cls(parent, component_type, data, save_callback, title=button_id)

    # ---- layout --------------------------------------------------------
    def _build_form(self):
        if self.component_type == "buttons":
            self._build_button_form()
        elif self.component_type == "textinput":
            self._build_textinput_form()
        elif self.component_type == "headers":
            self._build_header_form()
        elif self.component_type == "datagridview":
            self._build_grid_form()
        elif self.component_type == "columnconfig":
            self._build_columnconfig_form()

    def _build_button_form(self):
        self.text_var = tk.StringVar(value=self.data.get("text", ""))
        self.script_var = tk.StringVar(value=self.data.get("script", ""))
        self.script_name_var = tk.StringVar(value=self.data.get("script_name", ""))

        # Show button_id prominently so users know what to type in run_script()
        row = styled_frame(self)
        row.pack(fill="x", padx=12, pady=8)
        styled_label(row, "Button ID (for run_script):", font=ui_styles.FONTS["header"]).pack(side="left")
        styled_label(row, self.button_id, font=(ui_styles.FONTS["mono"], 11), fg=ui_styles.COLORS["primary"]).pack(side="left", padx=8)

        row = styled_frame(self)
        row.pack(fill="x", padx=12, pady=8)
        styled_label(row, "Button Text:", font=ui_styles.FONTS["header"]).pack(side="left")
        styled_entry(row, textvariable=self.text_var).pack(side="left", fill="x", expand=True)

        # Script field: multiline, expandable, with scrollbar
        row = styled_frame(self)
        row.pack(fill="both", expand=True, padx=12, pady=8)
        styled_label(row, "Script:", font=ui_styles.FONTS["header"]).pack(side="top", anchor="w")

        # Create scrollbars and text widget
        scrollbar_y = tk.Scrollbar(row, orient="vertical")
        scrollbar_y.pack(side="right", fill="y")

        scrollbar_x = tk.Scrollbar(row, orient="horizontal")
        scrollbar_x.pack(side="bottom", fill="x")

        self.script_text = tk.Text(
            row,
            height=8,
            width=50,
            wrap="none",
            font=ui_styles.FONTS["mono"],
            yscrollcommand=scrollbar_y.set,
            xscrollcommand=scrollbar_x.set,
        )
        self.script_text.pack(fill="both", expand=True)
        scrollbar_y.config(command=self.script_text.yview)
        scrollbar_x.config(command=self.script_text.xview)

        self.script_text.insert("1.0", self.data.get("script", ""))

        row = styled_frame(self)
        row.pack(fill="x", padx=12, pady=8)
        styled_label(row, "Script Name:", font=ui_styles.FONTS["header"]).pack(side="left")
        styled_entry(row, textvariable=self.script_name_var).pack(
            side="left", fill="x", expand=True
        )

    def _build_textinput_form(self):
        self.var_name_var = tk.StringVar(value=self.data.get("var_name", ""))
        self.default_value_var = tk.StringVar(value=self.data.get("default_value", ""))

        row = styled_frame(self)
        row.pack(fill="x", padx=12, pady=8)
        styled_label(row, "Variable Name:", font=ui_styles.FONTS["header"]).pack(side="left")
        styled_entry(row, textvariable=self.var_name_var).pack(side="left", fill="x", expand=True)

        row = styled_frame(self)
        row.pack(fill="x", padx=12, pady=8)
        styled_label(row, "Default Value:", font=ui_styles.FONTS["header"]).pack(side="left")
        styled_entry(row, textvariable=self.default_value_var).pack(
            side="left", fill="x", expand=True
        )

    def _build_header_form(self):
        self.text_var = tk.StringVar(value=self.data.get("text", ""))

        row = styled_frame(self)
        row.pack(fill="x", padx=12, pady=8)
        styled_label(row, "Header Text:", font=ui_styles.FONTS["header"]).pack(side="left")
        styled_entry(row, textvariable=self.text_var).pack(side="left", fill="x", expand=True)

    def _build_grid_form(self):
        column_mapping = self.data.get("column_mapping", {})
        self.entries: dict[str, tk.Entry] = {}

        for key, value in column_mapping.items():
            row = styled_frame(self)
            row.pack(fill="x", padx=12, pady=4)
            styled_label(row, f"{key}:", font=ui_styles.FONTS["header"]).pack(side="left")
            entry = styled_entry(row, textvariable=tk.StringVar(value=value))
            entry.pack(side="left", fill="x", expand=True)
            self.entries[key] = entry

    def _build_columnconfig_form(self):
        """Build form for column configuration with display names and order controls."""
        if not self.available_columns:
            msg_frame = styled_frame(self)
            msg_frame.pack(fill="both", expand=True, padx=12, pady=8)
            styled_label(
                msg_frame, "No columns available yet.", font=ui_styles.FONTS["header"]
            ).pack(pady=(0, 4))
            styled_label(
                msg_frame,
                "Export data from SAP first, then reopen this dialog to configure columns.",
                font=ui_styles.FONTS["body"],
                fg=ui_styles.COLORS["text_secondary"],
            ).pack(fill="x")
            return

        visible_cols = self.data.get("visible_columns", self.available_columns[:])
        col_order = self.data.get("column_order", self.available_columns[:])
        display_names = self.data.get("column_display_names", {})

        if not visible_cols:
            visible_cols = self.available_columns[:]
        if not col_order:
            col_order = self.available_columns[:]

        # Merge saved order with live export headers: keep saved order
        # (deduped), then append any live column not yet in config so new
        # SAP columns always appear in the dialog instead of being dropped
        # on save. Stale config entries are kept so preferences survive
        # variant changes; GridManager filters to live headers on display.
        seen: set[str] = set()
        merged_order: list[str] = []
        for c in list(col_order) + list(self.available_columns):
            if c.startswith("_spacer_") or c in seen:
                continue
            seen.add(c)
            merged_order.append(c)

        original_order_set = set(col_order)
        visible_set = set(visible_cols)

        self.column_vars: dict[str, tk.BooleanVar] = {}
        self.up_buttons: dict[str, tk.Button] = {}
        self.down_buttons: dict[str, tk.Button] = {}
        self.display_name_vars: dict[str, tk.StringVar] = {}

        self._ui_column_order = merged_order
        self._drag_col: str | None = None

        # Search + bulk actions
        ctrl = tk.Frame(self, bg=ui_styles.COLORS["bg"])
        ctrl.pack(fill="x", padx=12, pady=(8, 4))
        self._search_var = tk.StringVar()
        search_entry = tk.Entry(
            ctrl, textvariable=self._search_var, font=ui_styles.FONTS["body"],
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=ui_styles.COLORS["border"],
        )
        search_entry.pack(side="left", fill="x", expand=True)
        search_entry.insert(0, "Search columns...")
        search_entry.bind("<FocusIn>", lambda e: search_entry.delete(0, "end") if search_entry.get() == "Search columns..." else None)
        self._search_var.trace_add("write", lambda *_: self._apply_filter())

        bulk = tk.Frame(ctrl, bg=ui_styles.COLORS["bg"])
        bulk.pack(side="right", padx=(8, 0))
        for label, cmd in (("All", self._select_all), ("None", self._select_none), ("Invert", self._invert_selection)):
            tk.Button(
                bulk, text=label, command=cmd, font=ui_styles.FONTS["small"],
                bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
                relief="flat", bd=0, padx=6, pady=2,
                activebackground=ui_styles.COLORS["surface_hover"],
            ).pack(side="left", padx=2)

        scroll_frame = tk.Frame(self, bg=ui_styles.COLORS["bg"])
        scroll_frame.pack(fill="both", expand=True, padx=12, pady=8)

        canvas = tk.Canvas(scroll_frame, highlightthickness=0, bg=ui_styles.COLORS["bg"])
        self._column_canvas = canvas
        scrollbar = ttk.Scrollbar(scroll_frame, orient="vertical", command=canvas.yview)

        self._column_scroll_frame = tk.Frame(canvas, bg=ui_styles.COLORS["bg"])
        self._column_scroll_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self._column_scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        scroll_frame.grid_rowconfigure(0, weight=1)
        scroll_frame.grid_columnconfigure(0, weight=1)

        def _on_canvas_resize(event):
            self._column_scroll_frame.config(width=event.width)

        canvas.bind("<Configure>", _on_canvas_resize)

        def _on_mousewheel(event):
            try:
                if canvas.winfo_exists():
                    canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            except tk.TclError:
                pass

        canvas.bind("<MouseWheel>", _on_mousewheel)

        for col_name in merged_order:
            is_new = col_name not in original_order_set
            self._build_column_row(
                col_name=col_name,
                visible=(col_name in visible_set) or is_new,
                display_name=display_names.get(col_name, col_name),
            )

        if merged_order:
            self.up_buttons[merged_order[0]].config(state="disabled")
            self.down_buttons[merged_order[-1]].config(state="disabled")

        self._column_scroll_frame.update_idletasks()
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _build_column_row(self, col_name: str, visible: bool, display_name: str):
        """Build a single column-config row: drag handle, checkbox, name, display-entry."""
        row = tk.Frame(self._column_scroll_frame, bg=ui_styles.COLORS["bg"])
        row.pack(fill="x", padx=4, pady=2)
        row.bind("<Enter>", lambda e, r=row: r.config(bg=ui_styles.COLORS["surface_hover"]))
        row.bind("<Leave>", lambda e, r=row: r.config(bg=ui_styles.COLORS["bg"]))

        # ── Drag handle ─────
        handle = tk.Label(
            row, text="\u2630", font=ui_styles.FONTS["body"],
            bg=ui_styles.COLORS["bg"], fg=ui_styles.COLORS["text_secondary"],
            cursor="fleur", padx=4,
        )
        handle.pack(side="left")
        handle.bind("<ButtonPress-1>", lambda e, c=col_name: self._drag_start(c, e))
        handle.bind("<B1-Motion>", self._drag_motion)
        handle.bind("<ButtonRelease-1>", self._drag_end)

        # ── Checkbox (visibility toggle) ─────
        var = tk.BooleanVar(value=visible)
        self.column_vars[col_name] = var
        cb = tk.Checkbutton(
            row, variable=var, command=self._on_column_toggle,
            bg=ui_styles.COLORS["bg"], activebackground=ui_styles.COLORS["bg"],
        )
        cb.pack(side="left", padx=(4, 2))

        # ── Up / Down buttons ────
        up_btn = tk.Button(
            row, text="\u25b2",
            command=lambda c=col_name: self._move_column_up(c),
            fg=ui_styles.COLORS["primary"], bg=ui_styles.COLORS["surface"],
            activebackground=ui_styles.COLORS["primary"], relief="flat",
            bd=0, padx=2, pady=0,
        )
        up_btn.pack(side="left", padx=1)
        self.up_buttons[col_name] = up_btn

        down_btn = tk.Button(
            row, text="\u25bc",
            command=lambda c=col_name: self._move_column_down(c),
            fg=ui_styles.COLORS["primary"], bg=ui_styles.COLORS["surface"],
            activebackground=ui_styles.COLORS["primary"], relief="flat",
            bd=0, padx=2, pady=0,
        )
        down_btn.pack(side="left", padx=1)
        self.down_buttons[col_name] = down_btn

        # ── Real SAP column name (read-only, mono, small) ────
        tk.Label(
            row,
            text=col_name,
            font=(ui_styles.FONTS["mono"][0], ui_styles.FONTS["mono"][1] - 1),
            bg=ui_styles.COLORS["bg"],
            fg=ui_styles.COLORS["text_secondary"],
            anchor="w",
        ).pack(side="left", padx=(6, 4))

        # ── Display name entry ────
        dv = tk.StringVar(value=display_name)
        self.display_name_vars[col_name] = dv
        entry = tk.Entry(
            row, textvariable=dv,
            font=ui_styles.FONTS["body"],
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text"],
            relief="flat", bd=0,
            highlightthickness=1, highlightbackground=ui_styles.COLORS["border"],
        )
        entry.pack(side="left", fill="x", expand=True, padx=(4, 0))

    def _on_column_toggle(self):
        """Refresh button states when visibility is toggled."""
        self._update_move_button_states()

    def _move_column_up(self, col_name: str):
        """Move a column up in the order."""
        order = self._ui_column_order
        i = order.index(col_name)
        if i > 0:
            order[i], order[i - 1] = order[i - 1], order[i]
            self._refresh_column_order()
            self._update_move_button_states()

    def _move_column_down(self, col_name: str):
        """Move a column down in the order."""
        order = self._ui_column_order
        i = order.index(col_name)
        if i < len(order) - 1:
            order[i], order[i + 1] = order[i + 1], order[i]
            self._refresh_column_order()
            self._update_move_button_states()

    def _refresh_column_order(self):
        """Rebuild the column list in the UI to reflect the new order."""
        for child in self._column_scroll_frame.winfo_children():
            child.destroy()
        for col_name in self._ui_column_order:
            self._build_column_row(
                col_name=col_name,
                visible=self.column_vars[col_name].get(),
                display_name=self.display_name_vars[col_name].get(),
            )
        self._update_move_button_states()

    def _update_move_button_states(self):
        """Update enabled state of up/down buttons based on current order."""
        order = self._ui_column_order
        for i, col_name in enumerate(order):
            if col_name in self.up_buttons:
                self.up_buttons[col_name].config(state="disabled" if i == 0 else "normal")
            if col_name in self.down_buttons:
                self.down_buttons[col_name].config(
                    state="disabled" if i == len(order) - 1 else "normal"
                )

    def _build_buttons(self):
        row = styled_frame(self)
        row.pack(fill="x", padx=12, pady=8, side="bottom")  # Snap to bottom
        # Use titlebar color for Save button
        save_btn = primary_button(
            row,
            "Save",
            command=self._save,
            bg=self._titlebar_color
            if hasattr(self, "_titlebar_color")
            else ui_styles.COLORS["edit"],
        )
        save_btn.pack(side="left")

        if self.component_type == "columnconfig":
            styled_button(
                row, "Reset",
                command=self._reset_column_config,
                fg=ui_styles.COLORS["error"],
            ).pack(side="left", padx=(8, 0))

        styled_button(
            row, "Cancel", command=self.destroy, fg=ui_styles.COLORS["text_secondary"]
        ).pack(side="right")

    # ---- reset/cancel/save --------------------------------------------------
    def _reset_column_config(self):
        """Reset column config to show all SAP columns in original order."""
        if not self.available_columns:
            return
        live = [c for c in self.available_columns if not c.startswith("_spacer_")]
        self._ui_column_order = live[:]
        for col, var in self.column_vars.items():
            var.set(col in live)
        for col, var in self.display_name_vars.items():
            if col in live:
                var.set(col)
        self._refresh_column_order()

    def _drag_start(self, col_name: str, event):
        self._drag_col = col_name
        self._drag_start_y = event.y_root

    @staticmethod
    def _row_col_name(row, column_vars):
        """Column name for a config row (matched via its name label)."""
        for w in row.winfo_children():
            if isinstance(w, tk.Label):
                text = w.cget("text")
                if text in column_vars:
                    return text
        return None

    def _child_rows(self) -> list[tk.Frame]:
        """Config row widgets (always tk.Frame). winfo_children() is typed as
        Widget | Toplevel, so narrow once here instead of in every loop."""
        return [
            r for r in self._column_scroll_frame.winfo_children()
            if isinstance(r, tk.Frame)
        ]

    def _mapped_rows(self):
        """Displayed config rows in order (skips search-filtered ones)."""
        return [r for r in self._child_rows() if r.winfo_ismapped()]

    def _drag_motion(self, event):
        if not self._drag_col:
            return
        rows = self._mapped_rows()
        target = None
        for row in rows:
            mid = row.winfo_rooty() + row.winfo_height() // 2
            if event.y_root < mid:
                target = row
                break
        for row in self._child_rows():
            row.config(bg=ui_styles.COLORS["bg"])
        if target is not None:
            target.config(bg=ui_styles.COLORS["primary"])

    def _drag_end(self, event):
        if not self._drag_col:
            return
        col = self._drag_col
        self._drag_col = None
        for row in self._child_rows():
            row.config(bg=ui_styles.COLORS["bg"])
        current = list(self._ui_column_order)
        if col not in current:
            return
        current.remove(col)
        # Insert before the drop-target row's column. The target is resolved
        # among VISIBLE rows, then mapped back into the full order -- this
        # stays correct when dragging down (indices shift after removal) and
        # while a search filter hides rows. Dropping below all visible rows
        # appends at the end.
        insert_at = len(current)
        for row in self._mapped_rows():
            mid = row.winfo_rooty() + row.winfo_height() // 2
            if event.y_root < mid:
                target_col = self._row_col_name(row, self.column_vars)
                if target_col is not None and target_col in current:
                    insert_at = current.index(target_col)
                break
        current.insert(insert_at, col)
        self._ui_column_order = current
        # preserve scroll
        canvas = getattr(self, "_column_canvas", None)
        frac = canvas.yview()[0] if canvas is not None else 0.0
        self._refresh_column_order()
        self._apply_filter()
        if canvas is not None:
            canvas.yview_moveto(frac)

    def _apply_filter(self):
        q = (self._search_var.get() or "").strip().lower()
        if q == "search columns...":
            q = ""
        for row in self._child_rows():
            col = self._row_col_name(row, self.column_vars)
            if col is None:
                continue
            if q and q not in col.lower():
                row.pack_forget()
            else:
                row.pack(fill="x", padx=4, pady=2)

    def _select_all(self):
        for var in self.column_vars.values():
            var.set(True)

    def _select_none(self):
        for var in self.column_vars.values():
            var.set(False)

    def _invert_selection(self):
        for var in self.column_vars.values():
            var.set(not var.get())

    def _save(self):
        """Collect values from form and call save_callback."""
        if self.component_type == "buttons":
            new_data = {
                "text": self.text_var.get(),
                "script": self.script_text.get("1.0", "end-1c"),  # Read from Text widget
                "script_name": self.script_name_var.get(),
            }
        elif self.component_type == "textinput":
            new_data = {
                "var_name": self.var_name_var.get(),
                "default_value": self.default_value_var.get(),
            }
        elif self.component_type == "headers":
            new_data = {
                "text": self.text_var.get(),
            }
        elif self.component_type == "datagridview":
            new_data = {
                "column_mapping": {key: entry.get() for key, entry in self.entries.items()},
            }
        elif self.component_type == "columnconfig":
            visible_columns = [col for col, var in self.column_vars.items() if var.get()]
            ordered_columns = list(self._ui_column_order)
            display_names = {
                col: var.get() for col, var in self.display_name_vars.items()
            }
            new_data = {
                "visible_columns": visible_columns,
                "column_order": ordered_columns,
                "column_display_names": display_names,
            }
        else:
            new_data = {}

        self.save_callback(new_data)
        self.destroy()
