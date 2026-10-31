"""Custom widgets with built-in right-click editing.

This module provides CustomButton, CustomEntry, and CustomLabel classes that
support right-click editing with automatic config loading and live updates.
"""

from __future__ import annotations

import json
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Literal

from core import config as cfg

if TYPE_CHECKING:
    from core.engine import SapScriptEngine
    from ui.sap_toolbar import SapToolbar

from . import ui_styles
from .ui_styles import styled_label

# Type alias for valid component types
ComponentType = Literal["buttons", "textinput", "headers", "datagridview"]

# Global config cache
_config_cache: dict | None = None


def _get_config() -> dict:
    """Load and cache config data. Returns cached version if available."""
    global _config_cache
    if _config_cache is None:
        try:
            _config_cache = cfg.load_config()
        except FileNotFoundError:
            import warnings

            warnings.warn("Config file not found, using empty config", UserWarning)
            _config_cache = {}
        except json.JSONDecodeError as e:
            # Corrupt JSON — show error and crash loudly so the user knows
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Config Error",
                f"toolbar_config.json is corrupt:\n\n{e.msg}\n\nLine: {e.lineno}, Col: {e.colno}\n\nPlease fix the JSON and restart the app.",
            )
            root.destroy()
            raise
        except Exception as e:
            # Other errors (permission denied, etc.) — also crash loudly
            import traceback

            print(f"Failed to load config: {e}")
            traceback.print_exc()
            raise
    return _config_cache


def _id_exists(component, button_id: str) -> bool:
    """Check if a button_id exists in the config."""
    if component.config_data is None:
        return False
    component_type = component.component_type
    if component_type not in component.config_data["customcomponents"]:
        return False
    return button_id in component.config_data["customcomponents"][component_type]


def _toolbar_of(component):
    """The SapToolbar that owns this widget, if any."""
    from ui.sap_toolbar import SapToolbar

    node = component
    while node is not None:
        if isinstance(node, SapToolbar):
            return node
        node = getattr(node, "master", None)
    return None


class CustomComponent:
    """Mixin for right-click editing on any widget."""

    _var: tk.StringVar | None = None

    def __init__(
        self,
        master,
        component_type: ComponentType | None = None,
        button_id: str | None = None,
        editable: bool = True,
        **kwargs,
    ):
        super().__init__(master, **kwargs)  # type: ignore[call-overload]
        self.component_type = component_type
        self.button_id = button_id
        self.editable = editable
        self.config_data = _get_config()

        if editable:
            self.bind("<Button-3>", self._on_right_click)  # type: ignore[attr-defined]

    def _on_right_click(self, event):
        """Show edit modal on right-click."""
        from .edit_modal import EditModal

        # Tool-window buttons are a power-user feature; main-toolbar buttons
        # and all entry/header/column editing stay end-user editable.
        if self.component_type == "buttons":
            from ui.sap_toolbar import SapToolbar

            if not isinstance(self.winfo_toplevel(), SapToolbar):
                toolbar = _toolbar_of(self)
                power = getattr(toolbar, "power_user", None) if toolbar else None
                if power is None or not power.get():
                    return

        # Get the original ID for config lookup
        original_id = self._get_original_id()

        # Get the data for this component
        if self.component_type not in self.config_data["customcomponents"]:
            return
        if original_id not in self.config_data["customcomponents"][self.component_type]:
            return

        self.config_data["customcomponents"][self.component_type][original_id]

        # Create save callback
        def save_callback(new_data: dict):
            if original_id not in self.config_data["customcomponents"][self.component_type]:
                self.config_data["customcomponents"][self.component_type][original_id] = {}
            self.config_data["customcomponents"][self.component_type][original_id].update(new_data)
            cfg.save_config(self.config_data)

            # Update this widget
            self._update_from_data(new_data)

        EditModal.from_widget(
            self, self, self.component_type, original_id, self.config_data, save_callback  # type: ignore[arg-type]
        )

    def _get_original_id(self) -> str:
        """Get the original button_id (strip suffixes like _header, _entry, _btn)."""
        if self.button_id is None:
            return ""
        suffixes = ["_header", "_entry", "_btn"]
        original_id = self.button_id
        for suffix in suffixes:
            if original_id.endswith(suffix):
                stripped = original_id[: -len(suffix)]
                # Check if stripped ID exists in config
                if _id_exists(self, stripped):
                    return stripped
        return original_id

    def _update_from_data(self, new_data: dict):
        """Update widget display based on new config data."""
        if self.component_type == "buttons":
            if "text" in new_data:
                self.config(text=new_data["text"])  # type: ignore[attr-defined]
        elif self.component_type == "headers":
            if "text" in new_data:
                self.config(text=new_data["text"])  # type: ignore[attr-defined]
        elif self.component_type == "textinput":
            # For entries, update the associated StringVar if available
            if hasattr(self, "_var") and self._var:
                new_val = new_data.get("default_value", "")
                self._var.set(new_val)


