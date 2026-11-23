"""SapToolbar -- the root window.

Layout is 100% hardcoded here; only text, default values, and scripts come
from config/toolbar_config.json. If you want a 5th shortcut button, add a
row/column here -- don't make the layout data-driven, that complexity is
exactly what this rework removes.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

import tkinterdnd2

from core import config as cfg
from core import console, i18n, user_settings
from core.com_backend import RealSapBackend
from core.commands import sanitize_sap_number
from core.engine import SapScriptEngine
from core.fake_com import FakeComClient
from core.sap_worker import SapWorker
from tools.sap_import.models import FAILED_STATUSES, FINISHED_STATUSES, PENDING_STATUSES

from . import ui_styles
from .custom_widgets import CustomButton, CustomEntry, CustomLabel, ScriptRunnerMixin, _get_config
from .debugger import DebuggerWindow
from .dock_manager import DockManager, load_logo_image
from .dpi import pin_dpi_mode, read_startup_mode
from .ui_styles import (
    card_frame,
    styled_frame,
    styled_label,
    styled_status_bar,
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

# Quick-open fields holding SAP numbers (werkorder, melding): pasted
# values get hardened to the digit run (see sanitize_sap_number).
# Revisie (toolbar_qo3) is a free code -- leaders/whitespace stripped only.
SAP_NUMBER_FIELDS = {"toolbar_qo1", "toolbar_qo2"}
# The toolbar can stay open for weeks, so re-read the ISO week hourly; the
# label must not keep the week the app happened to start in.
WEEK_REFRESH_MS = 60 * 60 * 1000
OPEN_APPS = [
    ("purchase_request", "PRH", "_open_purchase_request"),
    ("print_orders", "Print", "_open_print_orders"),
    ("sap_import", "Import", "_open_sap_import"),
    ("planner", "Planner", "_open_planner"),
]
# Tool windows the user can show or hide from Settings.
# (key, menu label, catalogue label, one-line description, default on, opener)
TOOL_CATALOGUE = [
    ("purchase_request", "Purchase Request Helper", "Purchase Request Helper",
     "Create purchase requests and attach documents.", True,
     "_open_purchase_request"),
    ("print_orders", "Print Orders", "Print Orders",
     "Print work orders from IW38 as PDFs.", True, "_open_print_orders"),
    ("sap_import", "SAP Import", "SAP Import — Work Orders",
     "Upload work orders and photos to BCsys.", True, "_open_sap_import"),
    ("planner", "Planner", "Planner (unfinished)",
     "Day/team planning board; also needs Power User.", False,
     "_open_planner"),
    ("revisions", "Revisions Register", "Revisions Register",
     "Manage revision weeks and options.", True,
     "_open_revisions_register"),
    # Temporary: remove with tools/dock_diag.py once the dock bug is fixed.
    ("dock_diag", "Dock Diagnostic", "Dock Diagnostic (temporary)",
     "Records monitor, DPI and dock geometry while you reproduce a bug.", True,
     "_open_dock_diag"),
    # Temporary: remove with tools/dpi_test.py once the dock bug is fixed.
    ("dpi_test", "DPI Mode Test", "DPI Mode Test (temporary)",
     "Restart the app DPI-unaware or per-monitor V2 and compare the dock.",
     True, "_open_dpi_test"),
]
# Frames for the Import marker while auto upload runs (Braille spinner).
IMPORT_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class SapToolbar(ScriptRunnerMixin, tkinterdnd2.TkinterDnD.Tk):
    def __init__(self):
        # Before the root window exists: one fixed DPI mode for the app. The
        # temporary DPI Test window can select per-monitor V2 for a restart.
        pin_dpi_mode(read_startup_mode())
        super().__init__()
        self.title("SAP Work Order Tool")
        self.config(bg=ui_styles.COLORS["bg"])

        # Hide window while everything gets built (prevents flicker).
        self.withdraw()
        self.update_idletasks()
        self.resizable(False, False)

        self.config_data = _get_config()

        settings = user_settings.load_user_settings()
        self._tools_enabled = self._enabled_from(settings)
        i18n.set_language(settings.get("ui_language", i18n.AUTO_LANGUAGE))
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
        self._helper_menu = None
        self._widget_registry: dict[str, tk.Widget] = {}  # Maps component_id -> widget
        self._poll_id = None  # for after() polling
        self._import_poll_running = False
        self._import_down = False
        self._open_app_buttons: dict[str, tk.Button] = {}
        self._open_chip_close: dict[str, tk.Button] = {}
        self.dock = DockManager(
            self, settings,
            save_callback=self._save_dock_meta,
            on_decorated=self._set_titlebar_color,
            on_before_show=self._sync_dock_layout,
        )

        self._build_quick_open_row()
        self._build_shortcuts_row()
        self._build_apps_tray()
        self._build_status_bar()

        self._set_app_icon()  # logo as window/taskbar icon, then reveal
        self._set_titlebar_color()  # Apply pywinstyles color, then reveal
        self.deiconify()
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

        # Helper launcher: available on both the docked and undocked toolbar.
        # The slot matches the 28 px logo beside it, so the icon is not a
        # tiny text button next to a larger image.
        helper_slot = tk.Frame(
            row, width=28, height=28,
            bg=ui_styles.COLORS["surface"],
        )
        helper_slot.pack(
            side="left", anchor="s",
            padx=(0, ui_styles.SPACING["tight"]),
            pady=(ui_styles.SPACING["tight"], ui_styles.SPACING["tight"]),
        )
        helper_slot.pack_propagate(False)
        self._helper_toggle = tk.Button(
            helper_slot,
            text="☰",
            font=(ui_styles.FONTS["header"][0], 12),
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text"],
            activebackground=ui_styles.COLORS["surface_hover"],
            activeforeground=ui_styles.COLORS["text"],
            relief="flat", bd=0, highlightthickness=0,
            padx=0, pady=0, cursor="hand2",
            command=self._toggle_helper_menu,
        )
        self._helper_toggle.pack(fill="both", expand=True)

        # Brand home: the logo fronts the quick-open row, left of the
        # Werkorder field, and parents the open-helper tray -- the tray
        # unfolds beneath the shortcuts card, and the logo pulses as it
        # opens/closes.
        logo_img = load_logo_image(28, master=row)
        # No label border: Tk's default 1 px border made the logo label 32 px
        # tall against the helper's 28 px slot, which pushed the hamburger a
        # couple of pixels lower than the logo.
        if logo_img is not None:
            self._logo_img = logo_img  # hold a ref while shown
            logo = tk.Label(
                row, image=logo_img, bg=ui_styles.COLORS["bg"], cursor="hand2",
                bd=0, highlightthickness=0, padx=0, pady=0,
            )
        else:
            self._logo_img = None
            logo = tk.Label(
                row, text="BC", font=ui_styles.FONTS["header"],
                bg=ui_styles.COLORS["bg"], fg=ui_styles.COLORS["primary"],
                cursor="hand2", bd=0, highlightthickness=0, padx=0, pady=0,
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
            popdown = combo.tk.call("ttk::combobox::PopdownWindow", str(combo))
            if combo.tk.call("winfo", "ismapped", popdown):
                return  # never reconfigure an open dropdown under the pointer
        except Exception:
            pass
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
    def _toggle_helper_menu(self):
        if self._helper_menu is not None:
            self._close_helper_menu()
            return
        from .helper_menu import HelperMenu

        menu = HelperMenu(self, TOOL_CATALOGUE)
        if menu.winfo_exists():
            self._helper_menu = menu

    def _close_helper_menu(self):
        menu = self._helper_menu
        if menu is not None:
            try:
                menu.close()
            except Exception:
                pass
        self._helper_menu = None
        self._set_helper_toggle_active(False)

    def _set_helper_toggle_active(self, active: bool):
        try:
            self._helper_toggle.configure(
                bg=(ui_styles.COLORS["primary"] if active
                    else ui_styles.COLORS["surface"]),
                fg=("#ffffff" if active else ui_styles.COLORS["text"]),
                activebackground=(ui_styles.COLORS["primary_hover"]
                                  if active
                                  else ui_styles.COLORS["surface_hover"]),
                activeforeground=("#ffffff" if active
                                  else ui_styles.COLORS["text"]),
            )
        except tk.TclError:
            pass

    def _open_settings_tools(self):
        self._open_dock_settings()
        win = getattr(self, "_dock_settings_win", None)
        if win is not None and win.winfo_exists():
            win.open_tools_tab()

    def _open_dock_settings(self):
        """Open the Settings helper modal; one instance at a time."""
        self._close_helper_menu()
        win = getattr(self, "_dock_settings_win", None)
        if win is not None and win.winfo_exists():
            win.deiconify()
            win.lift()
            win.focus_force()
            return
        from .settings_window import SettingsWindow

        self._dock_settings_win = SettingsWindow(self, TOOL_CATALOGUE)

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
            self.dock.refit()
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

    def _sync_dock_layout(self):
        """Refresh the OPEN tray before the dock measures its show height."""
        tray = getattr(self, "_apps_tray", None)
        if tray is None:
            return
        self._refresh_open_apps(animate=False)
        try:
            self.update_idletasks()
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
                self.dock.refit()
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
            try:
                hidden = self.dock._is_hidden()
            except Exception:
                hidden = False
            if hidden:
                # Collapse now: a shrink animation inside a withdrawn bar
                # leaves a stale height for the next show.
                self._sync_dock_layout()
            else:
                self.after(50, self._refresh_open_apps)

    def _toggle_dock_mode(self):
        """Logo double-click: dock ON = auto-hide + borderless, OFF = normal."""
        self._close_helper_menu()
        self.dock.set_dock_mode(not self.dock.dock_enabled.get())

    def _save_dock_meta(self):
        self._save_user_settings({
            "dock_enabled": bool(self.dock.dock_enabled.get()),
            "dock_autohide": bool(self.dock.autohide.get()),
        })

    @staticmethod
    def _enabled_from(settings: dict) -> set[str]:
        """Tool keys the user enabled; unknown keys are ignored."""
        known = {key for key, *_rest in TOOL_CATALOGUE}
        saved = settings.get("enabled_tools")
        if isinstance(saved, list):
            return {key for key in saved if key in known}
        return {key for key, _menu, _name, _desc, default, _opener in TOOL_CATALOGUE
                if default}

    def _apply_enabled_tools(self, enabled: list[str]):
        """Persist the catalogue choice and apply it everywhere it shows."""
        known = {key for key, *_rest in TOOL_CATALOGUE}
        self._tools_enabled = {key for key in enabled if key in known}
        ordered = [key for key, *_rest in TOOL_CATALOGUE
                   if key in self._tools_enabled]
        self._save_user_settings({"enabled_tools": ordered})
        self._close_helper_menu()
        for key in list(self.tool_windows):
            if key not in self._tools_enabled:
                self._close_tool_window(key)
        self._set_import_enabled("sap_import" in self._tools_enabled)

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

    def _open_week_popup(self):
        from tools.sap_import.week_popup import WeekRevisionPopup
        WeekRevisionPopup(self)

    @staticmethod
    def _week_text(now=None) -> str:
        """``Wnn · yyyy`` for ``now`` (default: the current moment)."""
        try:
            from datetime import datetime
            iso_y, iso_w, _ = (now or datetime.now()).isocalendar()
            return f"W{iso_w:02d} · {iso_y}"
        except Exception:
            return ""

    def _refresh_week_label(self, now=None):
        """Re-read the ISO week and keep one hourly refresh armed."""
        text = self._week_text(now)
        try:
            if text and self.week_label.cget("text") != text:
                self.week_label.config(text=text)
        except tk.TclError:
            return
        self._schedule_week_refresh()

    def _schedule_week_refresh(self):
        if self._week_refresh_id is not None:
            return
        try:
            self._week_refresh_id = self.after(
                WEEK_REFRESH_MS, self._week_refresh_tick)
        except tk.TclError:
            self._week_refresh_id = None

    def _week_refresh_tick(self):
        self._week_refresh_id = None
        self._refresh_week_label()

    def _stop_week_refresh(self):
        if getattr(self, "_week_refresh_id", None) is not None:
            try:
                self.after_cancel(self._week_refresh_id)
            except tk.TclError:
                pass
            self._week_refresh_id = None

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
        if "sap_import" in self._tools_enabled:
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
        # Week number — always visible in main UI, refreshed hourly so a
        # toolbar left open across a week boundary follows the clock.
        self._week_refresh_id = None
        self.week_label = styled_label(
            bar, self._week_text(), anchor="e", font=(ui_styles.FONTS["body"][0], 8, "bold"),
            fg="#004085", bg="#e8f4f8",
        )
        self.week_label.pack(side="right", padx=(8, 0))
        self.week_label.config(cursor="hand2")
        self.week_label.bind("<Button-1>", lambda e: self._open_week_popup())
        self._refresh_week_label()
        self.connection_label = styled_label(
            bar,
            "\u25cf Disconnected",
            anchor="e",
            font=ui_styles.FONTS["body"],
            fg=ui_styles.COLORS["text_secondary"],
        )
        self.connection_label.pack(side="right")
        # While BCsys is down this label becomes the retry control.
        self.connection_label.bind(
            "<Button-1>", lambda _e: self._retry_import_counter())

    def _start_import_counter(self):
        # Immediate, offline-friendly display from the local queue; the server
        # poll (read-only) takes over on its first 2-minute tick.
        self._render_local_import_counter()
        self._cancel_import_timer()
        if "sap_import" not in self._tools_enabled:
            return
        self._import_poll_id = self.after(120000, self._poll_import_counter)

    def _cancel_import_timer(self):
        if getattr(self, "_import_poll_id", None):
            try:
                self.after_cancel(self._import_poll_id)
            except Exception:
                pass
            self._import_poll_id = None

    def _set_import_enabled(self, enabled: bool):
        """Show or remove every BCsys piece that belongs to SAP Import."""
        self._cancel_import_timer()
        self._import_down = False
        if enabled:
            if self.import_counter_label.winfo_manager() == "":
                self.import_counter_label.pack(side="right", padx=(8, 12))
            self._start_import_counter()
            return
        self.import_counter_label.pack_forget()
        if self._import_poll_running:
            self._import_poll_running = False
            try:
                self._import_counter.end()
            except Exception:
                pass
        self._sap_import_ok = None
        self._render_sap_indicator()

    def _on_import_counter_changed(self):
        """A fresh count landed from the toolbar poll or the Import tick."""
        self._sap_import_ok = getattr(self._import_counter, "last_ok", None)
        if self._sap_import_ok is False:
            self._import_down = True
            self._cancel_import_timer()
        elif self._sap_import_ok is True and self._import_down:
            self._import_down = False
            self._resume_import_poll()
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
        if "sap_import" not in self._tools_enabled:
            return
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
        """Ask for the server pending count without blocking the UI.

        The HTTP call runs on a worker thread; `_collect_import_counter`
        picks the answer up on the main thread. A failure stops the timer
        until the indicator is clicked (`_retry_import_counter`).
        """
        self._import_poll_id = None
        if "sap_import" not in self._tools_enabled or self._import_down:
            return
        if self._import_window_state() != "running":
            if not self._start_import_poll():
                self._schedule_import_counter()
            return
        self._schedule_import_counter()

    def _start_import_poll(self) -> bool:
        """Start one background fetch; False when one is already in flight or
        the Import window holds the shared guard."""
        if self._import_poll_running or not self._import_counter.begin():
            return False
        try:
            pg = (self.engine.get_variable("si_pg")
                  or user_settings.load_user_settings().get("si_pg")
                  or "WP1")
        except Exception:
            pg = "WP1"
        self._import_poll_running = True
        results: queue.Queue = queue.Queue()
        threading.Thread(
            target=self._fetch_import_counter,
            args=(pg, results),
            daemon=True,
        ).start()
        self._collect_import_counter(results)
        return True

    def _retry_import_counter(self):
        """Click on the SAP indicator while BCsys is down."""
        if not self._import_down or "sap_import" not in self._tools_enabled:
            return
        if self._import_poll_running:
            return
        self.report_status("Retrying BCsys...", "info")
        self._start_import_poll()

    def _resume_import_poll(self):
        """Restart the 2-minute timer after a success, once only."""
        if ("sap_import" in self._tools_enabled
                and not self._import_down
                and self._import_poll_id is None):
            self._schedule_import_counter()

    def _fetch_import_counter(self, pg: str, results: queue.Queue):
        """Worker: network only -- no Tk or counter state off the main thread."""
        try:
            result = self._import_counter.fetch("WO", pg)
        except Exception:
            result = None
        results.put(result)

    def _collect_import_counter(self, results: queue.Queue):
        """Main thread: record a finished poll and schedule the next tick."""
        if not self.winfo_exists():
            return
        try:
            result = results.get_nowait()
        except queue.Empty:
            self._import_poll_id = self.after(
                100, lambda: self._collect_import_counter(results))
            return
        self._import_poll_running = False
        if "sap_import" not in self._tools_enabled:
            try:
                self._import_counter.end()
            except Exception:
                pass
            return
        try:
            if result is None:
                self._import_counter.note_request(False)
            else:
                self._import_counter.record(result.get("data", []))
        finally:
            self._import_counter.end()
        self._render_import_counter()
        self._sap_import_ok = self._import_counter.last_ok
        self._render_sap_indicator()
        self._import_down = self._sap_import_ok is False
        if self._import_down:
            self._cancel_import_timer()
        else:
            self._resume_import_poll()

    def _schedule_import_counter(self):
        self._import_poll_id = self.after(120000, self._poll_import_counter)

    def _render_import_counter(self):
        if "sap_import" not in self._tools_enabled:
            return
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

    def _open_dock_diag(self):
        from tools.dock_diag import DockDiagWindow

        key = "dock_diag"
        win = self.tool_windows.get(key)
        if win is not None and win.winfo_exists():
            self._bring_to_front(win)
            return
        win = DockDiagWindow(self, self.engine)
        self._track_tool_window(key, win)

    def _open_dpi_test(self):
        from tools.dpi_test import DpiTestWindow

        key = "dpi_test"
        win = self.tool_windows.get(key)
        if win is not None and win.winfo_exists():
            self._bring_to_front(win)
            return
        win = DpiTestWindow(self, self.engine)
        self._track_tool_window(key, win)

    def _open_planner(self):
        from tools.planner import PlannerWindow

        key = "planner"
        win = self.tool_windows.get(key)
        if win is not None and win.winfo_exists():
            self._bring_to_front(win)
            return
        win = PlannerWindow(self, self.engine)
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

    def _disable_developer_modes(self):
        """Power User is off: leave no hidden dev mode running."""
        if self.fake_sap.get():
            self.fake_sap.set(False)
            self._toggle_fake()
        if self.debug_mode.get():
            self.debug_mode.set(False)
            self._toggle_debug_mode()
        if self.show_console.get():
            self.show_console.set(False)
            self._toggle_console()

    def _toggle_power_user(self):
        """Power-user mode: right-click editing on tool-window buttons and the
        developer toggles. Turning it off also leaves those modes off, so no
        hidden dev mode keeps running."""
        self._save_meta_value("power_user", self.power_user.get())
        if not self.power_user.get():
            self._disable_developer_modes()
        self._close_helper_menu()
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
        self._close_helper_menu()
        self._stop_polling()
        self._stop_sap_activity()
        self._stop_week_refresh()
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
        if self._sap_import_ok is False and "sap_import" in self._tools_enabled:
            return ("\u25cf BCsys unreachable", ui_styles.COLORS["error"],
                    "\u25cf BCsys !", "#ffd6d6")
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
        retryable = self._import_down and "sap_import" in self._tools_enabled
        try:
            self.connection_label.config(
                text=text, fg=color, cursor="hand2" if retryable else "")
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
        if getattr(self, "_import_poll_running", False):
            # A fetch outlived the window: release its guard; the daemon
            # worker's result is simply never collected.
            self._import_poll_running = False
            try:
                self._import_counter.end()
            except Exception:
                pass

    def _poll(self):
        """Poll worker thread for completed jobs."""
        self.engine.poll()
        # Connection-state changes are emitted worker->main-thread via the
        # event queue (never by calling Tk from the worker thread). Handle
        # them here, on the main thread, inside the Tk event loop.
        for connected in self.engine.worker.drain_connect_events():
            self._on_sap_connected(connected)
        self._poll_id = self.after(30, self._poll)
