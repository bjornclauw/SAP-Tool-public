"""SapToolbar -- the root window.

Layout is 100% hardcoded here; only text, default values, and scripts come
from config/toolbar_config.json. If you want a 5th shortcut button, add a
row/column here -- don't make the layout data-driven, that complexity is
exactly what this rework removes.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import tkinterdnd2

from core import config as cfg
from core import console, user_settings
from core.com_backend import RealSapBackend
from core.commands import sanitize_sap_number
from core.engine import SapScriptEngine
from core.fake_com import FakeComClient
from core.sap_worker import SapWorker
from tools.sap_import.models import FAILED_STATUSES, FINISHED_STATUSES, PENDING_STATUSES

from . import ui_styles
from .custom_menu import CustomMenuBar, MenuBarItem
from .custom_widgets import CustomButton, CustomEntry, CustomLabel, ScriptRunnerMixin, _get_config
from .debugger import DebuggerWindow
from .dock_manager import DockManager, load_logo_image
from .ui_styles import (
    card_frame,
    styled_frame,
    styled_label,
    styled_status_bar,
)
from .window_chrome import (
    apply_titlebar_color,
    center_on_toolbar,
    correct_for_decorations,
)

QUICK_OPEN_GROUPS = [
    ("toolbar_qo1", "Quick Open 1"),
    ("toolbar_qo2", "Melding"),
    ("toolbar_qo3", "Revisie"),
]
SHORTCUT_BUTTONS = [
    "toolbar_sc1", "toolbar_sc2", "toolbar_sc3", "toolbar_sc4",
    "toolbar_sc5", "toolbar_sc6",
]

# Docked-mode helper rail: (tool key, badge, label, opener method, colour).
HELPER_RAIL_ITEMS = [
    ("purchase_request", "PR", "Purchase Request",
     "_open_purchase_request", ui_styles.TOOL_COLORS["purchase_request"]),
    ("print_orders", "PO", "Process Orders",
     "_open_print_orders", ui_styles.TOOL_COLORS["print_orders"]),
    ("sap_import", "SI", "SAP Import",
     "_open_sap_import", ui_styles.TOOL_COLORS["sap_import"]),
    ("revisions", "RR", "Revisions",
     "_open_revisions_register", ui_styles.SUPPORT_COLOR),
]
HELPER_RAIL_WIDTH = 156
# Quick-open fields holding SAP numbers (werkorder, melding): pasted
# values get hardened to the digit run (see sanitize_sap_number).
# Revisie (toolbar_qo3) is a free code -- leaders/whitespace stripped only.
SAP_NUMBER_FIELDS = {"toolbar_qo1", "toolbar_qo2"}
OPEN_APPS = [
    ("purchase_request", "PRH", "_open_purchase_request"),
    ("print_orders", "Print", "_open_print_orders"),
    ("sap_import", "Import", "_open_sap_import"),
]
# Frames for the Import marker while auto upload runs (Braille spinner).
IMPORT_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class SapToolbar(ScriptRunnerMixin, tkinterdnd2.TkinterDnD.Tk):
    def __init__(self):
        super().__init__()
        self.title("SAP Work Order Tool")
        self.config(bg=ui_styles.COLORS["bg"])

        # Hide window while everything gets built (prevents flicker).
        self.withdraw()
        self.update_idletasks()
        self.resizable(False, False)

        self.config_data = _get_config()

        settings = user_settings.load_user_settings()
        self.fake_sap = tk.BooleanVar(value=bool(settings.get("fake_sap", False)))
        self.always_on_top = tk.BooleanVar(value=bool(settings.get("always_on_top", False)))
        self.debug_mode = tk.BooleanVar(value=bool(settings.get("debug_mode", False)))
        self.power_user = tk.BooleanVar(value=bool(settings.get("power_user", False)))
        self.show_console = tk.BooleanVar(
            value=bool(settings.get("show_console", False)) or console.has_console()
        )
        if settings.get("show_console", False):
            console.show_console()

        self.engine = SapScriptEngine(self._backend_factory_for(self.fake_sap.get()))
        # Connection policy: attach only for the duration of a run and release
        # when it ends (engine.begin_run/end_run), holding nothing between.
        # The startup probe below settles the connection label without holding
        # a handle; debug sessions hold the attach (engine.start/end_debug_session).
        self.engine.worker.start()
        self._worker_fake = bool(self.fake_sap.get())
        # Any programmatic set (tests, scripts) switches the backend too; only
        # the menu path persists the choice (see _toggle_fake).
        self.fake_sap.trace_add("write", lambda *_: self._apply_fake_mode())
        # One combined SAP indicator: connection state + script activity.
        self._sap_connected = False
        self._sap_active = False
        self._sap_import_ok: bool | None = None
        self._sap_activity_id = None
        self._sap_activity_frame = 0
        self.engine.run_state_callbacks.append(self._on_sap_activity)

        self.debugger_window: DebuggerWindow | None = None
        self.tool_windows: dict[str, tk.Toplevel] = {}
        self._widget_registry: dict[str, tk.Widget] = {}  # Maps component_id -> widget
        self._poll_id = None  # for after() polling
        self._open_app_buttons: dict[str, tk.Button] = {}
        self._open_chip_close: dict[str, tk.Button] = {}
        self.dock = DockManager(
            self, settings,
            save_callback=self._save_dock_meta,
            on_decorated=self._set_titlebar_color,
        )

        self._build_menu()
        self._build_quick_open_row()
        self._build_shortcuts_row()
        self._build_apps_tray()
        self._build_helper_rail()
        self._build_status_bar()

        self._set_app_icon()  # logo as window/taskbar icon, then reveal
        self._set_titlebar_color()  # Apply pywinstyles color, then reveal
        self.deiconify()
        self.dock.dock_enabled.trace_add(
            "write", lambda *_: self._apply_chrome_layout())
        self._apply_chrome_layout()
        self.dock.apply()  # snap to top if dock was persisted

        self._start_polling()
        # One-shot startup probe: attach once and release immediately, purely
        # to settle the connection label ("SAP ready" / "SAP not running").
        # It holds nothing; every run attaches on demand instead.
        self._probe_attach()

        # Cleanup on close
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_app_icon(self):
        """Brand the window + taskbar with the logo. iconphoto(default=True)
        makes it the app-wide default, so every tool window opened later
        inherits it -- one call covers toolbar and helpers. The image must
        stay referenced (self._icon_img) or Tk drops it."""
        try:
            icon_img = load_logo_image(64, master=self)
            if icon_img is None:
                return
            self._icon_img = icon_img
            self.iconphoto(True, icon_img)
        except Exception:
            pass

    def _set_titlebar_color(self):
        """Set Windows title bar color to match primary color. Nothing else
        lives in here -- it used to secretly run the entire app bootstrap
        because of an indentation slip; that's fixed now."""
        try:
            import pywinstyles

            pywinstyles.change_header_color(self, color=ui_styles.COLORS["primary"])
            pywinstyles.change_title_color(self, color="white")
        except Exception:
            pass

    # ---- layout --------------------------------------------------------
    def _build_quick_open_row(self):
        row = styled_frame(self)
        row.pack(
            fill="x",
            padx=ui_styles.SPACING["comfortable"],
            pady=(ui_styles.SPACING["comfortable"], ui_styles.SPACING["normal"]),
        )
        self._quick_open_row = row

        # Docked-mode helper drawer toggle (hidden while undocked).
        self._helper_toggle = tk.Button(
            row,
            text="☰",
            font=ui_styles.FONTS["header"],
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text"],
            activebackground=ui_styles.COLORS["surface_hover"],
            activeforeground=ui_styles.COLORS["text"],
            relief="flat", bd=0, highlightthickness=0,
            padx=4, pady=0, cursor="hand2",
            command=self._toggle_helper_rail,
        )

        # Brand home: the logo fronts the quick-open row, left of the
        # Werkorder field, and parents the open-helper tray -- the tray
        # unfolds beneath the shortcuts card, and the logo pulses as it
        # opens/closes.
        logo_img = load_logo_image(28, master=row)
        if logo_img is not None:
            self._logo_img = logo_img  # hold a ref while shown
            logo = tk.Label(
                row, image=logo_img, bg=ui_styles.COLORS["bg"], cursor="hand2",
            )
        else:
            self._logo_img = None
            logo = tk.Label(
                row, text="BC", font=ui_styles.FONTS["header"],
                bg=ui_styles.COLORS["bg"], fg=ui_styles.COLORS["primary"],
                cursor="hand2",
            )
        logo.pack(side="left", anchor="s",
                  padx=(0, ui_styles.SPACING["comfortable"]),
                  pady=(ui_styles.SPACING["tight"], ui_styles.SPACING["tight"]))
        logo.bind("<Double-Button-1>", lambda _e: self._toggle_dock_mode())
        self._logo_label = logo
        self._logo_pulse_id = None

        for i, (btn_id, default_header) in enumerate(QUICK_OPEN_GROUPS):
            group = styled_frame(row)
            group.pack(side="left")

            # Header label
            header_text = cfg.get_header_cfg(self.config_data, btn_id).get("text", default_header)
            header_label = CustomLabel(
                group,
                "headers",
                f"{btn_id}_header",
                text=header_text,
                font=ui_styles.FONTS["header"],
            )
            header_label.pack(anchor="w")
            self._widget_registry[f"{btn_id}_header"] = header_label

            # Entry + button row -- one line wires this field into the
            # engine (initial value, live sync both ways, auto-resync
            # before every run_script() call). See ScriptRunnerMixin.
            entry_var = self.bind_entry(btn_id)
            # Paste-hardening for SAP numbers: the trace runs after the
            # mixin's engine sync, so the engine always ends up with the
            # cleaned value too. Equality check is the re-entrancy guard.
            digits_only = btn_id in SAP_NUMBER_FIELDS
            entry_var.trace_add(
                "write",
                lambda *_a, v=entry_var, d=digits_only: self._sanitize_entry(v, d),
            )

            entry_frame = styled_frame(group)
            entry_frame.pack(anchor="w", pady=(ui_styles.SPACING["tight"], 0))
            if btn_id == "toolbar_qo3":
                # Revisie: an editable suggestion list, not a closed set.
                entry = ttk.Combobox(
                    entry_frame, textvariable=entry_var, width=21, state="normal"
                )
                self._refresh_rev_suggestions(entry)
                entry.bind("<Button-1>", lambda _e, c=entry: self._refresh_rev_suggestions(c))
                entry.bind("<FocusIn>", lambda _e, c=entry: self._refresh_rev_suggestions(c))
            else:
                entry = CustomEntry(
                    entry_frame, "textinput", f"{btn_id}_entry", textvariable=entry_var, width=23
                )
            entry.pack(side="left")
            entry.bind("<Return>", lambda e, bid=btn_id: self.run_script(bid))
            self._widget_registry[f"{btn_id}_entry"] = entry

            btn_cfg = cfg.get_button_cfg(self.config_data, btn_id)
            btn = CustomButton(
                entry_frame,
                "buttons",
                f"{btn_id}_btn",
                text=btn_cfg.get("text", "Go"),
                width=6,
                command=lambda bid=btn_id: self.run_script(bid),
                bg=ui_styles.COLORS["primary"],
                fg="#ffffff",
                activebackground=ui_styles.COLORS["primary_hover"],
            )
            btn.pack(side="left", padx=(ui_styles.SPACING["tight"], 0))
            self._widget_registry[f"{btn_id}_btn"] = btn

            # Separator between groups (except after last)
            if i < len(QUICK_OPEN_GROUPS) - 1:
                ttk.Separator(row, orient="vertical").pack(
                    side="left",
                    fill="y",
                    padx=ui_styles.SPACING["comfortable"],
                    pady=ui_styles.SPACING["normal"],
                )

    def _refresh_rev_suggestions(self, combo):
        """Reload the current week's register into the Revisie dropdown."""
        try:
            from tools.sap_import.revisions import get_revisions
            combo.configure(values=get_revisions())
        except Exception:
            pass

    def _sanitize_entry(self, var: tk.StringVar, digits_only: bool):
        """Trace hook: clean pasted junk (spaces, bullets, trailing prose)
        out of SAP number fields the moment it lands."""
        try:
            raw = var.get()
        except tk.TclError:
            return
        clean = sanitize_sap_number(raw, digits=digits_only)
        if clean != raw:
            try:
                var.set(clean)
            except tk.TclError:
                pass

    def _build_shortcuts_row(self):
        row = styled_frame(self)
        row.pack(
            fill="x",
            padx=ui_styles.SPACING["comfortable"],
            pady=(ui_styles.SPACING["normal"], ui_styles.SPACING["comfortable"]),
        )
        self._shortcuts_row = row

        # Shortcuts live here label-first now that the logo fronts the
        # quick-open row; the tray still unfolds directly beneath.
        head = styled_frame(row)
        head.pack(side="left")
        self._shortcuts_head = head

        # Shortcuts label and buttons
        shortcuts_frame = styled_frame(head)
        shortcuts_frame.pack(side="left")
        styled_label(shortcuts_frame, "SHORTCUTS", font=ui_styles.FONTS["header"]).pack(anchor="w")
        btn_row = styled_frame(shortcuts_frame)
        btn_row.pack(anchor="w", pady=(ui_styles.SPACING["tight"], 0))
        for btn_id in SHORTCUT_BUTTONS:
            btn_cfg = cfg.get_button_cfg(self.config_data, btn_id)
            btn = CustomButton(
                btn_row,
                "buttons",
                btn_id,
                text=btn_cfg.get("text", btn_id),
                width=14,
                command=lambda b=btn_id: self.run_script(b),
            )
            btn.pack(side="left", padx=(0, ui_styles.SPACING["normal"]))
            self._widget_registry[btn_id] = btn

    def _build_apps_tray(self):
        """Collapsible open-helper tray, parented to the logo in the
        shortcuts header above: it stays UNPACKED (zero footprint, no
        blank card) while no helper is open, and unfolds downward from
        under the shortcuts card on first open. Markers size to their
        text and only exist while their window is open."""
        tray = card_frame(self)
        # Deliberately not packed -- _refresh_open_apps packs it on
        # first open and unpacks it again on last close.
        self._apps_tray = tray
        self._tray_anim_id = None
        self._tray_anim_gen = 0
        self._import_spin_id = None
        self._import_spin_i = 0
        inner = styled_frame(tray, bg=ui_styles.COLORS["surface"])
        inner.pack(fill="x", padx=ui_styles.SPACING["normal"],
                   pady=ui_styles.SPACING["tight"])
        self._apps_tray_inner = inner
        styled_label(
            inner, "OPEN", font=ui_styles.FONTS["header"], bg=ui_styles.COLORS["surface"]
        ).pack(side="left", padx=(0, ui_styles.SPACING["tight"]))
        for key, title, opener in OPEN_APPS:
            marker = CustomButton(
                inner, "buttons", f"openapp_{key}", text=f"● {title}",
                command=lambda k=key, o=opener: self._on_marker_click(k, o),
                editable=False,
                bg=ui_styles.COLORS["success_light"],
            )
            # Packing is owned by _refresh_open_apps: markers size to their
            # text and only exist while their window is open, so helpers
            # never clip no matter how many are open.
            self._open_app_buttons[key] = marker
            closer = tk.Button(
                inner, text="✕", width=2, relief="flat", bd=0,
                bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text_secondary"],
                activebackground=ui_styles.COLORS["surface"],
                activeforeground=ui_styles.COLORS["error"],
                font=(ui_styles.FONTS["body"][0], 7),
                cursor="hand2",
                command=lambda k=key: self._close_tool_window(k),
            )
            self._open_chip_close[key] = closer
        self._refresh_open_apps(animate=False)

    # ---- docked helper rail --------------------------------------------
    def _build_helper_rail(self):
        self._helper_rail_visible = False
        self._helper_rail = tk.Frame(
            self,
            bg=ui_styles.COLORS["surface"],
            highlightthickness=1,
            highlightbackground=ui_styles.COLORS["border_dark"],
        )
        header = tk.Frame(self._helper_rail, bg=ui_styles.COLORS["surface"])
        header.pack(fill="x", padx=8, pady=(6, 4))
        tk.Label(
            header, text="Helpers", font=ui_styles.FONTS["header"],
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
            anchor="w",
        ).pack(side="left")
        tk.Button(
            header, text="×", command=self._close_helper_rail,
            font=ui_styles.FONTS["body"],
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text_secondary"],
            activebackground=ui_styles.COLORS["surface_hover"],
            activeforeground=ui_styles.COLORS["error"],
            relief="flat", bd=0, highlightthickness=0,
            width=2, cursor="hand2",
        ).pack(side="right")

        for _key, badge, label, opener, color in HELPER_RAIL_ITEMS:
            row = tk.Frame(
                self._helper_rail, bg=ui_styles.COLORS["surface"],
                cursor="hand2")
            row.pack(fill="x", padx=6, pady=1)
            badge_lbl = tk.Label(
                row, text=badge, bg=color, fg="#ffffff",
                font=(ui_styles.FONTS["body"][0], 8, "bold"),
                width=3, padx=2, pady=1,
            )
            badge_lbl.pack(side="left", padx=(0, 6))
            text_lbl = tk.Label(
                row, text=label, anchor="w", font=ui_styles.FONTS["body"],
                bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
            )
            text_lbl.pack(side="left", fill="x", expand=True)
            for widget in (row, badge_lbl, text_lbl):
                widget.bind(
                    "<Button-1>", lambda _e, o=opener: self._launch_helper(o))
                widget.bind(
                    "<Enter>",
                    lambda _e, r=row, t=text_lbl: self._hover_helper_row(
                        r, t, True))
                widget.bind(
                    "<Leave>",
                    lambda _e, r=row, t=text_lbl: self._hover_helper_row(
                        r, t, False))

        tk.Frame(
            self._helper_rail, height=1,
            bg=ui_styles.COLORS["border"],
        ).pack(fill="x", padx=8, pady=4)

        gear = tk.Frame(
            self._helper_rail, bg=ui_styles.COLORS["surface"], cursor="hand2")
        gear.pack(fill="x", padx=6, pady=(0, 4))
        gear_badge = tk.Label(
            gear, text="⚙", bg=ui_styles.SUPPORT_COLOR, fg="#ffffff",
            font=(ui_styles.FONTS["body"][0], 8, "bold"),
            width=3, padx=2, pady=1,
        )
        gear_badge.pack(side="left", padx=(0, 6))
        gear_text = tk.Label(
            gear, text="Settings", anchor="w", font=ui_styles.FONTS["body"],
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
        )
        gear_text.pack(side="left", fill="x", expand=True)
        for widget in (gear, gear_badge, gear_text):
            widget.bind("<Button-1>", lambda _e: self._open_dock_settings())
            widget.bind(
                "<Enter>",
                lambda _e, r=gear, t=gear_text: self._hover_helper_row(
                    r, t, True))
            widget.bind(
                "<Leave>",
                lambda _e, r=gear, t=gear_text: self._hover_helper_row(
                    r, t, False))

        self._helper_rail.place_forget()
        self.bind("<Escape>", lambda _e: self._close_helper_rail(), "+")
        self.bind("<Unmap>", lambda _e: self._close_helper_rail(), "+")

    def _hover_helper_row(self, row, label, active):
        bg = (ui_styles.COLORS["surface_hover"] if active
              else ui_styles.COLORS["surface"])
        try:
            row.configure(bg=bg)
            label.configure(bg=bg)
        except tk.TclError:
            pass

    def _apply_chrome_layout(self):
        if not hasattr(self, "_helper_toggle") or not hasattr(self, "menu_bar"):
            return
        docked = bool(self.dock.dock_enabled.get())
        if docked:
            if self.menu_bar.frame.winfo_manager():
                self.menu_bar.frame.pack_forget()
            if not self._helper_toggle.winfo_manager():
                self._helper_toggle.pack(
                    side="left", anchor="s", before=self._logo_label,
                    padx=(0, ui_styles.SPACING["tight"]),
                    pady=(0, ui_styles.SPACING["tight"]))
        else:
            self._close_helper_rail()
            if self._helper_toggle.winfo_manager():
                self._helper_toggle.pack_forget()
            if not self.menu_bar.frame.winfo_manager():
                self.menu_bar.frame.pack(
                    side="top", fill="x", before=self._quick_open_row)
        self.update_idletasks()

    def _toggle_helper_rail(self):
        if self._helper_rail_visible:
            self._close_helper_rail()
        else:
            self._open_helper_rail()

    def _open_helper_rail(self):
        if not self.dock.dock_enabled.get():
            return
        self._helper_rail.place(
            x=0, y=0, width=HELPER_RAIL_WIDTH, relheight=1)
        self._helper_rail.lift()
        self._helper_rail_visible = True
        self._helper_toggle.configure(
            bg=ui_styles.COLORS["primary"], fg="#ffffff",
            activebackground=ui_styles.COLORS["primary_hover"],
            activeforeground="#ffffff")

    def _close_helper_rail(self):
        if not getattr(self, "_helper_rail", None):
            return
        self._helper_rail.place_forget()
        self._helper_rail_visible = False
        if hasattr(self, "_helper_toggle"):
            self._helper_toggle.configure(
                bg=ui_styles.COLORS["surface"],
                fg=ui_styles.COLORS["text"],
                activebackground=ui_styles.COLORS["surface_hover"],
                activeforeground=ui_styles.COLORS["text"])

    def _launch_helper(self, opener_name):
        self._close_helper_rail()
        if opener_name == "_open_revisions_register":
            self._open_revisions_register()
            return
        key = opener_name.removeprefix("_open_")
        self._focus_or_open(key, opener_name)

    def _open_dock_settings(self):
        self._close_helper_rail()
        win = getattr(self, "_dock_settings_win", None)
        if win is not None and win.winfo_exists():
            win.deiconify()
            win.lift()
            win.focus_force()
            return
        win = tk.Toplevel(self)
        win.withdraw()
        win.title("Settings")
        win.resizable(False, False)
        win.configure(bg=ui_styles.COLORS["bg"])
        body = styled_frame(win)
        body.pack(fill="both", expand=True, padx=12, pady=(12, 6))
        toggles = [
            ("Always on Top", self.always_on_top, self._toggle_always_on_top),
            ("Debug Mode", self.debug_mode, self._toggle_debug_mode),
            ("Power User", self.power_user, self._toggle_power_user),
            ("Debug Console", self.show_console, self._toggle_console),
            ("Fake SAP", self.fake_sap, self._toggle_fake),
            ("Dock top", self.dock.dock_enabled, self._toggle_dock),
            ("Auto-hide", self.dock.autohide, self._toggle_autohide),
        ]
        for text, var, command in toggles:
            tk.Checkbutton(
                body, text=text, variable=var, command=command,
                font=ui_styles.FONTS["body"], anchor="w",
                bg=ui_styles.COLORS["bg"], fg=ui_styles.COLORS["text"],
                activebackground=ui_styles.COLORS["bg"],
                activeforeground=ui_styles.COLORS["text"],
                selectcolor=ui_styles.COLORS["surface"],
                highlightthickness=0, bd=0,
            ).pack(fill="x", pady=1)
        buttons = styled_frame(win)
        buttons.pack(fill="x", padx=12, pady=(4, 12))
        ui_styles.styled_button(
            buttons, "Check for updates…", command=self._open_update_modal
        ).pack(side="left")
        ui_styles.styled_button(
            buttons, "Quit", command=self._on_close
        ).pack(side="right")
        ui_styles.styled_button(
            buttons, "Close", command=win.destroy
        ).pack(side="right", padx=(0, 6))
        win.bind("<Escape>", lambda _e: win.destroy(), "+")
        self._dock_settings_win = win
        center_on_toolbar(win, self)
        win.deiconify()
        win.lift()
        win.focus_force()
        correct_for_decorations(win, self)
        apply_titlebar_color(win, ui_styles.SUPPORT_COLOR)

    def _cancel_tray_anim(self):
        self._tray_anim_gen += 1
        if self._tray_anim_id is not None:
            try:
                self.after_cancel(self._tray_anim_id)
            except Exception:
                pass
            self._tray_anim_id = None

    def _settle_tray(self):
        """Steady state: propagation on AND an explicit full height, so a
        height left over from a cancelled grow can never stick the tray
        at 1px (packed but never mapped)."""
        tray = getattr(self, "_apps_tray", None)
        if tray is None:
            return
        try:
            tray.pack_propagate(True)
            tray.update_idletasks()
            tray.configure(height=tray.winfo_reqheight())
        except tk.TclError:
            pass

    def _pulse_logo(self):
        """The 'logo opens it' cue: a quick highlight flash as the tray
        unfolds beneath it."""
        logo = getattr(self, "_logo_label", None)
        if logo is None:
            return

        def settle():
            try:
                logo.configure(bg=ui_styles.COLORS["bg"])
            except tk.TclError:
                pass

        try:
            logo.configure(bg=ui_styles.COLORS["primary"])
            if self._logo_pulse_id is not None:
                try:
                    self.after_cancel(self._logo_pulse_id)
                except Exception:
                    pass
            self._logo_pulse_id = self.after(140, settle)
        except tk.TclError:
            pass

    def _refresh_open_apps(self, animate: bool = True):
        """Show a marker + ✕ only while its helper window is open; unfold
        the tray on first open, collapse it away on last close."""
        open_keys = []
        for key, _title, _opener in OPEN_APPS:
            marker = self._open_app_buttons.get(key)
            closer = self._open_chip_close.get(key)
            if marker is None or closer is None:
                continue
            try:
                if not marker.winfo_exists():
                    continue
            except tk.TclError:
                continue
            win = self.tool_windows.get(key)
            is_open = False
            if win is not None:
                try:
                    is_open = bool(win.winfo_exists())
                except tk.TclError:
                    is_open = False
            if is_open:
                open_keys.append(key)
        if "sap_import" in open_keys:
            self._ensure_import_spinner()
        else:
            self._stop_import_spinner()
        tray = getattr(self, "_apps_tray", None)
        if tray is None:
            return
        try:
            tray_mapped = tray.winfo_manager() != ""
        except tk.TclError:
            return
        if open_keys:
            for key in open_keys:
                self._pack_chip(key)
            for key in list(self._open_app_buttons):
                if key not in open_keys:
                    self._unpack_chip(key)
            if not tray_mapped:
                try:
                    status = getattr(self, "_status_bar", None)
                    pack_opts: dict = {
                        "fill": "x",
                        "padx": ui_styles.SPACING["comfortable"],
                        "pady": (0, ui_styles.SPACING["normal"]),
                    }
                    if status is not None:
                        # Belt and suspenders with the bottom-pinned
                        # statusbar: the tray always lands directly above it.
                        pack_opts["before"] = status
                    tray.pack(**pack_opts)
                except tk.TclError:
                    return
                self._pulse_logo()
                if animate:
                    self._grow_tray()
            else:
                self._cancel_tray_anim()
                self._settle_tray()
        elif tray_mapped:
            self._pulse_logo()
            if animate:
                self._shrink_tray()
            else:
                self._cancel_tray_anim()
                for key in list(self._open_app_buttons):
                    self._unpack_chip(key)
                try:
                    tray.pack_propagate(True)
                    tray.pack_forget()
                except tk.TclError:
                    pass
        else:
            for key in list(self._open_app_buttons):
                self._unpack_chip(key)

    def _pack_chip(self, key: str):
        try:
            marker = self._open_app_buttons.get(key)
            closer = self._open_chip_close.get(key)
            if marker is not None and marker.winfo_manager() == "":
                marker.pack(side="left", padx=(0, 2))
            if closer is not None and closer.winfo_manager() == "":
                closer.pack(side="left", padx=(0, ui_styles.SPACING["tight"]))
        except tk.TclError:
            pass

    def _unpack_chip(self, key: str):
        try:
            marker = self._open_app_buttons.get(key)
            closer = self._open_chip_close.get(key)
            if marker is not None and marker.winfo_manager() != "":
                marker.pack_forget()
            if closer is not None and closer.winfo_manager() != "":
                closer.pack_forget()
        except tk.TclError:
            pass

    # ---- Import marker: spinner + pause/restart --------------------------

    def _import_window_state(self) -> str:
        """The Import window's auto-upload state, or "off" when it is closed."""
        win = self.tool_windows.get("sap_import")
        state_fn = getattr(win, "auto_upload_state", None)
        try:
            if win is not None and win.winfo_exists() and callable(state_fn):
                return str(state_fn())
        except Exception:
            pass
        return "off"

    def _on_marker_click(self, key: str, opener: str):
        """The Import marker pauses/resumes auto upload while it is enabled;
        every other marker (and Import when off) opens/focuses its window."""
        if key == "sap_import" and self._import_window_state() != "off":
            win = self.tool_windows.get("sap_import")
            toggle_fn = getattr(win, "toggle_auto_upload_pause", None)
            try:
                if callable(toggle_fn):
                    toggle_fn()
                    self._refresh_import_marker()
                    return
            except Exception:
                pass
        self._focus_or_open(key, opener)

    def _ensure_import_spinner(self):
        if self._import_spin_id is None:
            self._import_spin_i = 0
            self._spin_import_marker()

    def _stop_import_spinner(self):
        if self._import_spin_id is not None:
            try:
                self.after_cancel(self._import_spin_id)
            except Exception:
                pass
            self._import_spin_id = None
        self._refresh_import_marker(reset=True)

    def _spin_import_marker(self):
        state = self._import_window_state()
        self._refresh_import_marker(state)
        win = self.tool_windows.get("sap_import")
        try:
            alive = win is not None and win.winfo_exists()
        except tk.TclError:
            alive = False
        # Fast frames only while running; a slow poll otherwise so an in-window
        # toggle still reaches the marker without animating a paused one.
        delay = 160 if state == "running" else 600
        self._import_spin_id = self.after(delay, self._spin_import_marker) if alive else None

    def _refresh_import_marker(self, state: str | None = None, reset: bool = False):
        if reset:
            state = "off"
        elif state is None:
            state = self._import_window_state()
        frame = ""
        if state == "running":
            frame = IMPORT_SPINNER[self._import_spin_i % len(IMPORT_SPINNER)]
            self._import_spin_i += 1
        else:
            self._import_spin_i = 0
        self._paint_import_marker(state, frame)
        self._paint_auto_upload_indicator(state, frame)

    def _paint_import_marker(self, state: str, frame: str):
        marker = self._open_app_buttons.get("sap_import")
        if marker is None:
            return
        try:
            if state == "running":
                marker.config(text=f"● Import {frame}", bg=ui_styles.COLORS["success"],
                              fg=ui_styles.COLORS["text_on_primary"])
            elif state == "paused":
                marker.config(text="● Import ⏸", bg=ui_styles.COLORS["warning"],
                              fg=ui_styles.COLORS["text_on_primary"])
            else:
                marker.config(text="● Import", bg=ui_styles.COLORS["success_light"],
                              fg=ui_styles.COLORS["text"])
        except tk.TclError:
            pass

    def _paint_auto_upload_indicator(self, state: str, frame: str):
        """Read-only running/paused note on the collapsed docked pill (the
        hover-to-show header); the pill only exists while docked + auto-hidden.
        The tray marker remains the control."""
        try:
            if state == "running":
                self.dock.set_peek_status(f"⟳ Auto upload {frame}",
                                          ui_styles.COLORS["success_light"])
            elif state == "paused":
                self.dock.set_peek_status("⏸ Auto upload paused",
                                          ui_styles.COLORS["warning"])
            else:
                self.dock.set_peek_status("")
        except Exception:
            pass

    def _grow_tray(self):
        """Unfold: grow the tray 1px -> full height (children clip in =
        the tray reveals downward from under the shortcuts card)."""
        tray = self._apps_tray
        self._cancel_tray_anim()
        try:
            tray.update_idletasks()
            target = max(tray.winfo_reqheight(), 8)
            tray.pack_propagate(False)
            tray.configure(height=1)
        except tk.TclError:
            return
        gen = self._tray_anim_gen
        steps, delay = 10, 18

        def frame(i: int):
            if gen != self._tray_anim_gen:
                return
            self._tray_anim_id = None
            t = min(1.0, i / steps)
            e = 1.0 - (1.0 - t) ** 3  # ease-out: quick start, soft landing
            try:
                tray.configure(height=max(1, int(1 + e * (target - 1))))
            except tk.TclError:
                return
            if t < 1.0:
                try:
                    self._tray_anim_id = self.after(delay, lambda: frame(i + 1))
                except Exception:
                    self._tray_anim_id = None
                return
            self._settle_tray()

        try:
            self._tray_anim_id = self.after(delay, lambda: frame(1))
        except Exception:
            self._tray_anim_id = None

    def _shrink_tray(self):
        """Collapse: wipe the tray away upward, then unpack for zero
        footprint."""
        tray = self._apps_tray
        self._cancel_tray_anim()
        try:
            tray.update_idletasks()
            start = max(tray.winfo_reqheight(), tray.winfo_height(), 8)
            tray.pack_propagate(False)
            tray.configure(height=start)
        except tk.TclError:
            return
        gen = self._tray_anim_gen
        steps, delay = 8, 18

        def frame(i: int):
            if gen != self._tray_anim_gen:
                return
            self._tray_anim_id = None
            t = min(1.0, i / steps)
            try:
                tray.configure(height=max(1, int(start * (1.0 - t * t * t)) or 1))
            except tk.TclError:
                return
            if t < 1.0:
                try:
                    self._tray_anim_id = self.after(delay, lambda: frame(i + 1))
                except Exception:
                    self._tray_anim_id = None
                return
            for key in list(self._open_app_buttons):
                self._unpack_chip(key)
            try:
                tray.pack_propagate(True)
                tray.pack_forget()
            except tk.TclError:
                pass

        try:
            self._tray_anim_id = self.after(delay, lambda: frame(1))
        except Exception:
            self._tray_anim_id = None

    def _close_tool_window(self, key: str):
        """✕ on an open marker: close that helper window."""
        win = self.tool_windows.get(key)
        if win is None:
            return
        try:
            win.destroy()  # _on_tool_destroy refreshes the markers
        except tk.TclError:
            self.tool_windows.pop(key, None)
            self._refresh_open_apps()

    def _bring_to_front(self, win: tk.Toplevel):
        """Restore a minimized/withdrawn window, then raise and focus it."""
        try:
            if win.state() in ("iconic", "withdrawn"):
                win.deiconify()
                win.state("normal")
            win.lift()
            win.focus_force()
        except tk.TclError:
            pass

    def _focus_or_open(self, key: str, opener_name: str):
        win = self.tool_windows.get(key)
        try:
            if win is not None and win.winfo_exists():
                self._bring_to_front(win)
                return
        except tk.TclError:
            pass
        getattr(self, opener_name)()

    def _track_tool_window(self, key: str, win: tk.Toplevel):
        self.tool_windows[key] = win
        try:
            win.bind("<Destroy>", lambda e, k=key: self._on_tool_destroy(e, k), "+")
        except tk.TclError:
            pass
        self._refresh_open_apps()

    def _on_tool_destroy(self, event, key: str):
        if event.widget is self.tool_windows.get(key):
            self.tool_windows.pop(key, None)
            self.after(50, self._refresh_open_apps)

    def _toggle_dock(self):
        self.dock.apply()

    def _toggle_dock_mode(self):
        """Logo double-click: dock ON = auto-hide + borderless, OFF = normal."""
        self.dock.set_dock_mode(not self.dock.dock_enabled.get())

    def _toggle_autohide(self):
        if self.dock.autohide.get():
            self.dock.dock_enabled.set(True)  # auto-hide implies docked
        self.dock.apply()

    def _save_dock_meta(self):
        self._save_user_settings({
            "dock_enabled": bool(self.dock.dock_enabled.get()),
            "dock_autohide": bool(self.dock.autohide.get()),
        })

    def _build_menu(self):
        self.menu_bar = CustomMenuBar(self, height=30)
        self.menu_bar.add_menu(
            "Tools",
            [
                MenuBarItem("Purchase Request Helper", self._open_purchase_request),
                MenuBarItem("Process Orders", self._open_print_orders),
                MenuBarItem("SAP Import", self._open_sap_import),
                MenuBarItem("Revisions Register", self._open_revisions_register),
            ],
        )
        self.menu_bar.add_menu(
            "Settings",
            [
                MenuBarItem("Always on Top", variable=self.always_on_top,
                            command=self._toggle_always_on_top),
                MenuBarItem("Debug Mode", variable=self.debug_mode,
                            command=self._toggle_debug_mode),
                MenuBarItem("Power User", variable=self.power_user,
                            command=self._toggle_power_user),
                MenuBarItem("Debug Console", variable=self.show_console,
                            command=self._toggle_console),
                MenuBarItem("Fake SAP", variable=self.fake_sap,
                            command=self._toggle_fake),
                MenuBarItem("", separator=True),
                MenuBarItem("Dock top", variable=self.dock.dock_enabled,
                            command=self._toggle_dock),
                MenuBarItem("Auto-hide", variable=self.dock.autohide,
                            command=self._toggle_autohide),
                MenuBarItem("", separator=True),
                MenuBarItem("Check for updates…", command=self._open_update_modal),
                MenuBarItem("", separator=True),
                MenuBarItem("Quit", command=self._on_close),
            ],
        )
        self.menu_bar.pack(side="top", fill="x")
        # Remove default menu
        self.config(menu=None)

    def _open_revisions_register(self):
        from tools.sap_import.week_popup import RevisionsRegisterPopup
        RevisionsRegisterPopup(self)

    # ---- updates -------------------------------------------------------

    def _update_blocker(self) -> str | None:
        """Why an update cannot start now, or ``None`` when idle."""
        if self._sap_active:
            return "A SAP script is running. Finish it before updating."
        if self._import_window_state() == "running":
            return "Auto upload is running. Pause it before updating."
        return None

    def _open_update_modal(self):
        from .update_modal import UpdateWindow

        UpdateWindow.open(self, self._update_blocker, self._on_close)

    def _open_week_popup(self):
        from tools.sap_import.week_popup import WeekRevisionPopup
        WeekRevisionPopup(self)

    def _build_status_bar(self):
        # Status bar with sunken border — now with SAP Import counter on the right of status.
        # Pinned to the bottom: the open-helper tray packs above it, never below.
        bar = styled_status_bar(self)
        bar.pack(fill="x", padx=ui_styles.SPACING["normal"], pady=ui_styles.SPACING["tight"],
                 side="bottom")
        self._status_bar = bar
        self.status_label = styled_label(
            bar, "Starting up...", anchor="w", font=ui_styles.FONTS["body"]
        )
        self.status_label.pack(side="left", fill="x", expand=True)
        # Import counter (nice, colored) — between status and connection
        self.import_counter_label = styled_label(
            bar, "", anchor="e", font=(ui_styles.FONTS["body"][0], 8, "bold"),
            fg=ui_styles.COLORS["text_secondary"],
        )
        self.import_counter_label.pack(side="right", padx=(8, 12))
        self.import_counter_label.bind("<Button-1>", lambda e: self._open_sap_import())
        # Toolbar-scoped pending counter: polls the server list endpoint every
        # 2 minutes (read-only); the Import window's tick shares the same
        # counter so a running auto upload is reused with no extra request.
        import os

        from tools.sap_import.api_client import ImportApiClient
        from tools.sap_import.counter import ImportCounter
        auth = os.environ.get("BCSYS_AUTH") or None
        self._import_counter = ImportCounter(ImportApiClient(auth=auth))
        self._import_counter.on_change = self._on_import_counter_changed
        self._start_import_counter()
        # Week number — always visible in main UI
        try:
            from datetime import datetime
            iso_y, iso_w, _ = datetime.now().isocalendar()
            wk_txt = f"W{iso_w:02d} · {iso_y}"
        except Exception:
            wk_txt = ""
        self.week_label = styled_label(
            bar, wk_txt, anchor="e", font=(ui_styles.FONTS["body"][0], 8, "bold"),
            fg="#004085", bg="#e8f4f8",
        )
        self.week_label.pack(side="right", padx=(8, 0))
        self.week_label.config(cursor="hand2")
        self.week_label.bind("<Button-1>", lambda e: self._open_week_popup())
        self.connection_label = styled_label(
            bar,
            "\u25cf Disconnected",
            anchor="e",
            font=ui_styles.FONTS["body"],
            fg=ui_styles.COLORS["text_secondary"],
        )
        self.connection_label.pack(side="right")

    def _start_import_counter(self):
        # Immediate, offline-friendly display from the local queue; the server
        # poll (read-only) takes over on its first 2-minute tick.
        self._render_local_import_counter()
        self._import_poll_id = self.after(120000, self._poll_import_counter)

    def _on_import_counter_changed(self):
        """A fresh count landed from the Import tick: render it now."""
        self._sap_import_ok = getattr(self._import_counter, "last_ok", None)
        try:
            self.after(0, self._render_import_counter)
            self.after(0, self._render_sap_indicator)
        except Exception:
            pass

    def _on_import_queue_changed(self):
        """The local queue changed: refresh the pending/done split now."""
        try:
            self.after(0, self._render_local_import_counter)
        except Exception:
            pass

    def _render_local_import_counter(self):
        """Pending/failed/done from the local si_queue.json (no network)."""
        try:
            import json
            from pathlib import Path
            p = Path(__file__).parent.parent / "data" / "si_queue.json"
            if not p.exists():
                self.import_counter_label.config(text="◈ Import: —", cursor="hand2")
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            # 66 is the retired FORCEREN code; the Import window migrates it
            # on load, but the toolbar may read the raw file before that.
            pending = sum(1 for r in data
                          if r.get("Status") in PENDING_STATUSES
                          or r.get("Status") == "66")
            failed = sum(1 for r in data if r.get("Status") in FAILED_STATUSES)
            done = sum(1 for r in data if r.get("Status") in FINISHED_STATUSES)
            if pending or failed:
                txt = f"◈ Import: {pending} pending"
                if failed:
                    txt += f" • {failed} failed"
                elif done:
                    txt += f" • {done} done"
                color = ui_styles.COLORS["error"] if failed else ui_styles.COLORS["warning"]
            else:
                txt, color = "◈ Import: up to date", ui_styles.COLORS["success"]
            self.import_counter_label.config(text=txt, fg=color, cursor="hand2")
        except Exception:
            self.import_counter_label.config(text="◈ Import: —", cursor="hand2")

    def _poll_import_counter(self):
        """2-minute server pending count (read-only).

        When the Import window is auto-uploading, its tick already fetched the
        list and recorded the count here, so this reuses it with no request;
        otherwise it polls the list endpoint itself.
        """
        if self._import_window_state() != "running":
            try:
                pg = (self.engine.get_variable("si_pg")
                      or user_settings.load_user_settings().get("si_pg")
                      or "WP1")
            except Exception:
                pg = "WP1"
            try:
                self._import_counter.poll("WO", pg)
            except Exception:
                pass
        self._render_import_counter()
        self._sap_import_ok = self._import_counter.last_ok
        self._render_sap_indicator()
        self._import_poll_id = self.after(120000, self._poll_import_counter)

    def _render_import_counter(self):
        counter = self._import_counter
        if not counter.known:
            return  # keep the local display until a server answer lands
        if counter.count:
            self.import_counter_label.config(
                text=f"◈ Import: {counter.count} pending",
                fg=ui_styles.COLORS["warning"], cursor="hand2")
        else:
            self.import_counter_label.config(
                text="◈ Import: up to date", fg=ui_styles.COLORS["success"], cursor="hand2")

    # ---- debugger ---------------------------------------------------------
    def open_debugger(self, script: str, title: str = "", on_save=None, on_success=None):
        """The single entry point for opening the debugger with a script
        loaded. Every window (including tool windows, via ScriptRunnerMixin.
        run_script -> self._toolbar.open_debugger) goes through this one
        method -- there is no second copy of this logic anywhere."""
        # Hold the SAP attach for the whole debug session; DebuggerWindow.close
        # ends it (and releases) on every close path.
        self.engine.start_debug_session()
        if self.debugger_window is None or not self.debugger_window.winfo_exists():
            self.debugger_window = DebuggerWindow(self, self.engine)

        # Release any modal grab so debugger can receive events; store for
        # restoration when the debugger closes (restored in _on_close).
        try:
            self.debugger_window._previous_grab = self.grab_current()
            if self.debugger_window._previous_grab:
                self.debugger_window._previous_grab.grab_release()
        except tk.TclError:
            self.debugger_window._previous_grab = None

        self.debugger_window.load_script(script, title=title, on_save=on_save, on_success=on_success)
        self.debugger_window.deiconify()
        self.debugger_window.lift()
        self.debugger_window.focus_force()

    def _process_pending_debug_script(self):
        """Process the next pending script in the debug queue."""
        # Get pending script from engine (set when RUN_SCRIPT pauses execution)
        if self.engine._pending_debug_script:
            button_id, script_name, script = self.engine._pending_debug_script
            self.engine._pending_debug_script = None

            # Try toolbar first (for toolbar buttons)
            if hasattr(self, 'run_script'):
                self.run_script(button_id, title=script_name)
                return

            # Then try tool windows (BaseToolWindow has run_script via the
            # mixin, but the registry is typed as plain Toplevel).
            for win in self.tool_windows.values():
                runner = getattr(win, "run_script", None)
                if callable(runner) and win.winfo_exists():
                    runner(button_id, title=script_name)
                    break

    def _open_purchase_request(self):
        from tools.purchase_request import PurchaseRequestHelperWindow

        key = "purchase_request"
        win = self.tool_windows.get(key)
        if win is not None and win.winfo_exists():
            self._bring_to_front(win)
            return
        win = PurchaseRequestHelperWindow(self, self.engine)
        self._track_tool_window(key, win)

    def _open_print_orders(self):
        from tools.print_orders import PrintOrdersWindow

        key = "print_orders"
        win = self.tool_windows.get(key)
        if win is not None and win.winfo_exists():
            self._bring_to_front(win)
            return
        win = PrintOrdersWindow(self, self.engine)
        self._track_tool_window(key, win)

    def _open_sap_import(self):
        from tools.sap_import import SapImportWindow

        key = "sap_import"
        win = self.tool_windows.get(key)
        if win is not None and win.winfo_exists():
            self._bring_to_front(win)
            return
        win = SapImportWindow(self, self.engine, is_fake=self.fake_sap.get,
                              counter=self._import_counter,
                              on_queue_changed=self._on_import_queue_changed)
        self._track_tool_window(key, win)

    # ---- edit modal --------------------------------------------------------
    # Right-click is now handled by CustomComponent itself
    # No additional methods needed in SapToolbar

    # ---- toggles --------------------------------------------------------
    def _toggle_always_on_top(self):
        self.attributes("-topmost", self.always_on_top.get())
        self._save_meta_value("always_on_top", self.always_on_top.get())

    def _save_meta_value(self, key: str, value):
        """Persist one Settings toggle in the untracked user settings."""
        self._save_user_settings({key: value})

    def _save_user_settings(self, updates: dict):
        try:
            user_settings.update_user_settings(updates)
        except Exception:
            pass

    def _toggle_debug_mode(self):
        if (
            not self.debug_mode.get()
            and self.debugger_window is not None
            and self.debugger_window.winfo_exists()
        ):
            self.debugger_window.withdraw()
            self.debugger_window.close()
            self.debugger_window = None
            # Process any pending scripts when debugger closes
            self._process_pending_debug_script()
        self._save_meta_value("debug_mode", self.debug_mode.get())

    def _toggle_power_user(self):
        """Power-user mode: right-click editing on tool-window buttons."""
        self._save_meta_value("power_user", self.power_user.get())
        self.report_status(
            "Power User on - tool-window buttons can be right-click edited"
            if self.power_user.get() else "Power User off",
            "info",
        )

    def _toggle_console(self):
        """Spawn or tear down the debug console (Windows terminal window)
        used to see engine logs. Persists the choice in user settings."""
        if self.show_console.get():
            if not console.show_console():
                self.show_console.set(False)
                self.report_status("Could not open debug console", "error")
                return
            self.report_status("Debug console opened", "info")
        else:
            console.hide_console()
            self.report_status("Debug console closed", "info")

        self._save_meta_value("show_console", self.show_console.get())

    @staticmethod
    def _backend_factory_for(fake: bool):
        """Backend factory captured for one mode.

        The factory runs on the worker thread, so it must not read Tk state --
        ``BooleanVar.get()`` off the main thread raises "main thread is not in
        main loop". The caller passes a plain bool snapshot on the main thread
        instead; toggling rebuilds the worker with a fresh snapshot.
        """
        if fake:
            return lambda: RealSapBackend(app_factory=FakeComClient())
        return lambda: RealSapBackend()

    def _apply_fake_mode(self, persist: bool = False):
        """Rebuild the worker for the current Fake/Real choice, once."""
        fake = bool(self.fake_sap.get())
        if persist:
            self._save_meta_value("fake_sap", fake)
        if fake == getattr(self, "_worker_fake", None):
            return
        self._worker_fake = fake
        # Stop worker and restart so the new backend choice takes effect.
        self.engine.worker.stop()
        self.engine.worker = SapWorker(self._backend_factory_for(fake))
        self.engine.worker.start()
        # Settle the label through the normal event path in both modes.
        self._probe_attach()

    def _toggle_fake(self):
        self._apply_fake_mode(persist=True)

    def _on_close(self):
        """Clean up worker thread and polling on window close."""
        self._stop_polling()
        self._stop_sap_activity()
        try:
            self._cancel_tray_anim()
        except Exception:
            pass
        try:
            self.dock.destroy()
        except Exception:
            pass
        self.engine.worker.stop()
        self.destroy()

    def _probe_attach(self):
        """Attach once and release immediately to settle the connection label
        ("SAP ready" / "SAP not running"), holding nothing."""
        self.connection_label.config(
            text="\u25cf Connecting...", fg=ui_styles.COLORS["text_secondary"]
        )
        self.engine.probe_attach()

    def _on_sap_connected(self, connected: bool):
        """Called on the main thread when the worker reports an attach attempt
        (the startup probe, or the first command of a run)."""
        self._sap_connected = bool(connected)
        if connected:
            self.status_label.config(text="Ready", fg=ui_styles.COLORS["success"])
        else:
            self.status_label.config(
                text="SAP GUI not running — the next run will try again",
                fg=ui_styles.COLORS["text_secondary"],
            )
        self._render_sap_indicator()

    def _sap_is_fake(self) -> bool:
        try:
            return bool(self.fake_sap.get())
        except Exception:
            return False

    def _sap_idle_parts(self):
        """(label text, colour, pill text, pill colour) for a non-running state."""
        if not self._sap_connected:
            return ("\u25cf SAP Disconnected", ui_styles.COLORS["error"],
                    "\u25cf SAP \u2715", "#ffd6d6")
        if self._sap_import_ok is False:
            return ("\u25cf SAP unreachable (Import)", ui_styles.COLORS["error"],
                    "\u25cf SAP !", "#ffd6d6")
        name = "Fake SAP" if self._sap_is_fake() else "SAP"
        return (f"\u25cf {name} ready", ui_styles.COLORS["success"],
                f"\u25cf {name}", "#e6f5e6")

    def _paint_sap_indicator(self, frame: str | None = None):
        if not hasattr(self, "connection_label"):
            return
        if self._sap_active:
            name = "Fake SAP" if self._sap_is_fake() else "SAP"
            frame = frame or IMPORT_SPINNER[0]
            text = f"\u25c9 {name} {frame}"
            color = ui_styles.COLORS["primary"]
            pill_text, pill_fg = f"\u25c9 SAP {frame}", "#e6f5e6"
        else:
            text, color, pill_text, pill_fg = self._sap_idle_parts()
        try:
            self.connection_label.config(text=text, fg=color)
        except tk.TclError:
            return
        try:
            self.dock.set_peek_sap_state(pill_text, pill_fg)
        except Exception:
            pass

    def _render_sap_indicator(self):
        self._paint_sap_indicator()

    def _on_sap_activity(self, running: bool):
        """The engine entered/left a script run: animate the indicator."""
        running = bool(running)
        if running == self._sap_active:
            return
        self._sap_active = running
        if running:
            self._sap_activity_frame = 0
            self._spin_sap_activity()
        else:
            self._stop_sap_activity()
            self._render_sap_indicator()

    def _spin_sap_activity(self):
        if not self._sap_active:
            return
        frame = IMPORT_SPINNER[self._sap_activity_frame % len(IMPORT_SPINNER)]
        self._sap_activity_frame += 1
        self._paint_sap_indicator(frame)
        try:
            self._sap_activity_id = self.after(160, self._spin_sap_activity)
        except tk.TclError:
            self._sap_activity_id = None

    def _stop_sap_activity(self):
        if self._sap_activity_id:
            try:
                self.after_cancel(self._sap_activity_id)
            except Exception:
                pass
            self._sap_activity_id = None

    def _start_polling(self):
        """Start polling for worker thread results."""
        self._poll_id = self.after(30, self._poll)

    def _stop_polling(self):
        """Stop polling for worker thread results."""
        if self._poll_id:
            self.after_cancel(self._poll_id)
            self._poll_id = None
        if getattr(self, "_import_poll_id", None):
            try:
                self.after_cancel(self._import_poll_id)
            except tk.TclError:
                pass
            self._import_poll_id = None

    def _poll(self):
        """Poll worker thread for completed jobs."""
        self.engine.poll()
        # Connection-state changes are emitted worker->main-thread via the
        # event queue (never by calling Tk from the worker thread). Handle
        # them here, on the main thread, inside the Tk event loop.
        for connected in self.engine.worker.drain_connect_events():
            self._on_sap_connected(connected)
        self._poll_id = self.after(30, self._poll)