class CustomButton(CustomComponent, tk.Button):
    """Button with optional right-click editing."""

    def __init__(
        self,
        master,
        component_type: ComponentType | None = None,
        button_id: str | None = None,
        editable: bool = True,
        **kwargs,
    ):
        # Apply default styling if not overridden (subtle border)
        if "bg" not in kwargs:
            kwargs["bg"] = ui_styles.COLORS["surface"]
        if "fg" not in kwargs:
            kwargs["fg"] = ui_styles.COLORS["text"]
        if "font" not in kwargs:
            kwargs["font"] = ui_styles.FONTS["body"]
        if "bd" not in kwargs:
            kwargs["bd"] = 1
        if "highlightthickness" not in kwargs:
            kwargs["highlightthickness"] = 1
        if "relief" not in kwargs:
            kwargs["relief"] = "solid"  # Visible border
        if "highlightbackground" not in kwargs:
            kwargs["highlightbackground"] = ui_styles.COLORS["border"]  # Theme border

        super().__init__(
            master,
            component_type=component_type,
            button_id=button_id,
            editable=editable,
            **kwargs,
        )


class CustomEntry(CustomComponent, tk.Entry):
    """Entry field with optional right-click editing."""

    def __init__(
        self,
        master,
        component_type: ComponentType | None = None,
        button_id: str | None = None,
        editable: bool = True,
        **kwargs,
    ):
        # Extract textvariable for our tracking
        textvariable = kwargs.get("textvariable", None)
        super().__init__(
            master,
            component_type=component_type,
            button_id=button_id,
            editable=editable,
            **kwargs,
        )
        # Store reference to StringVar for live updates
        if textvariable is not None:
            self._var = textvariable


class CustomLabel(CustomComponent, tk.Label):
    """Label with optional right-click editing."""

    def __init__(
        self,
        master,
        component_type: ComponentType | None = None,
        button_id: str | None = None,
        editable: bool = True,
        **kwargs,
    ):
        super().__init__(
            master,
            component_type=component_type,
            button_id=button_id,
            editable=editable,
            **kwargs,
        )


class CustomTreeview(CustomComponent, ttk.Treeview):
    """Treeview with optional right-click column configuration.

    Extends ttk.Treeview with:
    - Right-click menu for column configuration
    - Config-driven column setup from JSON
    - Automatic column rebuild when config changes
    """

    status_label: tk.Label | None = None

    def __init__(
        self,
        master,
        component_type: ComponentType | None = None,
        button_id: str | None = None,
        editable: bool = True,
        **kwargs,
    ):
        # Apply default styling
        if "style" not in kwargs:
            kwargs["style"] = "PRTreeview.Treeview"  # Default style, can be overridden
        if "show" not in kwargs:
            kwargs["show"] = "headings"

        # Filter out ttk.Treeview-incompatible kwargs (bg, fg, font, style, etc.)
        incompatible = ("bg", "fg", "font", "bd", "highlightthickness", "relief", "style")
        ttk_kwargs = {k: v for k, v in kwargs.items() if k not in incompatible}

        super().__init__(
            master,
            component_type=component_type,
            button_id=button_id,
            editable=editable,
            **ttk_kwargs,
        )

        # Apply style after creation (ttk.Treeview doesn't accept style in constructor)
        if "style" in kwargs:
            self.configure(style=kwargs["style"])

        # Column configuration right-click is owned by GridColumnManager
        # (bound when a manager is attached to this treeview).

    # Column configuration is owned by GridColumnManager.


