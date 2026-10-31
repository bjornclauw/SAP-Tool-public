"""PurchaseRequestHelperWindow -- unified PRH (= ATB) tool window.

Split layout via tk.PanedWindow (60% left / 40% right, both stretch):
   Left  — order card + SAP export Treeview (owns all vertical growth) +
           fixed-height attachments zone (tree + fixed-width upload column)
   Right — ATB type radios + Description + vendor lookup + 2-col field grid
           + action buttons, then the history pane (owns right-pane growth)

All behaviour is config-driven: button scripts, field defaults, column
mappings live in config/toolbar_config.json. Right-click any CustomEntry or
CustomButton to edit its config in-place.

Variable wiring uses ScriptRunnerMixin (see custom_widgets.py) exclusively:
bind_entry()/bind_var() for fields, run_script() for buttons. Nothing in
this file calls trace_add, set_variable, or opens the debugger directly --
if you're tempted to, that's a sign it belongs in the mixin, not here.

This replaces the old standalone atb_helper.py — the ATB functionality is
now part of the PRH screen as requested.
"""

from __future__ import annotations

import os
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from tkinter import ttk

import tkinterdnd2

from core import config as cfg
from tools import outlook_drop
from tools.atb_history import AtbHistoryStore, parse_atb_number
from tools.atb_history_view import AtbHistoryPane
from tools.grid_column_manager import GridColumnManager as GridManager
from tools.order_searcher import AutoClosingDateEntry
from ui import ui_styles
from ui.custom_menu import CustomMenuBar, MenuBarItem
from ui.custom_widgets import BaseToolWindow, CustomButton, CustomEntry, CustomLabel
from ui.ui_styles import styled_frame, styled_label
from ui.window_chrome import show_centered

# ---------------------------------------------------------------------------
# ATB field definitions — used to build the grid and bind variables.
# Each entry maps to a textinput config key (e.g. "atb_txz01"); var_name and
# default_value are read from config/toolbar_config.json via bind_entry(),
# not hardcoded here.
# ---------------------------------------------------------------------------
ATB_FIELDS = [
    ("atb_txz01", "Description"),  # Full width
    ("atb_lifnr", "Vendor"),  # Lookup dropdown
    ("atb_afnam", "Requisitioner"),
    ("atb_eeind", "Delivery Date"),
    ("atb_name1", "Plant"),
    ("atb_lgobe", "Storage Location"),
    ("atb_ekgrp", "Purchasing Group"),
    ("atb_ekorg", "Purchasing Org"),
    ("atb_wgbbez", "Material Group"),
    ("atb_sakto", "G/L Account"),
    ("atb_ktext1", "Service Text"),
]
# Fields shown in the 2-column grid (Description and Vendor get their own
# full-width rows above the grid -- see _build_right_panel).
ATB_GRID_FIELDS = ATB_FIELDS[2:]

# ---------------------------------------------------------------------------
# _VendorSearchBox -- entry + a dropdown list owned by us, not ttk.
#
# ttk.Combobox cannot do "filter while typing with the list open": opening
# its native popdown moves keyboard focus into the popdown listbox, so the
# next keystroke never reaches the entry (and pulling focus back unmaps the
# list). The ttkwidgets library doesn't help either -- both its autocomplete
# widgets do inline text completion only, with no filtering list at all.
# This box keeps focus in the entry for its whole lifetime: the dropdown is
# a plain Listbox in an overrideredirect Toplevel, driven entirely by entry
# bindings, so typing, arrows, Return and Escape all work without a focus
# fight. Private to this tool until a second window needs one.
# ---------------------------------------------------------------------------
_DROP_MAX_ROWS = 12
_DROP_MAX_RENDER = 200