class ScriptRunnerMixin:
    """Everything a window needs to join the shared variable system and run
    scripts, in four calls total. This is the ONLY supported way to wire a
    tool window up -- no tool should hand-roll trace_add/set_variable pairs
    or its own copy of the debug-mode branch. Used by both `SapToolbar`
    (the root window) and `BaseToolWindow` (every tool window), so there is
    exactly one implementation of "how a click runs a script", not two.

    What each call owns:
      bind_var(name, tk_var)   -- one field, fully wired, forever
      bind_entry(config_key)   -- same, but reads var_name/default from JSON
      run_script(button_id)    -- read config -> sync -> debug-or-run -> status
      report_status(text, kind)-- the one place status-bar text/color happens

    You should never need to write `trace_add`, `set_variable`, or
    `debugger_window.load_script` yourself in a tool window again. If you
    find yourself doing that, something belongs in this mixin instead.
    """

    # Set by SapToolbar and BaseToolWindow at runtime via MRO
    config_data: dict
    status_label: tk.Label
    engine: SapScriptEngine = None  # type: ignore[assignment]

    _STATUS_STYLE = {
        "info": ("", "text"),
        "success": ("\u2713 ", "success"),
        "error": ("\u2717 ", "error"),
        "warning": ("\u26a0 ", "warning"),
    }

    @property
    def _toolbar(self) -> SapToolbar:
        """The single SapToolbar instance: walks up the widget hierarchy
        to find the root Tk window (which is always SapToolbar in this app).
        Debug mode and the debugger window are root-level, app-wide state --
        every window reaches them through this one property instead of
        poking at `self.master` directly."""
        from ui.sap_toolbar import SapToolbar

        if isinstance(self, SapToolbar):
            return self
        parent = self.master  # type: ignore[attr-defined]
        while parent is not None:
            if isinstance(parent, SapToolbar):
                return parent
            try:
                parent = parent.master
            except AttributeError:
                break
        # No SapToolbar up the chain (e.g. a root window in a unit test) --
        # the root window itself owns the app-wide debug mode/status state.
        return self.master or self  # type: ignore[attr-defined]

    # ---- variables ------------------------------------------------------
    def bind_var(self, var_name: str, tk_var: tk.StringVar | None = None, default: str = ""):
        """Wire a StringVar into `engine.variables[var_name]`, both directions,
        AND register it to be force-synced right before every run_script()
        call. That last part is what makes manual pre-run syncing (the
        biggest source of "works until you click without typing" bugs)
        unnecessary -- you never call set_variable yourself again.

        Pass an existing tk_var to bind it, or omit it to get a fresh one
        back. Either way, call this once per field and you're done.
        """
        if tk_var is None:
            tk_var = tk.StringVar(value=default)
        elif not tk_var.get():
            tk_var.set(default)

        if not hasattr(self, "_bound_vars"):
            self._bound_vars: list[tuple[str, tk.StringVar]] = []
        self._bound_vars.append((var_name, tk_var))
        if not hasattr(self, "_engine_watchers"):
            self._engine_watchers: list[tuple[str, object]] = []
        if not hasattr(self, "_tk_traces"):
            self._tk_traces: list[tuple[tk.StringVar, str]] = []

        self.engine.set_variable(var_name, tk_var.get())
        # UI -> engine, on every keystroke (tracked so destroy() can remove it)
        trace_id = tk_var.trace_add(
            "write", lambda *_: self.engine.set_variable(var_name, tk_var.get())
        )
        self._tk_traces.append((tk_var, trace_id))
        # engine -> UI, guarded so it doesn't fight the trace_add above
        def _engine_to_ui(value, v=tk_var):
            try:
                if v.get() != value:
                    v.set(value)
            except tk.TclError:
                pass  # variable belongs to a destroyed window

        self.engine.watch_variable(var_name, _engine_to_ui)
        self._engine_watchers.append((var_name, _engine_to_ui))
        return tk_var

    def bind_entry(self, config_key: str, var_name: str | None = None):
        """Like bind_var, but reads `var_name` / `default_value` for this
        field straight out of toolbar_config.json. This is what almost
        every entry field wants -- one line, no separate init method:

            self.vendor_var = self.bind_entry("atb_lifnr")
        """
        entry_cfg = cfg.get_textinput_cfg(self.config_data, config_key)
        var_name = var_name or entry_cfg.get("var_name", config_key)
        default_val = entry_cfg.get("default_value", "")
        return self.bind_var(var_name, default=default_val)

    def _sync_bound_vars(self):
        """Force-push every bound var into the engine. Called automatically
        by run_script() -- this is the safety net that replaces every
        hand-written `_sync_*_entries()` method."""
        for var_name, tk_var in getattr(self, "_bound_vars", []):
            self.engine.set_variable(var_name, tk_var.get())

    def watch_engine_var(self, var_name: str, callback):
        """Like engine.watch_variable, but tracked so destroy() unregisters
        it. Use this for any hand-rolled watcher (e.g. status coloring) --
        otherwise the shared engine keeps calling the closed window and the
        next window of the same type breaks on reopen."""
        self.engine.watch_variable(var_name, callback)
        if not hasattr(self, "_engine_watchers"):
            self._engine_watchers: list[tuple[str, object]] = []
        self._engine_watchers.append((var_name, callback))
        return callback

    def unbind_all(self):
        """Unregister every engine watcher and tk trace owned by this window.
        Safe to call when the engine is a test fake without unwatch support.
        """
        engine = getattr(self, "engine", None)
        if engine is not None:
            unwatch = getattr(engine, "unwatch_variable", None)
            for var_name, cb in list(getattr(self, "_engine_watchers", [])):
                try:
                    if unwatch is not None:
                        unwatch(var_name, cb)
                except Exception:
                    pass
            # Backstop: drop any bound-method watcher owned by this window
            # that was registered directly via engine.watch_variable.
            try:
                var_watchers = getattr(engine, "_var_watchers", None)
                if isinstance(var_watchers, dict):
                    for cbs in list(var_watchers.values()):
                        for cb in list(cbs):
                            if getattr(cb, "__self__", None) is self:
                                try:
                                    cbs.remove(cb)
                                except ValueError:
                                    pass
            except Exception:
                pass
        for tk_var, trace_id in list(getattr(self, "_tk_traces", [])):
            try:
                tk_var.trace_remove("write", trace_id)
            except Exception:
                pass
        self._engine_watchers = []
        self._tk_traces = []

    # ---- status -----------------------------------------------------------
    def report_status(self, text: str, kind: str = "info"):
        """The one place status-bar text and color get decided. kind is one
        of info/success/error/warning. Replaces ad-hoc
        `self.status_label.config(text=..., fg=...)` calls scattered per
        window, each picking its own icons/colors."""
        prefix, color_key = self._STATUS_STYLE.get(kind, self._STATUS_STYLE["info"])
        self.status_label.config(text=f"{prefix}{text}", fg=ui_styles.COLORS[color_key])

    # ---- running scripts ----------------------------------------------------
    def run_script(self, button_id: str, title: str | None = None, on_success=None,
                   on_error=None):
        """The one call that launches a button's script:
          1. read the script text from config
          2. force-sync every bound var (see bind_var)
          3. debug mode on  -> open the debugger, loaded, focused
             debug mode off -> run immediately, then report status

        Pass on_success for custom post-run behaviour (e.g. populating a grid)
        and on_error(script_name, message, line_no, raw) when the script stops on
        a failing SAP line; with on_error the failure is handed over instead of
        on_success.
        """
        btn_cfg = cfg.get_button_cfg(self.config_data, button_id)
        script = btn_cfg.get("script", "")
        script_name = title or btn_cfg.get("script_name") or button_id
        if not script:
            self.report_status(f"No script assigned to '{button_id}'", "warning")
            return

        self._sync_bound_vars()

        toolbar = self._toolbar
        if toolbar.debug_mode.get():
            def save_script(new_script: str):
                self.config_data["customcomponents"]["buttons"][button_id]["script"] = new_script
                cfg.save_config(self.config_data)
            toolbar.open_debugger(script, script_name, on_save=save_script, on_success=on_success)
            return

        # Register callback for run_script() commands in scripts
        def run_script_callback(button_id: str) -> None:
            self.run_script(button_id)

        self.engine.run_script_callbacks.append(run_script_callback)

        try:
            self.engine.run_immediate(script)
        finally:
            # Remove callback after script completes
            if run_script_callback in self.engine.run_script_callbacks:
                self.engine.run_script_callbacks.remove(run_script_callback)

        detail: tuple | None = None
        getter = getattr(self.engine, "last_error_detail", None)
        if callable(getter):
            raw_detail = getter()
            if isinstance(raw_detail, tuple) and len(raw_detail) == 3:
                detail = raw_detail
        elif getattr(self.engine, "_last_error", None):
            detail = (self.engine._last_error, None, "")

        if detail and on_error:
            message, line_no, raw = detail
            on_error(script_name, message, line_no, raw)
        elif on_success:
            # Historical behaviour: on_success also runs when the script stopped
            # on an error, so callers that inspect engine._last_error still work.
            on_success()
        elif detail:
            message, line_no, _raw = detail
            where = f" at line {line_no}" if line_no else ""
            self.report_status(f"'{script_name}' failed{where}: {message}", "error")
        else:
            self.report_status(f"Ran '{script_name}'", "success")


class BaseToolWindow(ScriptRunnerMixin, tk.Toplevel):
    """Base class for all tool windows.

    Provides:
    - Automatic config_data loading via _get_config()
    - Access to Custom* widgets
    - Common initialization including status bar
    - Windows title bar color (if on Windows)
    - The whole variable/script-running system, via ScriptRunnerMixin --
      see that class before writing any tool-window init code.
    """

    def __init__(self, parent, engine, titlebar_color=None, modal=False):
        super().__init__(parent)
        self.engine = engine
        self.config_data = _get_config()
        self.config(bg=ui_styles.COLORS["bg"])  # Match window background
        self.pack_propagate(False)

        # Make Custom* widgets available as class attributes (needed by subclass
        # __init__ methods before they call _show())
        from .custom_widgets import CustomButton, CustomEntry, CustomLabel, CustomTreeview

        self.CustomButton = CustomButton
        self.CustomEntry = CustomEntry
        self.CustomLabel = CustomLabel
        self.CustomTreeview = CustomTreeview

        # Set up modality while hidden (avoid DWM redraw flash from
        # calling transient() on an already-visible window)
        if modal:
            self.transient(parent)
            self.grab_set()

        # Hide window so subclasses can build UI before first paint
        self.withdraw()
        self.update_idletasks()
        self._set_titlebar_color(titlebar_color)

    def _show(self):
        """Reveal the fully-built window after all widgets are added.

        First show centers a sized window on the work area of the monitor
        holding the toolbar (see window_chrome). Never re-centers (user moves
        win), and zoomed windows (PRH owns its screen) are left alone."""
        from .window_chrome import center_on_toolbar, correct_for_decorations

        center_on_toolbar(self, self.master)
        self.deiconify()
        self.lift()
        self.focus_force()
        correct_for_decorations(self, self.master)

    def _set_titlebar_color(self, color=None):
        """Set the Windows title bar colour (default: primary blue)."""
        from .window_chrome import apply_titlebar_color

        apply_titlebar_color(self, color or ui_styles.COLORS["primary"])

    def destroy(self):
        """Unregister engine watchers/traces before tearing down widgets, so
        reopening the same tool window starts clean instead of tripping over
        the closed window's callbacks on the shared engine."""
        try:
            self.unbind_all()
        except Exception:
            pass
        super().destroy()

    def _build_status_bar(self, text: str = "Ready"):
        """Create a standard status bar matching toolbar style."""
        from .ui_styles import styled_status_bar

        bar = styled_status_bar(self)  # type: ignore[arg-type]
        bar.pack(fill="x", padx=ui_styles.SPACING["normal"], pady=ui_styles.SPACING["tight"])
        self.status_label = styled_label(
            bar, text, anchor="w", font=ui_styles.FONTS["body"], bg=ui_styles.COLORS["surface"]
        )
        self.status_label.pack(side="left", fill="x", expand=True, padx=8)
        return bar, self.status_label