class _VendorSearchBox(tk.Entry):
    """Live-filter vendor picker. Owner sets entries + on_commit(display)."""

    # Keys that move in the list or leave it without changing the text:
    # refiltering on these would rebuild the open list mid-navigation.
    _NAV_KEYS = frozenset({
        "Up", "Down", "Left", "Right", "Home", "End", "Prior", "Next",
        "Return", "Escape", "Tab",
    })

    def __init__(self, master, on_commit=None, **kwargs):
        super().__init__(master, **kwargs)
        self._on_commit_cb = on_commit
        self._entries: list[str] = []
        self._matches: list[str] = []
        self._highlight = -1
        self._hide_after: str | None = None

        self._popup = tk.Toplevel(self)
        self._popup.withdraw()
        self._popup.overrideredirect(True)
        try:
            self._popup.transient(self.winfo_toplevel())
        except tk.TclError:
            pass
        self._listbox = tk.Listbox(
            self._popup,
            font=ui_styles.FONTS["body"],
            activestyle="none",
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text"],
            selectbackground=ui_styles.COLORS["primary"],
            selectforeground="#ffffff",
            relief="solid",
            borderwidth=1,
            highlightthickness=0,
            takefocus=0,
        )
        self._vscroll = ttk.Scrollbar(
            self._popup, orient="vertical", command=self._listbox.yview
        )
        self._listbox.configure(yscrollcommand=self._vscroll.set)
        self._vscroll.pack(side="right", fill="y")
        self._listbox.pack(side="left", fill="both", expand=True)

        self.bind("<KeyRelease>", self._on_text_key)
        self.bind("<Down>", self._on_down)
        self.bind("<Up>", self._on_up)
        self.bind("<Return>", self._on_return)
        self.bind("<Escape>", lambda e: self.hide())
        self.bind("<FocusOut>", self._on_focus_out)
        self._listbox.bind("<ButtonRelease-1>", self._on_list_click)
        self._listbox.bind("<MouseWheel>", self._on_list_wheel)

    # ---- owner API -------------------------------------------------
    def set_entries(self, entries: list[str]):
        """Replace the full display list (e.g. after a CRUD mutation)."""
        self._entries = list(entries)
        self.hide()

    def set(self, text: str):
        """Programmatic text (commit, clear). Never refilters by itself."""
        self.delete(0, tk.END)
        self.insert(0, text)
        if not text:
            self.hide()

    @property
    def matches(self) -> list[str]:
        return list(self._matches)

    def is_open(self) -> bool:
        return bool(self._popup.winfo_ismapped())

    def hide(self):
        self._cancel_hide()
        try:
            self._popup.withdraw()
        except tk.TclError:
            pass
        self._highlight = -1

    # ---- events ----------------------------------------------------
    def _on_text_key(self, event):
        if event is not None and getattr(event, "keysym", "") in self._NAV_KEYS:
            return
        self._apply_filter()

    def _on_down(self, event=None):
        if not self.is_open():
            if not self.get().strip():
                self._matches = list(self._entries[:_DROP_MAX_RENDER])
            else:
                self._apply_filter(silent=True)
            if self._matches:
                self._show()
            return
        self._move_highlight(1)

    def _on_up(self, event=None):
        if self.is_open():
            self._move_highlight(-1)

    def _on_return(self, event=None):
        rendered = self._rendered()
        if self.is_open() and 0 <= self._highlight < len(rendered):
            self._commit_display(rendered[self._highlight])
        elif len(self._matches) == 1:
            self._commit_display(self._matches[0])
        elif self._matches:
            self._show()

    def _on_focus_out(self, event=None):
        # Delayed so a listbox click (press = FocusOut, release = commit a
        # few ms later) still lands: commit cancels the scheduled hide.
        self._cancel_hide()
        try:
            self._hide_after = self.after(10, self.hide)
        except tk.TclError:
            self._hide_after = None

    def _on_list_click(self, event):
        idx = self._listbox.nearest(event.y)
        rendered = self._rendered()
        if 0 <= idx < len(rendered):
            self._commit_display(rendered[idx])

    def _on_list_wheel(self, event):
        self._listbox.yview_scroll(-1 * (event.delta // 120), "units")
        return "break"

    # ---- internals -------------------------------------------------
    def _cancel_hide(self):
        if self._hide_after is not None:
            try:
                self.after_cancel(self._hide_after)
            except tk.TclError:
                pass
            self._hide_after = None

    def _rendered(self) -> list[str]:
        return self._matches[:_DROP_MAX_RENDER]

    def _apply_filter(self, silent: bool = False):
        q = self.get().strip().lower()
        if not q:
            self.hide()
            self._matches = []
            return
        self._matches = [e for e in self._entries if q in e.lower()]
        if self._matches and not silent:
            self._show()
        elif not self._matches:
            self.hide()

    def _show(self):
        rendered = self._rendered()
        if not rendered:
            self.hide()
            return
        self._cancel_hide()
        self._listbox.delete(0, tk.END)
        for m in rendered:
            self._listbox.insert(tk.END, m)
        self._listbox.configure(height=min(len(rendered), _DROP_MAX_ROWS))
        if len(rendered) > _DROP_MAX_ROWS:
            if not self._vscroll.winfo_ismapped():
                self._vscroll.pack(side="right", fill="y")
        else:
            self._vscroll.pack_forget()
        font = tkfont.Font(font=ui_styles.FONTS["body"])
        zero = font.measure("0") or 1
        widest = max([font.measure(m) for m in rendered] + [self.winfo_width()])
        chars = max(10, min(70, widest // zero + 2))
        self._listbox.configure(width=chars)
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height()
        self._popup.geometry(f"+{x}+{y}")
        self._popup.deiconify()
        self._popup.lift()
        self._set_highlight(0)

    def _set_highlight(self, idx: int):
        rendered = self._rendered()
        if not rendered:
            self._highlight = -1
            return
        self._highlight = max(0, min(idx, len(rendered) - 1))
        self._listbox.selection_clear(0, tk.END)
        self._listbox.selection_set(self._highlight)
        self._listbox.activate(self._highlight)
        self._listbox.see(self._highlight)

    def _move_highlight(self, delta: int):
        rendered = self._rendered()
        if not rendered:
            return
        self._set_highlight((self._highlight + delta) % len(rendered))

    def _commit_display(self, display: str):
        self._cancel_hide()
        self.hide()
        if self._on_commit_cb is not None and display:
            self._on_commit_cb(display)


# ---------------------------------------------------------------------------
# _HoverTip -- delayed hover popup for one widget.
#
# A plain tk.Toplevel owned by the widget (dies with it), shown after a
# short hover delay, hidden on leave/press. Private to this tool until a
# second window needs one.
# ---------------------------------------------------------------------------
_TAFS_TIP = (
    "TAFS = technisch afgesloten (technically closed).\n"
    "The order is locked: reopen it before creating ATBs."
)


class _HoverTip:
    """Mouse-over explainer. Owner keeps the instance alive."""

    def __init__(self, widget, text: str, delay_ms: int = 500):
        self._widget = widget
        self._text = text
        self._delay_ms = delay_ms
        self._after_id = None
        self._popup = None
        widget.bind("<Enter>", self._schedule, "+")
        widget.bind("<Leave>", self.hide, "+")
        widget.bind("<ButtonPress>", self.hide, "+")

    @property
    def text(self) -> str:
        return self._text

    def is_open(self) -> bool:
        return self._popup is not None

    def _schedule(self, event=None):
        self.hide()
        try:
            self._after_id = self._widget.after(self._delay_ms, self.show)
        except tk.TclError:
            pass  # widget going away

    def show(self):
        self.hide()
        try:
            x = self._widget.winfo_rootx()
            y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        except tk.TclError:
            return
        popup = tk.Toplevel(self._widget)
        popup.withdraw()
        popup.overrideredirect(True)
        try:
            popup.attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(
            popup, text=self._text, justify="left", wraplength=320,
            bg=ui_styles.COLORS["warning_light"], fg=ui_styles.COLORS["text"],
            relief="solid", borderwidth=1,
            font=ui_styles.FONTS["small"], padx=8, pady=6,
        ).pack()
        popup.update_idletasks()
        popup.geometry(f"+{x}+{y}")
        popup.deiconify()
        self._popup = popup

    def hide(self, event=None):
        if self._after_id is not None:
            try:
                self._widget.after_cancel(self._after_id)
            except (tk.TclError, ValueError):
                pass
            self._after_id = None
        popup, self._popup = self._popup, None
        if popup is not None:
            try:
                popup.destroy()
            except tk.TclError:
                pass


class PurchaseRequestHelperWindow(BaseToolWindow):
    """Unified Purchase Request Helper + ATB creation window.

    Layout:
        ┌──────────────────────────┬────────────────────┐
        │ ┌ Order ───────────────┐ │  (•) Goods ( ) Services│
        │ │ Order nr [__] [Lookup]│ ├────────────────────┤
        │ │ Description: [______] │ │  Description: [__] │
        │ │ Status: [___]        │ │  Vendor: [____] [_] │
        │ │ Start: [_] Workplace │ │  Requisitioner …    │
        │ ├──────────────────────┤ │  [Create … ATB][Clear]│
        │ │ lookup grid (grows)  │ ├────────────────────┤
        │ │                      │ │  History (grows)   │
        │ ├──────────────────────┤ │                    │
        │ │ Attachments │ Upload │ │                    │
        │ │ (fixed H)   │(fix W) │ │                    │
        └──────────────────────────┴────────────────────┘
    """

    def __init__(self, parent, engine):
        super().__init__(parent, engine, titlebar_color=ui_styles.TOOL_COLORS["purchase_request"])
        self.title("Purchase Request Helper")

        # Start maximized
        self.state("zoomed")

        # Tools menu (same CustomMenuBar as the main toolbar): packs first
        # so it docks at the very top, above the panes.
        self._vendor_win = None
        self._menu_bar = CustomMenuBar(self)
        self._menu_bar.add_menu(
            "Tools", [MenuBarItem("Vendors...", self._open_vendor_manager)]
        )

        self._init_export_paths()

        # Local ATB/upload history + transient cross-callback state. The
        # meta snapshot is refreshed on every attachment read (when it ran
        # clean) so recordings pair an order with its own date/workplace.
        self._history = AtbHistoryStore()
        self._last_order_meta: dict = {}
        self._pending_attach_open: dict | None = None
        # Throttle clock for the 40-char SAP cap warnings (see _cap_validate).
        self._cap_warned_at = 0.0

        # --- Layout ---
        # Main horizontal PanedWindow: left (lookup+tree) | right (proforma+ATB).
        # opaqueresize=False: while the sash is dragged only a proxy line
        # moves; the panes relayout ONCE on release. Live-resizing repaints
        # the whole right column (radios, grid, history trees) on every
        # motion pixel, which tears visibly mid-drag and only settles after.
        self._paned = tk.PanedWindow(
            self, orient="horizontal", sashrelief="flat", bg=ui_styles.COLORS["bg"],
            opaqueresize=False,
        )
        self._paned.pack(fill="both", expand=True, padx=2, pady=2)

        # Left pane — lookup + treeview
        self._left_frame = styled_frame(self._paned)
        self._paned.add(self._left_frame)

        # Right pane — proforma (top 30%) + ATB (bottom 70%)
        self._right_frame = styled_frame(self._paned)
        self._paned.add(self._right_frame)

        self._supplier_mapping: dict[str, str] = {}
        self._supplier_display: dict[str, tuple[str, str]] = {}
        self._supplier_names: list[str] = []
        self._supplier_entries: list[tuple[str, str, str]] = []
        self._build_left_panel()
        self._build_right_panel()
        self._build_status_bar()
        self._init_grid_manager()

        # Panes cooperate: neither may collapse below its usable width, and
        # both absorb window growth (otherwise the PanedWindow parks all
        # extra space in the left pane and the ATB form stays squeezed).
        self._paned.paneconfigure(self._left_frame, minsize=480, stretch="always")
        self._paned.paneconfigure(self._right_frame, minsize=360, stretch="always")

        # Place the sash AFTER the panes have content: before the build both
        # frames are empty, so a width measured here would be ~0 and the
        # split would silently fall back to jammed-at-minsize.
        self.update_idletasks()
        paned_w = self._paned.winfo_width()
        if paned_w > 1:
            self._paned.sash_place(0, int(paned_w * 0.60), 1)

        # Minimum window: below 960x640 the order card, the ATB form, and the
        # upload column can no longer fit side by side (verified: widgets get
        # unmapped instead of squeezed). The WM clamps to this.
        self.minsize(960, 640)

        self.engine.process_events = self.update
        self._show()

    # ---- initialization ------------------------------------------------
    def _init_export_paths(self):
        """Compute the export file path and push it to the engine. This is
        NOT a UI-bound variable (nothing types into it), so it's a plain
        set_variable, not bind_var -- it doesn't belong in the field-binding
        system."""
        self._export_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tempexport")
        self._export_file = "sap_me5a_export.txt"
        os.makedirs(self._export_path, exist_ok=True)
        self.engine.set_variable("export_path", self._export_path)
        self.engine.set_variable("export_file", self._export_file)



    # ---- layout --------------------------------------------------------
    def _build_left_panel(self):
        """Build the lookup row + Treeview on the left side."""
        # --- Order card: number + actions, description, status (aligned rows) ---
        card = styled_frame(
            self._left_frame,
            highlightbackground=ui_styles.COLORS["border"],
            highlightthickness=1,
        )
        card.pack(
            fill="x",
            padx=ui_styles.SPACING["comfortable"],
            pady=(ui_styles.SPACING["comfortable"], ui_styles.SPACING["normal"]),
        )
        body = styled_frame(card)
        body.pack(fill="x", padx=ui_styles.SPACING["normal"], pady=ui_styles.SPACING["normal"])

        header_cfg = cfg.get_header_cfg(self.config_data, "prh_lookup_entry")
        header_label = CustomLabel(
            body,
            "headers",
            "prh_lookup_entry",
            text=header_cfg.get("text", "Order"),
            font=ui_styles.FONTS["header"],
        )
        header_label.pack(anchor="w", pady=(0, ui_styles.SPACING["tight"]))

        _lbl_w = 12  # one width for every row label so fields line up

        row1 = styled_frame(body)
        row1.pack(fill="x")
        styled_label(row1, "Order nr:", width=_lbl_w, anchor="e").pack(side="left", padx=(0, 4))

        # One line: reads var_name/default_value from config, wires both
        # directions, registers for auto pre-run sync.
        self.order_var = self.bind_entry("prh_lookup_entry")

        entry = CustomEntry(
            row1, "textinput", "prh_lookup_entry", textvariable=self.order_var, width=18
        )
        entry.pack(side="left")
        entry.bind("<Return>", lambda e: self._run_lookup())

        btn_cfg = cfg.get_button_cfg(self.config_data, "prh_lookup_btn")
        btn_text = btn_cfg.get("text", "Lookup")
        lookup_btn = CustomButton(
            row1,
            "buttons",
            "prh_lookup_btn",
            text=btn_text,
            width=12,
            command=self._run_lookup,
            bg=ui_styles.COLORS["primary"],
            fg="#ffffff",
            activebackground=ui_styles.COLORS["primary_hover"],
        )
        lookup_btn.pack(side="left", padx=(ui_styles.SPACING["normal"], 0))

        # Order Search button — opens OrderSearcherWindow popup
        search_cfg = cfg.get_button_cfg(self.config_data, "prh_order_search_btn")
        search_btn = CustomButton(
            row1,
            "buttons",
            "prh_order_search_btn",
            text=search_cfg.get("text", "Search Orders"),
            width=14,
            command=self._open_order_searcher,
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text"],
        )
        search_btn.pack(side="left", padx=(ui_styles.SPACING["tight"], 0))

        row2 = styled_frame(body)
        row2.pack(fill="x", pady=(ui_styles.SPACING["tight"], 0))
        # Short text — read-only, bound to order_short_text, auto-updated by engine
        self._st_var = tk.StringVar()
        self.bind_var("order_short_text", self._st_var)
        styled_label(row2, "Description:", width=_lbl_w, anchor="e").pack(side="left", padx=(0, 4))
        st_entry = tk.Entry(row2, textvariable=self._st_var, state="readonly",
                            font=ui_styles.FONTS["body"], bg=ui_styles.COLORS["surface"],
                            relief="flat", readonlybackground=ui_styles.COLORS["surface"])
        st_entry.pack(side="left", fill="x", expand=True)

        row3 = styled_frame(body)
        row3.pack(fill="x", pady=(ui_styles.SPACING["tight"], 0))
        # Order system status — read-only, engine-fed (IW32 STTXT via Lookup
        # or Systeemstatus via the Search picker). Only the first token
        # matters: TAFS = technisch afgesloten, must be reopened before PRH
        # can work. The Reopen button appears at the right of this row.
        self._status_var = tk.StringVar()
        self.bind_var("order_system_status", self._status_var)
        self.watch_engine_var("order_system_status", self._on_order_status_changed)

        styled_label(row3, "Status:", width=_lbl_w, anchor="e").pack(side="left", padx=(0, 4))
        self._status_entry = tk.Entry(row3, textvariable=self._status_var, state="readonly",
                                      font=ui_styles.FONTS["body"], bg=ui_styles.COLORS["surface"],
                                      relief="flat", readonlybackground=ui_styles.COLORS["surface"])
        self._status_entry.pack(side="left", fill="x", expand=True)
        # TAFS explainer on hover (status codes are SAP-opaque to new users).
        self._status_tip = _HoverTip(self._status_entry, _TAFS_TIP)

        # Reopen button — stays hidden until the order is TAFS. Everything it
        # does in SAP lives in its config script (right-click editable); the
        # TAFS guard lives in _run_reopen_order, not in the mixin.
        reopen_cfg = cfg.get_button_cfg(self.config_data, "prh_reopen_btn")
        self._reopen_btn = CustomButton(
            row3,
            "buttons",
            "prh_reopen_btn",
            text=reopen_cfg.get("text", "Reopen order"),
            width=12,
            command=self._run_reopen_order,
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["error"],
            activebackground=ui_styles.COLORS["surface_hover"],
        )
        self._reopen_tip = _HoverTip(self._reopen_btn, _TAFS_TIP)

        row4 = styled_frame(body)
        row4.pack(fill="x", pady=(ui_styles.SPACING["tight"], 0))
        # Basic start + main work center — read-only, engine-fed (IW32
        # GSTRP/VAPLZ via Lookup's attachment refresh, same as history).
        self._start_var = tk.StringVar()
        self.bind_var("order_basic_start", self._start_var)
        styled_label(row4, "Start:", width=_lbl_w, anchor="e").pack(side="left", padx=(0, 4))
        start_entry = tk.Entry(row4, textvariable=self._start_var, state="readonly", width=12,
                               font=ui_styles.FONTS["body"], bg=ui_styles.COLORS["surface"],
                               relief="flat", readonlybackground=ui_styles.COLORS["surface"])
        start_entry.pack(side="left")
        self._workplace_var = tk.StringVar()
        self.bind_var("order_workplace", self._workplace_var)
        styled_label(row4, "Workplace:", width=_lbl_w, anchor="e").pack(
            side="left", padx=(ui_styles.SPACING["normal"], 4))
        wp_entry = tk.Entry(row4, textvariable=self._workplace_var, state="readonly",
                            font=ui_styles.FONTS["body"], bg=ui_styles.COLORS["surface"],
                            relief="flat", readonlybackground=ui_styles.COLORS["surface"])
        wp_entry.pack(side="left", fill="x", expand=True)

        # --- Treeview (fills remaining space) ---
        # Built AFTER the attachments zone on purpose: the zone anchors to
        # the bottom (side="bottom") and claims its height first, so the
        # grid -- packed last with expand -- absorbs ALL shortage and ALL
        # growth. Packing it before the zone made the zone (packed last)
        # the one squeezed in short windows instead.
        self._build_attachments_zone()
        self._build_lookup_grid()

    def _on_order_status_changed(self, value: str):
        """Color the status field and toggle the Reopen button: red + bold
        with the button shown when the order's first status token is TAFS
        (technisch afgesloten), default text and no button otherwise. SAP
        concatenates all active statuses, hence first-token-only."""
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        try:
            first = (value or "").split()[0] if (value or "").strip() else ""
            if first == "TAFS":
                self._status_entry.config(
                    fg=ui_styles.COLORS["error"],
                    font=(ui_styles.FONTS["body"][0], ui_styles.FONTS["body"][1], "bold"),
                )
                self._reopen_btn.pack(side="right", padx=(ui_styles.SPACING["tight"], 0))
            else:
                self._status_entry.config(fg=ui_styles.COLORS["text"], font=ui_styles.FONTS["body"])
                self._reopen_btn.pack_forget()
        except Exception:
            # Window is being torn down (or already gone) -- a stale engine
            # callback must never take down the new window's __init__.
            return

    def _build_lookup_grid(self):
        """Build the Treeview that displays SAP export data."""
        style = ttk.Style()
        style.configure(
            "PRTreeview.Treeview",
            font=ui_styles.FONTS["body"],
            background=ui_styles.COLORS["surface"],
            fieldbackground=ui_styles.COLORS["surface"],
        )

        # Grid + its horizontal scrollbar share a container so the bar sits
        # directly under the grid. A manager-packed bar would land as the
        # left pane's last child (below the attachments zone) where the
        # packer squeezes it out on short windows -- exactly when scrolling
        # is needed.
        grid_frame = styled_frame(self._left_frame)
        grid_frame.pack(
            side="top",
            fill="both",
            expand=True,
            padx=ui_styles.SPACING["comfortable"],
            pady=ui_styles.SPACING["comfortable"],
        )

        # Use CustomTreeview for right-click column config
        self.tree = self.CustomTreeview(
            grid_frame,
            component_type="datagridview",
            button_id="prh_lookup_grid",
            style="PRTreeview.Treeview",
        )
        self._grid_h_scroll = ttk.Scrollbar(
            grid_frame, orient="horizontal", command=self.tree.xview
        )
        self._grid_v_scroll = ttk.Scrollbar(
            grid_frame, orient="vertical", command=self.tree.yview
        )
        # Title first: packs above the bars/tree, never squeezed (no expand).
        grid_title_cfg = cfg.get_header_cfg(self.config_data, "prh_grid_header")
        self._grid_title = CustomLabel(
            grid_frame,
            "headers",
            "prh_grid_header",
            text=grid_title_cfg.get("text", "ATB's for order"),
            font=ui_styles.FONTS["header"],
            bg=ui_styles.COLORS["bg"],
        )
        self._grid_title.pack(side="top", fill="x", pady=(0, ui_styles.SPACING["tight"]))
        # Order matters: v-bar on the right first, then the h-bar at the
        # bottom, then the tree. The h-bar stays packed adjacent to the tree
        # (test_ui_structure asserts this) so the packer can't squeeze it out
        # on short windows -- the tree absorbs the shortage instead.
        self._grid_v_scroll.pack(side="right", fill="y")
        self._grid_h_scroll.pack(side="bottom", fill="x")
        self.tree.pack(side="top", fill="both", expand=True)
        self.tree.configure(xscrollcommand=self._grid_h_scroll.set,
                            yscrollcommand=self._grid_v_scroll.set)
        self.tree.bind("<Double-1>", self._on_lookup_row_double_click)

    def _build_attachments_zone(self):
        """Build the attachments zone: treeview (expands) + fixed-width
        drag-and-drop upload column (never squeezed out).

        Fixed height on purpose: the lookup grid above owns ALL vertical
        growth, so this zone never fights it 50/50 -- at short heights the
        grid shrinks while attachments keep their 6 rows + scrollbars."""
        # Container for the two-column layout -- grid, not pack: column 0
        # (tree) owns all extra room AND absorbs all shortage, column 1
        # (upload) keeps its width no matter what. With pack, the tree's
        # 590px of fixed columns bullied the fixed-width upload frame to
        # zero and the whole column (drop zone, Refresh) unmapped.
        attachments_container = styled_frame(self._left_frame)
        attachments_container.pack(
            side="bottom",
            fill="x",
            padx=ui_styles.SPACING["comfortable"],
            pady=(0, ui_styles.SPACING["comfortable"]),
        )
        attachments_container.grid_columnconfigure(0, weight=1)
        attachments_container.grid_rowconfigure(0, weight=1)

        # --- Left: Attachments Treeview (owns the horizontal room) ---
        attachments_frame = styled_frame(attachments_container)
        attachments_frame.grid(row=0, column=0, sticky="nsew",
                               padx=(0, ui_styles.SPACING["normal"]))

        # Header
        att_header = styled_label(
            attachments_frame,
            "Attachments",
            font=ui_styles.FONTS["header"],
            bg=ui_styles.COLORS["bg"],
        )
        att_header.pack(anchor="w")

        # Treeview for attachments
        style = ttk.Style()
        style.configure(
            "AttTreeview.Treeview",
            font=ui_styles.FONTS["body"],
            background=ui_styles.COLORS["surface"],
            fieldbackground=ui_styles.COLORS["surface"],
        )

        att_body = styled_frame(attachments_frame)
        att_body.pack(fill="both", expand=True)

        self._attachments_tree = ttk.Treeview(
            att_body,
            columns=("doc_nr", "description", "filename", "status"),
            displaycolumns=("doc_nr", "description", "filename", "status"),
            show="headings",
            height=6,
            style="AttTreeview.Treeview",
        )
        self._attachments_tree.heading("doc_nr", text="Doc. Nr.")
        self._attachments_tree.heading("description", text="Description")
        self._attachments_tree.heading("filename", text="Filename")
        self._attachments_tree.heading("status", text="Status")
        self._attachments_tree.column("doc_nr", width=100, minwidth=80, anchor="w", stretch=False)
        self._attachments_tree.column("description", width=180, minwidth=100, anchor="w", stretch=True)
        self._attachments_tree.column("filename", width=200, minwidth=100, anchor="w", stretch=True)
        self._attachments_tree.column("status", width=110, minwidth=80, anchor="w", stretch=False)
        self._att_v_scroll = ttk.Scrollbar(
            att_body, orient="vertical", command=self._attachments_tree.yview
        )
        self._att_h_scroll = ttk.Scrollbar(
            att_body, orient="horizontal", command=self._attachments_tree.xview
        )
        self._att_v_scroll.pack(side="right", fill="y")
        self._att_h_scroll.pack(side="bottom", fill="x")
        self._attachments_tree.pack(side="top", fill="both", expand=True)
        self._attachments_tree.configure(
            yscrollcommand=self._att_v_scroll.set,
            xscrollcommand=self._att_h_scroll.set,
        )

        # Double-click handler for metadata editing
        self._attachments_tree.bind("<Double-1>", self._on_attachment_double_click)

        # --- Right: Drag-and-Drop Upload Zone (fixed width) ---
        # Fixed on purpose: with equal expand the 480px of attachment
        # columns squeezed this whole column (drop zone, Refresh button
        # included) to zero in merely narrow windows. 230px fits the label
        # and the button; the tree owns every pixel beyond that.
        upload_frame = styled_frame(attachments_container, width=230)
        upload_frame.grid(row=0, column=1, sticky="ns")
        upload_frame.pack_propagate(False)

        # Header
        upload_header = styled_label(
            upload_frame,
            "Upload",
            font=ui_styles.FONTS["header"],
            bg=ui_styles.COLORS["bg"],
        )
        upload_header.pack(anchor="w")

        # Drag and drop zone with proper DND support
        self._drop_zone = tk.Frame(
            upload_frame,
            bg=ui_styles.COLORS["surface"],
            highlightbackground=ui_styles.COLORS["border"],
            highlightthickness=2,
            relief="solid",
            cursor="hand2",
        )
        self._drop_zone.pack(fill="both", expand=True, pady=ui_styles.SPACING["normal"])

        drop_label = styled_label(
            self._drop_zone,
            "Drag & drop files here\nor click to upload",
            font=ui_styles.FONTS["body"],
            fg=ui_styles.COLORS["text_secondary"],
            bg=ui_styles.COLORS["surface"],
            anchor="center",
            cursor="hand2",
            wraplength=190,
            justify="center",
        )
        drop_label.pack(expand=True, fill="both", padx=ui_styles.SPACING["normal"], pady=ui_styles.SPACING["normal"])
        drop_label.bind("<Button-1>", self._on_upload_click)

        # Setup DND with tkinterdnd2. DND_ALL ('*') is required: Outlook
        # never offers file paths (only FileGroupDescriptor/FileContents),
        # so a files-only target rejects the drop before Tk ever sees it.
        # The inner label covers nearly the whole zone, so register it too
        # -- otherwise drops landing on the text are lost. Both fire for
        # one drop; the same-data guard in _on_file_drop skips the duplicate.
        # tkinterdnd2 patches drop_target_register/dnd_bind onto tk widgets
        # when its Tk is created, so the static checker can't see them --
        # verified present at runtime (see TkinterDnD.Tk children).
        self._drop_zone.drop_target_register(tkinterdnd2.DND_ALL)  # type: ignore[attr-defined]
        self._drop_zone.dnd_bind('<<Drop>>', self._on_file_drop)  # type: ignore[attr-defined]
        drop_label.drop_target_register(tkinterdnd2.DND_ALL)  # type: ignore[attr-defined]
        drop_label.dnd_bind('<<Drop>>', self._on_file_drop)  # type: ignore[attr-defined]
        self._last_drop: tuple[str, float] = ("", 0.0)

        # Bind click to open file picker
        self._drop_zone.bind("<Button-1>", self._on_upload_click)

        # Refresh attachments button
        self._refresh_btn = CustomButton(
            upload_frame,
            "buttons",
            "refresh_attachments_btn",
            text="⟳ Refresh",
            command=self._on_refresh_attachments_clicked,
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text"],
            activebackground=ui_styles.COLORS["surface_hover"],
        )
        self._refresh_btn.pack(fill="x", pady=ui_styles.SPACING["tight"])

    def _build_right_panel(self):
        """Build the right panel: ATB form (100%)."""
        # --- ATB Zone (full height) ---
        atb_container = styled_frame(self._right_frame)
        atb_container.pack(
            fill="both",
            expand=True,
            padx=ui_styles.SPACING["normal"],
            pady=(ui_styles.SPACING["comfortable"], ui_styles.SPACING["normal"]),
        )

        # Form card: bordered like the Order card on the left. The
        # container's top pad is comfortable (not normal) so both
        # rectangles start on the same line.
        form_card = styled_frame(
            atb_container,
            highlightbackground=ui_styles.COLORS["border"],
            highlightthickness=1,
        )
        form_card.pack(fill="x")
        self._form_card = form_card
        form_body = styled_frame(form_card)
        form_body.pack(
            fill="x",
            padx=ui_styles.SPACING["normal"],
            pady=ui_styles.SPACING["normal"],
        )

        # Header row: what this column is for (left) + Goods/Services
        # switch (right). One row, not two: an "ATB Type:" label used to sit
        # under the title in the same bold face, doubling the noise for zero
        # information -- the switch labels itself.
        header_row = styled_frame(form_body)
        header_row.pack(fill="x", padx=0, pady=(0, ui_styles.SPACING["normal"]))

        form_title_cfg = cfg.get_header_cfg(self.config_data, "atb_form_header")
        self._form_title = CustomLabel(
            header_row,
            "headers",
            "atb_form_header",
            text=form_title_cfg.get("text", "Add ATB to order"),
            font=ui_styles.FONTS["header"],
            bg=ui_styles.COLORS["bg"],
        )
        self._form_title.pack(side="left")

        # Goods / Services segmented switch (right-aligned in the header row).

        # Segmented Goods/Services switch. Flat toggle buttons (indicatoron
        # OFF) with EXPLICIT selected/unselected color pairs applied in
        # _paint_type_radios: native indicator dots render theme-dependent
        # (on some setups both states look identically "filled"). Critical
        # Tk detail: with indicatoron off, the ACTIVE face is painted with
        # -selectcolor, NOT -bg -- so selectcolor carries the primary blue
        # and the paint sets the matching white text. The shared atb_type
        # var still drives the engine sync -- the paint is pure view.
        self._atb_type_var = self.bind_var("atb_type", tk.StringVar(value=""))

        # Packed right-first: Services claims the far edge, Goods lands
        # left of it -- visual order stays [Goods][Services].
        self._services_radio = tk.Radiobutton(
            header_row,
            text="Services",
            variable=self._atb_type_var,
            value="D",
            font=ui_styles.FONTS["body"],
            bg=ui_styles.COLORS["bg"],
            selectcolor=ui_styles.COLORS["primary"],
            indicatoron=False,
            padx=8,
            pady=2,
            command=self._on_atb_type_changed,
        )
        self._services_radio.pack(side="right")

        self._goods_radio = tk.Radiobutton(
            header_row,
            text="Goods",
            variable=self._atb_type_var,
            value="",
            font=ui_styles.FONTS["body"],
            bg=ui_styles.COLORS["bg"],
            selectcolor=ui_styles.COLORS["primary"],
            indicatoron=False,
            padx=8,
            pady=2,
            command=self._on_atb_type_changed,
        )
        self._goods_radio.pack(side="right", padx=(0, ui_styles.SPACING["tight"]))

        # --- Description field (full width) ---
        desc_frame = styled_frame(form_body)
        desc_frame.pack(fill="x", padx=0, pady=ui_styles.SPACING["tight"])

        desc_header = CustomLabel(
            desc_frame,
            "headers",
            "atb_txz01_header",
            text="Description",
            font=ui_styles.FONTS["body"],
            bg=ui_styles.COLORS["bg"],
        )
        desc_header.pack(side="left")
        # SAP caps the description at 40 chars -- live counter, enforced
        # on keystroke below (see _install_length_cap).
        self._desc_counter = styled_label(
            desc_frame, "", font=ui_styles.FONTS["small"],
            fg=ui_styles.COLORS["text_secondary"], bg=ui_styles.COLORS["bg"],
        )
        self._desc_counter.pack(side="right")

        self.desc_var = self.bind_entry("atb_txz01")

        self.desc_entry = CustomEntry(
            desc_frame, "textinput", "atb_txz01", textvariable=self.desc_var, width=30
        )
        self.desc_entry.pack(fill="x", pady=ui_styles.SPACING["tight"])
        self.desc_entry.config(font=ui_styles.FONTS["body"])
        self._install_length_cap(self.desc_entry, 40, self._desc_counter, "Description")

        # --- Vendor lookup dropdown ---
        vendor_frame = styled_frame(form_body)
        vendor_frame.pack(fill="x", padx=0, pady=ui_styles.SPACING["tight"])

        vendor_header = CustomLabel(
            vendor_frame,
            "headers",
            "atb_lifnr_header",
            text="Vendor",
            font=ui_styles.FONTS["body"],
            bg=ui_styles.COLORS["bg"],
        )
        vendor_header.pack(anchor="w")

        self.vendor_var = self.bind_entry("atb_lifnr")

        vendor_row = styled_frame(vendor_frame)
        vendor_row.pack(fill="x", pady=ui_styles.SPACING["tight"])

        # Supplier search box: entry + owned filter list (see
        # _VendorSearchBox -- ttk's popdown steals entry focus on open).
        # Displays resolve back through _supplier_display so two masters
        # sharing one name stay separately pickable.
        self._supplier_combo = _VendorSearchBox(
            vendor_row,
            on_commit=self._select_supplier_display,
            font=ui_styles.FONTS["body"],
        )
        self._supplier_combo.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self._reload_suppliers()

        # Vendor code entry (shorter, shows the SAP code)
        self.vendor_entry = CustomEntry(
            vendor_row, "textinput", "atb_lifnr", textvariable=self.vendor_var, width=10
        )
        self.vendor_entry.pack(side="left")
        self.vendor_entry.config(font=ui_styles.FONTS["body"])

        # --- 2-column grid for remaining fields (fixed height: the history
        # zone below owns all expansion so the buttons hug the fields) ---
        atb_grid = styled_frame(form_body)
        atb_grid.pack(fill="x", expand=False, padx=0, pady=ui_styles.SPACING["tight"])

        self._atb_entries: list = []
        self._atb_headers: list[CustomLabel] = []

        self._service_text_header = None
        self._service_text_entry = None

        self._delivery_var = None
        self._delivery_entry = None
        self._delivery_header = None

        # Same picker + pattern as the order lookup (see order_searcher):
        # dd.MM.yyyy, body font, dropdown auto-closes on outside click.
        # NOTE: no tinted twin style -- on the native Windows theme the
        # entry field is OS-painted, so a ttk fieldbackground never shows.
        # The order-changed flash lives on the header label (plain tk,
        # always paintable) instead. See _flash_delivery_date.
        date_style = "PRH_Date.TEntry"
        ttk.Style().configure(date_style, font=ui_styles.FONTS["body"])
        self._delivery_base_style = date_style

        for idx, (config_key, label_text) in enumerate(ATB_GRID_FIELDS):
            row_idx = idx // 2  # 2 fields per row
            field_in_row = idx % 2  # 0 or 1

            # Each field occupies 2 columns: header (col 0/2) + entry (col 1/3)
            col_start = field_in_row * 2

            # Header label (right-aligned, padding on right)
            header_cfg = cfg.get_header_cfg(self.config_data, f"{config_key}_header")
            header_text = header_cfg.get("text", label_text)
            header = CustomLabel(
                atb_grid,
                "headers",
                f"{config_key}_header",
                text=header_text,
                font=ui_styles.FONTS["body"],
                bg=ui_styles.COLORS["bg"],
            )
            header.grid(
                row=row_idx,
                column=col_start,
                sticky="e",
                padx=(0, 4),
                pady=ui_styles.SPACING["tight"],
            )
            self._atb_headers.append(header)
            if config_key == "atb_eeind":
                self._delivery_header = header

            # Entry field -- one line, config-driven, auto-synced.
            # Delivery Date is the exception: same DateEntry picker as the
            # order lookup (bound to the same eeind engine var a CustomEntry
            # would use). It has no CustomEntry._var, so _clear_atb_fields
            # re-syncs it via _sync_delivery_date() instead of the loop.
            var = self.bind_entry(config_key)

            if config_key == "atb_eeind":
                self._delivery_var = var
                entry = AutoClosingDateEntry(
                    atb_grid, textvariable=var,
                    date_pattern="dd.MM.yyyy", width=12,
                    style=date_style,
                )
                entry.grid(
                    row=row_idx,
                    column=col_start + 1,
                    sticky="ew",
                    padx=0,
                    pady=ui_styles.SPACING["tight"],
                )
                self._delivery_entry = entry
                # Ownership hooks: typing or picking the date yourself
                # acknowledges the order-driven tint and clears it.
                entry.bind("<Key>", self._unflash_delivery_date, "+")
                entry.bind("<<DateEntrySelected>>", self._unflash_delivery_date, "+")
                self._atb_entries.append(entry)
                continue

            entry = CustomEntry(atb_grid, "textinput", config_key, textvariable=var, width=12)
            # Service Text is the odd field out (9 fields, 2 per row): it
            # spans cols 1-2 with a 40-char SAP counter in col 3, instead of
            # dangling in the row's left half. grid_remove/grid keeps this
            # layout when the mode toggle hides and re-shows the row.
            is_service_text = config_key == "atb_ktext1"
            entry.grid(
                row=row_idx,
                column=col_start + 1,
                columnspan=2 if is_service_text else 1,
                sticky="ew",
                padx=0,
                pady=ui_styles.SPACING["tight"],
            )
            entry.config(font=ui_styles.FONTS["body"])

            self._atb_entries.append(entry)

            if is_service_text:
                self._service_text_header = header
                self._service_text_entry = entry
                self._service_counter = styled_label(
                    atb_grid, "", font=ui_styles.FONTS["small"],
                    fg=ui_styles.COLORS["text_secondary"], bg=ui_styles.COLORS["bg"],
                )
                self._service_counter.grid(row=row_idx, column=3, sticky="e",
                                           padx=(4, 0), pady=ui_styles.SPACING["tight"])
                self._install_length_cap(entry, 40, self._service_counter, "Service Text")

        # Configure grid: 4 columns, entries expand, headers fixed
        for c in range(4):
            atb_grid.grid_columnconfigure(c, weight=1 if c % 2 == 1 else 0)

        # Delivery Date default: the order's date when one is selected,
        # else today (overrides the stale hardcoded config default).
        self._sync_delivery_date()

        # --- ATB action buttons ---
        btn_row = styled_frame(form_body)
        btn_row.pack(fill="x", padx=0, pady=(ui_styles.SPACING["normal"], 0))

        # Dynamic submit button (changes based on Goods/Services selection)
        self._atb_submit_btn = CustomButton(
            btn_row,
            "buttons",
            "atb_goods_btn",
            text="Create Goods ATB",
            command=self._run_atb_current,
            bg=ui_styles.COLORS["primary"],
            fg="#ffffff",
            activebackground=ui_styles.COLORS["primary_hover"],
        )
        self._atb_submit_btn.pack(side="left", padx=(0, ui_styles.SPACING["normal"]))

        clear_btn_cfg = cfg.get_button_cfg(self.config_data, "atb_clear_btn")
        clear_btn = CustomButton(
            btn_row,
            "buttons",
            "atb_clear_btn",
            text=clear_btn_cfg.get("text", "Clear"),
            command=self._clear_atb_fields,
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text"],
            activebackground=ui_styles.COLORS["surface_hover"],
        )
        clear_btn.pack(side="left")

        # --- History (fills remaining space) ---
        self._history_pane = AtbHistoryPane(
            atb_container,
            self._history,
            on_open_order=self._open_order_in_sap,
            on_open_atb=self._open_atb_in_sap,
            on_open_attachment=self._open_history_attachment,
        )
        # Breathing room where the separator used to be (no ruling line
        # between the form card and the history).
        self._history_pane.frame.pack_configure(
            pady=(ui_styles.SPACING["normal"], 0)
        )

        # Apply initial Goods state (hide service text, set button text)
        self._sync_submit_button("")

    # ---- supplier lookup -------------------------------------------------
    def _reload_suppliers(self):
        """Rebuild the picker from the vendor file: display entries, the
        display -> (name, code) map, and the plain name map. Called after
        the search box exists and after every vendor-manager mutation."""
        from tools.supplier_lookup import combo_entries, read_table

        _, _, rows = read_table()
        self._supplier_entries = combo_entries(rows)
        self._supplier_display = {d: (n, c) for d, n, c in self._supplier_entries}
        self._supplier_names = [d for d, _, _ in self._supplier_entries]
        self._supplier_mapping = {n: c for _, n, c in self._supplier_entries}
        self._supplier_combo.set_entries(self._supplier_names)

    def _select_supplier_display(self, display: str):
        """Resolve one display string to its (name, code) master."""
        pair = self._supplier_display.get(display)
        if not pair:
            return
        name, code = pair
        self._supplier_combo.set(display)
        self.vendor_var.set(code)
        self.report_status(f"Supplier: {name} ({code})", "success")

    def _supplier_picked_name(self) -> str:
        """Pure vendor name behind the current combo text (history must
        record 'VERMEERSCH LASERPRODUCTS', not its '(TORHOUT)' display)."""
        pair = self._supplier_display.get(self._supplier_combo.get())
        if pair:
            return pair[0]
        return (self._supplier_combo.get() or "").strip()

    def _build_status_bar(self, text: str = "Ready"):
        bar, self.status_label = super()._build_status_bar(text)
        bar.pack(fill="x", padx=2, pady=2)
        self._copy_status_btn = tk.Button(
            bar,
            text="Copy",
            width=6,
            font=ui_styles.FONTS["small"],
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text_secondary"],
            activebackground=ui_styles.COLORS["surface_hover"],
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._copy_status_message,
        )
        self._copy_status_btn.pack(side="right", padx=(0, 8))
        return bar, self.status_label

    def _copy_status_message(self):
        """Copy the current status-bar text to the clipboard. Silent on
        purpose -- reporting would overwrite the very message being copied."""
        self.clipboard_clear()
        self.clipboard_append(self.status_label.cget("text"))
        self._copy_status_btn.config(text="Copied")
        self.after(1200, lambda: self._copy_status_btn.config(text="Copy"))

    def _init_grid_manager(self):
        """Initialize the GridManager for this window's treeview."""
        # manage_scrollbar=False: _build_lookup_grid packs its own bar
        # directly under the grid (see above for why).
        self.grid_manager = GridManager(
            self.tree, "prh_lookup_grid", self.config_data, manage_scrollbar=False
        )
        self.grid_manager.status_label = self.status_label
        self.grid_manager.apply_config()

    # ---- ATB type toggle ----------------------------------------------
    def _on_atb_type_changed(self):
        """Toggle field visibility and update submit button. atb_type itself
        is already synced automatically -- it's a bound var (see
        _build_right_panel), so nothing needs to happen for the engine here."""
        self._sync_submit_button(self._atb_type_var.get())

    def _sync_submit_button(self, atb_type: str):
        """Switch between Goods and Services mode.

        Goods (atb_type=""): button says "Create Goods ATB"
        Services (atb_type="D"): button says "Create Services ATB"
        """
        if atb_type == "":
            self._atb_submit_btn.config(text="Create Goods ATB")
            self._atb_submit_btn.config(command=self._run_atb_goods)
            self._atb_submit_btn.button_id = "atb_goods_btn"
            self._paint_type_radios(self._goods_radio)
            if self._service_text_header:
                self._service_text_header.grid_remove()
            if self._service_text_entry:
                self._service_text_entry.grid_remove()
            if getattr(self, "_service_counter", None) is not None:
                self._service_counter.grid_remove()
        else:
            self._atb_submit_btn.config(text="Create Services ATB")
            self._atb_submit_btn.config(command=self._run_atb_services)
            self._atb_submit_btn.button_id = "atb_services_btn"
            self._paint_type_radios(self._services_radio)
            if self._service_text_header:
                self._service_text_header.grid()
            if self._service_text_entry:
                self._service_text_entry.grid()
            if getattr(self, "_service_counter", None) is not None:
                self._service_counter.grid()

    def _paint_type_radios(self, selected: tk.Radiobutton):
        """Paint the segmented switch: the active half gets white-on-primary
        (with matching hover), the idle half surface/dark text. Every color
        in every state is explicit -- nothing is left to the platform theme,
        which is what made both halves look identically 'filled' before."""
        c = ui_styles.COLORS
        for rb in (self._goods_radio, self._services_radio):
            if rb is selected:
                rb.config(bg=c["primary"], fg="#ffffff",
                          activebackground=c["primary_hover"],
                          activeforeground="#ffffff", relief="sunken")
            else:
                rb.config(bg=c["surface"], fg=c["text"],
                          activebackground=c["surface_hover"],
                          activeforeground=c["text"], relief="raised")

    def _run_atb_current(self):
        """Run the currently selected ATB script (Goods or Services)."""
        if self._atb_type_var.get() == "":
            self._run_atb_goods()
        else:
            self._run_atb_services()

    # ---- SAP field length caps --------------------------------------
    def _install_length_cap(self, entry: tk.Entry, limit: int,
                            counter_label: tk.Label, field_label: str):
        """Cap an entry at `limit` chars (SAP hard limit): keystrokes and
        pastes beyond it are rejected, and the live `n/limit` counter next
        to the field updates on every accepted edit. Validation only sees
        keystrokes -- programmatic sets bypass it, so Clear also refreshes
        the counter (see _clear_atb_fields)."""
        vcmd = (
            self.register(
                lambda proposed, lim=limit, counter=counter_label, name=field_label: self._cap_validate(
                    proposed, lim, counter, name
                )
            ),
            "%P",
        )
        entry.configure(validate="key", validatecommand=vcmd)
        counter_label.configure(text=f"{len(entry.get())}/{limit}")

    def _cap_validate(self, proposed: str, limit: int,
                      counter_label: tk.Label, field_label: str) -> bool:
        """validatecommand for a capped entry. Returns False to reject the
        edit; warns in the status bar, throttled so holding a key doesn't
        spam it."""
        if len(proposed) > limit:
            now = time.monotonic()
            if now - self._cap_warned_at > 3.0:
                self._cap_warned_at = now
                self.report_status(
                    f"{field_label} limited to {limit} characters (SAP)", "warning"
                )
            counter_label.configure(text=f"{limit}/{limit}")
            return False
        counter_label.configure(text=f"{len(proposed)}/{limit}")
        return True

    # ---- ATB behaviour -------------------------------------------------
    def _run_atb_goods(self):
        """Run the Create Goods ATB script."""
        self.run_script("atb_goods_btn", title="Create Goods ATB", on_success=self._read_atb_status)

    def _run_atb_services(self):
        """Run the Create Services ATB script."""
        self.run_script(
            "atb_services_btn", title="Create Services ATB", on_success=self._read_atb_status
        )

    def _read_atb_status(self):
        """Translate SAP's S/E/W/A status codes into a report_status() call.
        This is genuinely ATB-specific domain logic (SAP's own status
        vocabulary), so it stays here -- it just no longer duplicates the
        color/icon rendering that report_status() already owns."""
        status_text = self.engine.get_variable("atb_status_text") or ""
        status_type = self.engine.get_variable("atb_status_type") or ""

        kind_by_code = {"S": "success", "E": "error", "W": "warning", "A": "error"}
        if status_type in kind_by_code:
            prefix = "Abort — " if status_type == "A" else ""
            self.report_status(f"{prefix}{status_text}", kind_by_code[status_type])
        elif status_text:
            self.report_status(status_text, "info")
        else:
            self.report_status("ATB script complete", "info")

        if status_type == "S":
            self._record_created_atb(status_text)

    def _history_meta_for(self, order_nr: str) -> tuple[str, str, str]:
        """(short_text, date, workplace) for a history entry. The snapshotted
        meta is only trusted when it belongs to this order -- otherwise the
        entry gets the short text plus blanks (date falls back to the
        recording day in the view)."""
        short_text = self.engine.get_variable("order_short_text") or ""
        meta = getattr(self, "_last_order_meta", {}) or {}
        if meta.get("order") != order_nr:
            return short_text, "", ""
        return short_text, meta.get("date", ""), meta.get("workplace", "")

    def _record_created_atb(self, status_text: str):
        """Persist a freshly created ATB to history (success path only)."""
        nr = parse_atb_number(status_text or "")
        if not nr:
            return
        order_nr = (self.engine.get_variable("order") or "").strip()
        if not order_nr:
            return
        short_text, date, workplace = self._history_meta_for(order_nr)
        kind = "Services" if self._atb_type_var.get() == "D" else "Goods"
        try:
            atb_text = self.desc_var.get() or ""
        except Exception:
            atb_text = self.engine.get_variable("txz01") or ""
        try:
            vendor = self.vendor_var.get() or ""
        except Exception:
            vendor = self.engine.get_variable("lifnr") or ""
        try:
            vendor_name = self._supplier_picked_name()
        except Exception:
            vendor_name = ""
        self._history.ensure_order(
            order_nr, short_text=short_text, date=date, workplace=workplace
        )
        self._history.add_atb(
            order_nr, nr, kind, short_text=atb_text,
            vendor=vendor, vendor_name=vendor_name,
        )
        self._history_pane.refresh()

    def _record_upload(self, doc_nr: str, description: str, filename: str):
        """Persist a successful upload to history."""
        order_nr = (self.engine.get_variable("order") or "").strip()
        if not order_nr:
            return
        short_text, date, workplace = self._history_meta_for(order_nr)
        self._history.ensure_order(
            order_nr, short_text=short_text, date=date, workplace=workplace
        )
        self._history.add_attachment(order_nr, doc_nr, description, filename)
        self._history_pane.refresh()

    def _sync_delivery_date(self):
        """Delivery Date default: the order's Basic Start when an order is
        selected, else today. Runs at build, on every fresh order meta
        (i.e. every lookup fires it), and on Clear -- so the field always
        opens on the order date and never on a stale hardcoded default.
        Returns True when the field value actually changed."""
        if self._delivery_var is None:
            return False
        order_nr = (self.engine.get_variable("order") or "").strip()
        order_date = (self.engine.get_variable("order_basic_start") or "").strip()
        if order_nr and order_date:
            new_value = order_date
        else:
            new_value = datetime.now().strftime("%d.%m.%Y")
        if self._delivery_var.get() == new_value:
            return False
        self._delivery_var.set(new_value)
        return True

    def _flash_delivery_date(self):
        """Paint the Delivery Date header green: the order just rewrote the
        field under the user's hands, and the color asks for a glance at
        it. Stays until the user takes ownership (typing, picking from the
        calendar) or Clear resets the form -- a timed flash would already
        be gone behind the slow attachment phase that follows every
        lookup. Restores itself; safe if the window closes mid-flash."""
        header = getattr(self, "_delivery_header", None)
        if header is None:
            return
        try:
            if header.winfo_exists():
                header.configure(bg=ui_styles.COLORS["success_flash"])
        except tk.TclError:
            pass  # window going away -- nothing to flash

    def _unflash_delivery_date(self, event=None):
        """Back to the normal header face: user took ownership, Clear ran,
        or a test drives it. Doubles as the keystroke/calendar-pick hook."""
        header = getattr(self, "_delivery_header", None)
        if header is None:
            return
        try:
            if header.winfo_exists():
                header.configure(bg=ui_styles.COLORS["bg"])
        except tk.TclError:
            pass

    def _clear_atb_fields(self):
        """Reset all ATB fields to their default values from config."""
        for config_key, _label in ATB_FIELDS:
            entry_cfg = cfg.get_textinput_cfg(self.config_data, config_key)
            default = entry_cfg.get("default_value", "")
            if config_key == "atb_txz01":
                self.desc_var.set(default)
            elif config_key == "atb_lifnr":
                self.vendor_var.set(default)
                self._supplier_combo.set("")
        for entry, (config_key, _label) in zip(self._atb_entries, ATB_GRID_FIELDS):
            if config_key == "atb_eeind":
                continue  # DateEntry: re-synced below, order date else today
            if entry._var is None:
                continue
            entry_cfg = cfg.get_textinput_cfg(self.config_data, config_key)
            entry._var.set(entry_cfg.get("default_value", ""))
        self._sync_delivery_date()
        self._unflash_delivery_date()

        # Programmatic sets bypass keystroke validation -- refresh the SAP
        # 40-char counters explicitly.
        self._desc_counter.configure(text=f"{len(self.desc_var.get())}/40")
        svc_default = cfg.get_textinput_cfg(self.config_data, "atb_ktext1").get("default_value", "")
        self._service_counter.configure(text=f"{len(svc_default)}/40")

        self.report_status("ATB fields cleared", "info")

    # ---- order searcher -------------------------------------------------
    def _open_order_searcher(self):
        """Open the Order Searcher popup (modal). A double-clicked order
        lands in the order field via the engine -- then a lookup fires
        automatically, so the card/grid/attachments can never show the
        previous order's data under the new number. Only when the number
        actually changed: closing via X (or re-picking the same order)
        costs no slow refresh. Delegates to _run_lookup, i.e. the exact
        manual-Lookup path, so debug mode still steps the script."""
        from tools.order_searcher import OrderSearcherWindow

        before = (self.engine.get_variable("order") or "").strip()
        searcher = OrderSearcherWindow(self, self.engine)
        self.wait_window(searcher)
        after = (self.engine.get_variable("order") or "").strip()
        if after and after != before:
            self._lookup_picked_order(after)

    def _lookup_picked_order(self, order_nr: str):
        """Refresh PRH for a freshly picked order: bring the window
        forward (the modal just closed) and run the standard lookup,
        whose watch cursor + narrated status show the refresh."""
        self.lift()
        self.update()
        self._run_lookup()

    # ---- vendor manager -------------------------------------------------
    def _open_vendor_manager(self):
        """Open the vendor CRUD window (modeless, single instance)."""
        from tools.vendor_manager import VendorManagerWindow

        win = getattr(self, "_vendor_win", None)
        if win is not None and win.winfo_exists():
            if win.state() in ("iconic", "withdrawn"):
                win.deiconify()
                win.state("normal")
            win.lift()
            win.focus_force()
            return
        self._vendor_win = VendorManagerWindow(self, on_changed=self._on_vendors_changed)

    def _on_vendors_changed(self):
        """Reload the vendor combobox after a CRUD mutation. Only the value
        list refreshes -- the current field content is left alone."""
        self._reload_suppliers()

    # ---- lookup behaviour (preserved from original) --------------------
    def _run_lookup(self):
        self.config(cursor="watch")
        order_nr = (self.engine.get_variable("order") or "").strip()
        if order_nr:
            self.report_status(f"Loading order {order_nr}...", "info")
        else:
            self.report_status("Running ME5A lookup...", "info")
        self.update()
        self._delete_export_file()
        # Reset the export flag BEFORE running: otherwise a failed script
        # leaves the previous lookup's export_done="1" behind and the
        # failure branch reports the confusing (export_done='1').
        self.engine.set_variable("export_done", "0")
        self._pending_lookup_msg = ""
        self.run_script("prh_lookup_btn", title="PRH Lookup", on_success=self._on_lookup_complete)

    def _on_lookup_row_double_click(self, event):
        """Double-click a lookup result row → open that ATB in SAP (ME53N).

        Resolved by column NAME, not position: columns are user-configurable,
        so values[0] is whatever happens to be first (usually "AanvDt.", a
        date -- which used to land in the order field). The order field is
        never touched here; the Order Search picker owns order transfer.
        Everything SAP does lives in the prh_open_atb_btn script (config)."""
        item = self.tree.identify_row(event.y)
        if not item:
            return
        values = self.tree.item(item, "values")
        if not values:
            return
        columns = list(self.tree["columns"])
        atb_idx = next(
            (i for i, c in enumerate(columns) if c.strip().lower() == "atb"),
            None,
        )
        if atb_idx is None or atb_idx >= len(values):
            self.report_status("No ATB column in this grid", "info")
            return
        atb_nr = values[atb_idx].strip()
        if not atb_nr:
            return
        self._open_atb_in_sap(atb_nr)

    def _open_atb_in_sap(self, atb_nr: str):
        """Display a purchase requisition in SAP. Shared by grid + history
        double-click -- the ME53N work itself lives in prh_open_atb_btn."""
        atb_nr = (atb_nr or "").strip()
        if not atb_nr:
            return
        self.config(cursor="watch")
        self.report_status(f"Opening ATB {atb_nr} in SAP...", "info")
        self.update()
        self.engine.set_variable("prh_atb_number", atb_nr)
        self.run_script(
            "prh_open_atb_btn",
            title=f"Open ATB {atb_nr}",
            on_success=lambda: self._on_open_atb_complete(atb_nr),
        )

    def _open_order_in_sap(self, order_nr: str):
        """Open a work order in IW32 (history double-click). Also puts the
        order in the lookup field, like the Order Search picker does."""
        order_nr = (order_nr or "").strip()
        if not order_nr:
            return
        self.engine.set_variable("order", order_nr)
        self.config(cursor="watch")
        self.report_status(f"Opening order {order_nr} in SAP...", "info")
        self.update()
        self.run_script(
            "prh_open_order_btn",
            title=f"Open Order {order_nr}",
            on_success=lambda: self._on_open_order_complete(order_nr),
        )

    def _on_open_order_complete(self, order_nr: str):
        """Report the IW32 result. Nothing to refresh -- SAP shows the order."""
        self.config(cursor="")
        last_err = getattr(self.engine, "_last_error", None)
        if last_err:
            self.report_status(
                f"Open order {order_nr} failed -- SAP error: {last_err}{self._failing_script_line()}",
                "error",
            )
        else:
            self.report_status(f"Order {order_nr} opened in SAP", "success")

    def _open_history_attachment(self, payload: dict):
        """Download + open a history attachment: switch to its order, refresh
        the live attachment list, then pick the matching doc (handled in
        _populate_attachments_tree via _pending_attach_open)."""
        order_nr = ((payload or {}).get("order") or "").strip()
        if not order_nr:
            return
        self.engine.set_variable("order", order_nr)
        self._pending_lookup_msg = ""
        self._pending_attach_open = {
            "order": order_nr,
            "doc_nr": (payload or {}).get("doc_nr", ""),
            "filename": (payload or {}).get("filename", ""),
        }
        want = payload.get("filename") or payload.get("doc_nr")
        self.report_status(f"Locating {want} on order {order_nr}...", "info")
        self.update()
        self._read_attach_list()

    def _on_open_atb_complete(self, atb_nr: str):
        """Report the ME53N result. Nothing to refresh -- SAP now shows the
        purchase requisition itself."""
        self.config(cursor="")
        last_err = getattr(self.engine, "_last_error", None)
        if last_err:
            self.report_status(
                f"Open ATB {atb_nr} failed -- SAP error: {last_err}{self._failing_script_line()}",
                "error",
            )
        else:
            self.report_status(f"ATB {atb_nr} opened in SAP", "success")

    def _on_lookup_complete(self):
        """Custom success handling for the lookup script: populate the grid
        and refresh attachments. The order short text is auto-updated via bind_var."""
        if self.engine.get_variable("export_done") == "1" and os.path.exists(self._export_filepath):
            # Summary/total rows carry no ATB number -- exclude them so the
            # grid holds only real purchase requisitions.
            self.grid_manager.populate_from_export(self._export_filepath, require_nonempty="ATB")
            count = len(self.tree.get_children())
            cols = list(self.tree["columns"])
            first_status = (self.engine.get_variable("order_system_status") or "").split()
            is_tafs = bool(first_status and first_status[0] == "TAFS")
            if count == 0:
                # No ATB data for this order -- say so explicitly (GridManager
                # already cleared stale rows from any previous lookup). The
                # message is stashed so the attachment refresh below can't
                # silently overwrite it (see _on_attach_count_read /
                # _populate_attachments_tree).
                order_nr = (self.engine.get_variable("order") or "").strip()
                order_label = f" for order {order_nr}" if order_nr else ""
                if is_tafs:
                    msg = (
                        f"No ATB data{order_label} -- order is TAFS "
                        "(technisch afgesloten), reopen required"
                    )
                else:
                    msg = f"No ATB data{order_label}"
                self._pending_lookup_msg = msg
                self.report_status(msg, "warning")
            elif is_tafs:
                self._pending_lookup_msg = ""
                self.report_status(
                    f"Lookup complete: {count} row(s) -- order is TAFS (technisch afgesloten), reopen required",
                    "warning",
                )
            else:
                self._pending_lookup_msg = ""
                self.report_status(
                    f"Lookup complete: {count} row(s), {len(cols)} col(s) from {self._export_file}",
                    "success" if count else "warning",
                )
            self._refresh_attachments()
        else:
            # ME5A export failed (e.g. toolbar button id differs per SAP
            # version) -- clear BOTH grids so no stale previous-order data
            # lingers. Attachments are IW32-based and independent of the
            # failed ME5A export, so still refresh them; the failure message
            # is stashed in _pending_lookup_msg so the attachment refresh
            # can't silently overwrite it.
            self.tree.delete(*self.tree.get_children())
            if hasattr(self, "grid_manager"):
                self.grid_manager.clear()
            for item in self._attachments_tree.get_children():
                self._attachments_tree.delete(item)
            self.config(cursor="")
            # Capture the failing line BEFORE _refresh_attachments runs the
            # next script (which would move the engine pointer).
            last_err = getattr(self.engine, "_last_error", None) or ""
            failing_line = self._failing_script_line()
            failing_raw = self._failing_raw_line()
            order_nr = (self.engine.get_variable("order") or "").strip()
            order_label = f" for order {order_nr}" if order_nr else ""
            # Empty ME5A result shows no ALV grid, so the export button
            # (btn[45]) and everything after it can't be found. That is not
            # a broken lookup -- it simply means the order has no ATBs.
            is_empty_result = any(
                marker in failing_raw
                for marker in ("btn[45]", "SPOPLI-SELFLAG", "DY_PATH", "DY_FILENAME")
            )
            if last_err and not is_empty_result:
                msg = f"Lookup failed{order_label}. SAP error: {last_err}{failing_line}"
                kind = "error"
            else:
                first_status = (self.engine.get_variable("order_system_status") or "").split()
                if first_status and first_status[0] == "TAFS":
                    msg = (
                        f"No ATB data{order_label} -- order is TAFS "
                        "(technisch afgesloten), reopen required"
                    )
                else:
                    msg = f"No ATB data{order_label}"
                kind = "warning"
            self._pending_lookup_msg = msg
            self.report_status(msg, kind)
            self._refresh_attachments()

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

    def _failing_raw_line(self) -> str:
        """Raw script text at the engine pointer ("" if unavailable). Used
        to tell an empty ME5A result (export button missing) apart from a
        genuine lookup failure."""
        try:
            cmds = getattr(self.engine, "commands", [])
            ptr = getattr(self.engine, "pointer", 0)
            if 0 <= ptr < len(cmds):
                return cmds[ptr].raw or ""
        except Exception:
            pass
        return ""

    def _run_reopen_order(self):
        """Reopen a TAFS order via IW32. Guarded: runs only when the first
        status token is TAFS -- the button is hidden otherwise, this is the
        backstop."""
        first = (self.engine.get_variable("order_system_status") or "").split()
        if not first or first[0] != "TAFS":
            self.report_status("Order is not TAFS -- nothing to reopen", "info")
            return
        self.config(cursor="watch")
        self.report_status("Reopening order...", "info")
        self.update()
        self.run_script("prh_reopen_btn", title="Reopen Order", on_success=self._on_reopen_complete)

    def _on_reopen_complete(self):
        """Report the reopen result. The status field already updated itself
        via the engine variable the script wrote -- just confirm + reset."""
        self.config(cursor="")
        last_err = getattr(self.engine, "_last_error", None)
        if last_err:
            self.report_status(
                f"Reopen failed -- SAP error: {last_err}{self._failing_script_line()}", "error"
            )
        else:
            new_status = self.engine.get_variable("order_system_status") or "?"
            self.report_status(f"Order reopened -- status: {new_status} (re-run Lookup)", "success")

    def _delete_export_file(self):
        if os.path.exists(self._export_filepath):
            os.remove(self._export_filepath)

    def _refresh_attachments(self):
        self._read_attach_list()

    @property
    def _export_filepath(self):
        return os.path.join(self._export_path, self._export_file)

    # ---- attachment list via getCellValue (like Print Orders) --------
    def _read_attach_list(self):
        """Start reading the attachment grid via getCellValue loop.
        Clears the treeview immediately so old entries don't linger visually
        while the SAP script runs. Order date/workplace are read first, on
        the IW32 header context, then the attachment count runs -- chained
        as two scripts so a meta failure can never break the count (and a
        count failure never blocks the meta already captured)."""
        # Clear treeview before the SAP script runs
        for item in self._attachments_tree.get_children():
            self._attachments_tree.delete(item)
        self.config(cursor="watch")
        self._attach_rows = []
        self._attach_read_idx = 0
        self.run_script("prh_order_meta_btn", title="Order Details",
                        on_success=self._on_order_meta_done)

    def _on_order_meta_done(self):
        """Snapshot fresh order meta for history -- but only when the meta
        script ran clean. A failed read leaves the previous order's values
        behind, which must never be paired with the current order. Either
        way the attachment count runs next."""
        if not getattr(self.engine, "_last_error", None):
            self._last_order_meta = {
                "order": (self.engine.get_variable("order") or "").strip(),
                "date": self.engine.get_variable("order_basic_start") or "",
                "workplace": self.engine.get_variable("order_workplace") or "",
            }
            # Fresh order date just landed -- the Delivery Date follows it,
            # flashing green when it actually changed. A failed read leaves
            # the field (and any manual edit) alone.
            if self._sync_delivery_date():
                self._flash_delivery_date()
        else:
            # Failed meta read: Start/Workplace stay stale AND the Delivery
            # Date keeps its value by design. Say so via the pending-message
            # slot -- otherwise the follow-up attachment status ("Loaded N
            # attachments") claims all is fine and the date looks broken.
            # A lookup message already stashed there wins (it names the cause).
            if not getattr(self, "_pending_lookup_msg", ""):
                last_err = getattr(self.engine, "_last_error", None) or ""
                self._pending_lookup_msg = (
                    "Order details unreadable -- date not updated. "
                    f"SAP error: {last_err}{self._failing_script_line()}"
                )
        self.run_script("refresh_attachments_btn", on_success=self._on_attach_count_read)

    def _on_attach_count_read(self):
        count = int(self.engine.get_variable("prh_attach_count", "0"))
        if count == 0:
            self.config(cursor="")
            pending_open = getattr(self, "_pending_attach_open", None)
            if pending_open:
                self._pending_attach_open = None
                want = pending_open.get("filename") or pending_open.get("doc_nr")
                self.report_status(
                    f"Attachment {want} not found on order {pending_open['order']}",
                    "warning",
                )
                return
            pending = getattr(self, "_pending_lookup_msg", "")
            self._pending_lookup_msg = ""
            if pending:
                self.report_status(f"{pending} (no attachments)", "warning")
            else:
                self.report_status("No attachments found", "info")
            return
        self._attach_total = count
        self.report_status(f"Loading {count} attachment(s)...", "info")
        self._read_next_attach_row()

    def _read_next_attach_row(self):
        if self._attach_read_idx >= self._attach_total:
            self._populate_attachments_tree()
            return
        self.report_status(f"Reading attachment {self._attach_read_idx + 1} of {self._attach_total}...", "info")
        self.update()
        self.engine.set_variable("prh_read_idx", str(self._attach_read_idx))
        self.run_script("prh_read_attach_row", on_success=self._on_attach_row_read)

    def _on_attach_row_read(self):
        row = {
            "doc_nr": self.engine.get_variable("prh_cell_doknr", ""),
            "description": self.engine.get_variable("prh_cell_description", ""),
            "filename": (
                self.engine.get_variable("prh_cell_filename", "")
                or self.engine.get_variable("prh_cell_doknr", "")
            ),
            "status": "Uploaded",
        }
        self._attach_rows.append(row)
        self._attach_read_idx += 1
        self._read_next_attach_row()

    def _populate_attachments_tree(self):
        for row in self._attach_rows:
            self._attachments_tree.insert("", "end", values=(
                row.get("doc_nr", ""),
                row.get("description", ""),
                row.get("filename", ""),
                row.get("status", ""),
            ))

        # History attachment open: pick the matching live row and download it.
        pending_open = getattr(self, "_pending_attach_open", None)
        if pending_open:
            self._pending_attach_open = None
            idx = next(
                (
                    i
                    for i, r in enumerate(self._attach_rows)
                    if (pending_open.get("doc_nr") and r.get("doc_nr") == pending_open["doc_nr"])
                    or (
                        pending_open.get("filename")
                        and r.get("filename") == pending_open["filename"]
                    )
                ),
                None,
            )
            if idx is None:
                self.config(cursor="")
                want = pending_open.get("filename") or pending_open.get("doc_nr")
                self.report_status(
                    f"Attachment {want} not found on order {pending_open['order']}",
                    "warning",
                )
                return
            filename = self._attach_rows[idx].get("filename") or self._attach_rows[idx].get(
                "doc_nr"
            )
            safe_name = filename.replace("/", "_").replace("\\", "_")
            target_path = os.path.join(self._export_path, safe_name)
            self.config(cursor="watch")
            self.report_status(f"Downloading {filename}...", "info")
            self.update()
            self.engine.set_variable("prh_dl_row", str(idx))
            self.engine.set_variable("prh_dl_target", target_path)
            self.run_script(
                "prh_download_attach",
                title=f"Download {filename}",
                on_success=lambda: self._on_download_complete(target_path),
            )
            return

        self.config(cursor="")
        pending = getattr(self, "_pending_lookup_msg", "")
        self._pending_lookup_msg = ""
        if pending:
            self.report_status(f"{pending} ({len(self._attach_rows)} attachment(s))", "warning")
        else:
            self.report_status(f"Loaded {len(self._attach_rows)} attachment(s)", "info")

    # (Right-click handled by CustomTreeview's built-in _on_right_click)

    # ---- upload handler ----------------------------------------------
    def _on_upload_clicked(self, event=None):
        """Open file picker and immediately upload each selected file."""
        from tkinter import filedialog

        files = filedialog.askopenfilenames(
            title="Select Attachment",
            filetypes=[("All files", "*.*"), ("PDF files", "*.pdf"), ("Images", "*.png *.jpg *.jpeg")],
            parent=self,
        )
        if files:
            uploaded = 0
            failed = 0
            for filepath in files:
                if self._upload_file(filepath):
                    uploaded += 1
                else:
                    failed += 1
            if failed == 0:
                self.report_status(f"{uploaded} attachment(s) uploaded", "success")
            else:
                self.report_status(f"{uploaded} uploaded, {failed} failed", "error")
            self.lift()

    def _on_refresh_attachments_clicked(self, event=None):
        self._read_attach_list()

    def _show_upload_meta_popup(self, filename: str) -> tuple[str, str] | None:
        """Show a popup asking for document number and description before upload.
        Returns (doc_nr, description) or None if cancelled."""
        popup = tk.Toplevel(self)
        popup.withdraw()
        popup.title(f"Upload Metadata — {filename}")
        popup.transient(self)
        popup.grab_set()
        popup.resizable(False, False)

        styled_label(popup, "Document number:").pack(padx=10, pady=(10, 0), anchor="w")
        doc_var = tk.StringVar()
        entry_doc = tk.Entry(popup, textvariable=doc_var, width=40, font=ui_styles.FONTS["body"])
        entry_doc.pack(padx=10, pady=(2, 8), fill="x")
        entry_doc.focus_set()

        styled_label(popup, "Description:").pack(padx=10, anchor="w")
        desc_var = tk.StringVar()
        entry_desc = tk.Entry(popup, textvariable=desc_var, width=40, font=ui_styles.FONTS["body"])
        entry_desc.pack(padx=10, pady=(2, 10), fill="x")

        result = None

        def on_ok():
            nonlocal result
            result = (doc_var.get(), desc_var.get())
            popup.destroy()

        def on_cancel():
            popup.destroy()

        btn_frame = tk.Frame(popup, bg=ui_styles.COLORS["bg"])
        btn_frame.pack(padx=10, pady=(0, 10))

        ok_btn = ui_styles.styled_button(btn_frame, "Upload", command=on_ok, width=10)
        ok_btn.pack(side="left", padx=(0, 6))
        cancel_btn = ui_styles.styled_button(btn_frame, "Cancel", command=on_cancel, width=10)
        cancel_btn.pack(side="left")

        # Keyboard flow: Enter confirms (focus starts in Document number),
        # Escape cancels -- clicking is not the only way out.
        popup.bind("<Return>", lambda e: on_ok())
        popup.bind("<Escape>", lambda e: on_cancel())

        show_centered(popup, ui_styles.SUPPORT_COLOR)
        self.wait_window(popup)
        return result

    def _on_attachment_double_click(self, event):
        """Handle double-click on attachment row: download and open the file."""
        item = self._attachments_tree.identify_row(event.y)
        if not item:
            return

        values = self._attachments_tree.item(item, "values")
        filename = values[2] if len(values) > 2 else ""

        if not filename:
            return

        safe_name = filename.replace("/", "_").replace("\\", "_")
        target_path = os.path.join(self._export_path, safe_name)

        self.config(cursor="watch")
        self.report_status(f"Downloading {filename}...", "info")
        self.update()
        self.engine.set_variable("prh_dl_row", str(self._attachments_tree.index(item)))
        self.engine.set_variable("prh_dl_target", target_path)
        self.run_script(
            "prh_download_attach",
            title=f"Download {filename}",
            on_success=lambda: self._on_download_complete(target_path),
        )

    def _on_download_complete(self, filepath: str):
        self.config(cursor="")
        if self.engine.get_variable("export_done") == "1":
            # Mock mode: create a dummy placeholder
            if not os.path.exists(filepath):
                try:
                    with open(filepath, "w") as f:
                        f.write(f"[Mock download: {os.path.basename(filepath)}]\n")
                except OSError:
                    pass
            if os.path.exists(filepath):
                self.report_status(f"Opened {os.path.basename(filepath)}", "success")
                os.startfile(filepath)
            else:
                self.report_status("Download completed but file not found", "warning")
        else:
            self.report_status("Download failed", "error")

    def _on_upload_click(self, event):
        """Handle click on the drag-and-drop zone to open file picker."""
        self._on_upload_clicked()



    def _upload_file(self, filepath: str) -> bool:
        """Upload a single file immediately: show popup, run SAP script, add to treeview.
        Returns True on success, False on failure or cancellation."""
        filename = os.path.basename(filepath)

        # Show metadata popup for doc_nr + description
        meta = self._show_upload_meta_popup(filename)
        if meta is None:
            return False

        doc_nr, description = meta

        # Show wait cursor during SAP operation
        self.config(cursor="watch")
        self.report_status(f"Uploading {filename}...", "info")
        self.update()

        # Copy file to tempexport folder
        dest_path = os.path.join(self._export_path, filename)
        if os.path.exists(dest_path):
            os.remove(dest_path)
        import shutil
        shutil.copy2(filepath, dest_path)

        # Set variables for the SAP script
        self.engine.set_variable("file_dir", os.path.dirname(dest_path))
        self.engine.set_variable("file_name", os.path.basename(dest_path))
        self.engine.set_variable("doc_nr", doc_nr)
        self.engine.set_variable("description", description)

        # Run upload script
        self.run_script("proforma_upload_btn", title="Upload: " + filename)

        # In debug mode, wait for debugger to close before continuing
        if self._toolbar.debug_mode.get():
            debugger = self._toolbar.debugger_window
            if debugger and debugger.winfo_exists():
                debugger.wait_window()

        # Check upload status from SAP script
        upload_status = self.engine.variables.get("upload_status", "Unknown")
        msg_type = self.engine.variables.get("upload_status_type", "")
        is_success = msg_type in ("S", "W")

        if is_success:
            status_text = "Uploaded"
            if os.path.exists(dest_path):
                os.remove(dest_path)
        else:
            status_text = f"Failed: {upload_status[:40]}"

        self._attachments_tree.insert("", "end", values=(
            doc_nr, description, filename, status_text
        ))

        if is_success:
            self._record_upload(doc_nr, description, filename)

        self.config(cursor="")
        return is_success

    def _on_file_drop(self, event):
        """Handle drops on the upload zone: Explorer file paths upload
        directly; anything else (e.g. Outlook attachments, which never
        arrive as paths) is read via Outlook COM."""
        data = getattr(event, "data", "") or ""
        if not isinstance(data, str):
            data = str(data)
        now = time.monotonic()
        # The guard is only a de-dup for the zone + label both firing once, so
        # a window built without __init__ (tests) simply has no previous drop.
        last_data, last_at = getattr(self, "_last_drop", ("", 0.0))
        if data == last_data and now - last_at < 1.0:
            return  # zone + label both fired for one drop
        self._last_drop = (data, now)

        try:
            candidates = self._drop_zone.tk.splitlist(data)
        except tk.TclError:
            candidates = []
        files = [f for f in candidates if os.path.isfile(f)]
        if files:
            self._upload_paths(files)
            return
        if not data.strip() or self._looks_like_outlook_drop(data):
            # No usable paths: Outlook drop (FileGroupDescriptor, not
            # files -- its payload rarely survives as text, if at all).
            paths, error = outlook_drop.read_outlook_selection_attachments(self._export_path)
            if error:
                self.report_status(
                    f"{error} -- drag the file via Explorer instead", "warning"
                )
                return
            if paths:
                self._upload_paths(paths)
                return
        self.report_status("Drop contained no files", "warning")

    @staticmethod
    def _looks_like_outlook_drop(data: str) -> bool:
        """True when drop data smells like Outlook's descriptor formats."""
        lowered = data.lower()
        return "filegroup" in lowered or "descriptor" in lowered or "outlook" in lowered

    def _upload_paths(self, files) -> None:
        """Upload each path through the per-file flow + summary status."""
        uploaded = 0
        failed = 0
        for filepath in files:
            if self._upload_file(filepath):
                uploaded += 1
            else:
                failed += 1

        if uploaded + failed > 0:
            if failed == 0:
                self.report_status(f"{uploaded} attachment(s) uploaded", "success")
            else:
                self.report_status(f"{uploaded} uploaded, {failed} failed", "error")



