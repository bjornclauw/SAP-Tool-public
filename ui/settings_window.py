"""SettingsWindow -- the app's Settings helper modal.

Three tabs: General, Tool windows and Advanced. The tool catalogue is a
master-detail view: the left list shows every tool and its state, the right
pane explains the selected tool and carries the single Enabled switch.
Every change applies immediately; Close only dismisses the window.
"""

from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from core import i18n, user_settings
from core.updater import read_version

from . import ui_styles
from .ui_styles import (
    COLORS,
    FONTS,
    SPACING,
    styled_button,
    styled_entry,
    styled_frame,
    styled_label,
    surface_panel,
)
from .window_chrome import show_centered

APP_ROOT = Path(__file__).resolve().parent.parent
SCREENSHOT_DIR = APP_ROOT / "assets" / "screenshots"

# What the detail pane says about each tool. Name, description and opener
# still come from SapToolbar's TOOL_CATALOGUE; this is the extra copy.
TOOL_DETAILS = {
    "purchase_request": {
        "bullets": (
            "Build a purchase request from the open work order.",
            "Attach quotes and photos before sending to SAP.",
            "Look up existing ATBs, orders and attachments.",
        ),
        "screenshot": "purchase_request.png",
    },
    "print_orders": {
        "bullets": (
            "Load the IW38 list or the order open in IW32.",
            "Print the selected orders to PDF in one run.",
            "Retry failures and keep the last runs as history.",
        ),
        "screenshot": "print_orders.png",
    },
    "sap_import": {
        "bullets": (
            "Upload work orders and photos to BCsys.",
            "Queue rows, auto-assign revisions and retry failures.",
            "Watch progress without leaving the toolbar.",
        ),
        "screenshot": "sap_import.png",
    },
    "planner": {
        "bullets": (
            "Plan the day across teams on a drag-and-drop board.",
            "Preview data is local; live SAP wiring is still being built.",
            "Only shown when Power User is on.",
        ),
        "screenshot": "planner.png",
        "requires_power_user": True,
    },
    "revisions": {
        "bullets": (
            "Manage the revision week and its production lines.",
            "Generate downtime and backlog revisions.",
            "Shared with SAP Import's auto-assign.",
        ),
        "screenshot": "revisions.png",
    },
}

TABS = (
    ("general", "General"),
    ("tools", "Tool windows"),
    ("advanced", "Advanced"),
    ("update", "Update"),
)


class SettingsWindow(tk.Toplevel):
    """One settings window at a time; every change applies immediately."""

    def __init__(self, toolbar, catalogue):
        super().__init__(toolbar)
        self.toolbar = toolbar
        self.catalogue = catalogue
        self._tools = {row[0]: row for row in catalogue}
        self._tool_vars: dict[str, tk.BooleanVar] = {}
        self._tool_rows: dict[str, dict] = {}
        self._preview_cache: dict[str, tk.PhotoImage | None] = {}
        self._selected_tool = next(iter(self._tools))
        self._tab = "general"
        self._tab_buttons: dict[str, tuple[tk.Label, tk.Frame]] = {}
        self._tab_frames: dict[str, tk.Frame] = {}
        self._power_trace = None
        self._power_after = None
        self._update_pane = None

        self.withdraw()
        self.title("Settings")
        self.configure(bg=COLORS["bg"])
        self.geometry("900x660")
        self.minsize(820, 640)
        self.resizable(True, True)

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close(), "+")
        self._power_trace = toolbar.power_user.trace_add(
            "write", self._on_power_var)
        self.bind("<Destroy>", self._on_destroy, "+")

        show_centered(self, ui_styles.SUPPORT_COLOR, toolbar)

    # ---- lifecycle ------------------------------------------------------
    def close(self):
        try:
            self._save_import_settings()
        except Exception:
            pass
        self.destroy()

    def _on_destroy(self, event):
        if event.widget is not self:
            return
        try:
            self._save_import_settings()
        except Exception:
            pass
        if self._update_pane is not None:
            self._update_pane.close()
        if self._power_after is not None:
            try:
                self.after_cancel(self._power_after)
            except Exception:
                pass
            self._power_after = None
        if self._power_trace is not None:
            try:
                self.toolbar.power_user.trace_remove(
                    "write", self._power_trace)
            except Exception:
                pass
            self._power_trace = None
        if getattr(self.toolbar, "_dock_settings_win", None) is self:
            self.toolbar._dock_settings_win = None

    def _on_power_var(self, *_):
        # Deferred: the trace fires before the checkbutton command returns,
        # and refreshing destroys that checkbutton.
        if self._power_after is None:
            try:
                self._power_after = self.after(0, self._on_power_changed)
            except tk.TclError:
                self._power_after = None

    def _on_power_changed(self):
        self._power_after = None
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self._sync_tool_vars()
        self._rebuild_advanced()
        self._select_tool(self._selected_tool)

    def _sync_tool_vars(self):
        """Mirror external tool changes (e.g. Planner off with Power User)."""
        for key, var in self._tool_vars.items():
            var.set(key in self.toolbar._tools_enabled)
        for key in self._tool_rows:
            self._refresh_row(key)
        self._refresh_counts()

    # ---- shell ----------------------------------------------------------
    def _build(self):
        outer = styled_frame(self)
        outer.pack(fill="both", expand=True)
        self._build_header(outer)
        self._build_tabs(outer)

        self._content = tk.Frame(outer, bg=COLORS["bg"])
        self._content.pack(
            fill="both", expand=True,
            padx=SPACING["comfortable"],
            pady=(SPACING["comfortable"], 0),
        )
        self._tab_frames = {
            "general": self._build_general(self._content),
            "tools": self._build_tools(self._content),
            "advanced": self._build_advanced(self._content),
            "update": self._build_update(self._content),
        }
        self._build_footer(outer)
        self._select_tab("general")

    def _build_header(self, parent):
        header = tk.Frame(parent, bg=COLORS["bg"])
        header.pack(fill="x", padx=SPACING["comfortable"],
                    pady=(SPACING["comfortable"], 0))
        left = tk.Frame(header, bg=COLORS["bg"])
        left.pack(side="left")
        tk.Label(
            left, text="Settings",
            font=(FONTS["title"][0], 15, "bold"),
            bg=COLORS["bg"], fg=COLORS["text"], anchor="w",
        ).pack(anchor="w")
        tk.Label(
            left, text="Personalize the toolbar and tool windows",
            font=FONTS["body"], bg=COLORS["bg"],
            fg=COLORS["text_secondary"], anchor="w",
        ).pack(anchor="w", pady=(2, 0))

    def _build_tabs(self, parent):
        bar = tk.Frame(parent, bg=COLORS["bg"])
        bar.pack(fill="x", padx=SPACING["comfortable"],
                 pady=(SPACING["comfortable"], 0))
        for key, label in TABS:
            holder = tk.Frame(bar, bg=COLORS["bg"], cursor="hand2")
            holder.pack(side="left", padx=(0, 20))
            text = tk.Label(
                holder, text=label, font=FONTS["body"],
                bg=COLORS["bg"], fg=COLORS["text_secondary"],
                cursor="hand2",
            )
            text.pack()
            underline = tk.Frame(holder, height=2, bg=COLORS["bg"])
            underline.pack(fill="x", pady=(4, 0))
            for widget in (holder, text):
                widget.bind("<Button-1>",
                            lambda _e, k=key: self._select_tab(k))
            self._tab_buttons[key] = (text, underline)
        tk.Frame(
            parent, height=1, bg=COLORS["border"],
        ).pack(fill="x", padx=SPACING["comfortable"], pady=(6, 0))

    def _select_tab(self, key: str):
        self._tab = key
        for tab_key, (text, underline) in self._tab_buttons.items():
            selected = tab_key == key
            text.configure(
                fg=COLORS["text"] if selected else COLORS["text_secondary"],
                font=(FONTS["body"][0], FONTS["body"][1],
                      "bold" if selected else "normal"),
            )
            underline.configure(
                bg=COLORS["primary"] if selected else COLORS["bg"])
        for tab_key, frame in self._tab_frames.items():
            if tab_key == key:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
        if key == "tools":
            self._refresh_counts()
        elif key == "advanced":
            self._rebuild_advanced()
        elif key == "update":
            self._ensure_update_pane()
            self._update_pane.ensure_checked()

    def open_update_tab(self, force_check: bool = False):
        """Show the Update tab; the footer and Settings menu land here."""
        self._select_tab("update")
        self._ensure_update_pane()
        if force_check:
            self._update_pane.start_check()

    def open_tools_tab(self):
        """Show the Tool windows tab (the helper menu's footer link)."""
        self._select_tab("tools")

    def _build_update(self, parent) -> tk.Frame:
        frame = tk.Frame(parent, bg=COLORS["bg"])
        return frame

    def _ensure_update_pane(self):
        if self._update_pane is not None:
            return
        from core import updater

        from .update_pane import UpdatePane

        self._update_pane = UpdatePane(
            self._tab_frames["update"],
            is_busy=self.toolbar._update_blocker,
            on_exit=self.toolbar._on_close,
            previous_fn=updater.previous_update,
        )
        self._update_pane.pack(fill="both", expand=True)

    # ---- general tab ----------------------------------------------------
    def _build_general(self, parent) -> tk.Frame:
        frame = tk.Frame(parent, bg=COLORS["bg"])
        saved = user_settings.load_user_settings()

        behaviour_card, behaviour = surface_panel(frame)
        behaviour_card.pack(fill="x", pady=(0, SPACING["comfortable"]))
        self._section_title(behaviour, "Behaviour", bg=COLORS["surface"])

        lang_row = tk.Frame(behaviour, bg=COLORS["surface"])
        lang_row.pack(fill="x", pady=(SPACING["normal"], SPACING["tight"]))
        styled_label(
            lang_row, i18n.t("main.settings.language"),
            font=FONTS["body"], bg=COLORS["surface"],
        ).pack(side="left")

        codes = [i18n.AUTO_LANGUAGE, *i18n.available_languages()]
        labels = {
            code: (i18n.t("main.settings.language_auto")
                   if code == i18n.AUTO_LANGUAGE
                   else i18n.language_label(code))
            for code in codes
        }
        current = str(saved.get("ui_language", i18n.AUTO_LANGUAGE))
        if current not in codes:
            current = i18n.AUTO_LANGUAGE
        self._language_var = tk.StringVar(value=labels[current])
        language_box = ttk.Combobox(
            lang_row, textvariable=self._language_var, state="readonly",
            values=[labels[code] for code in codes],
            font=FONTS["body"], width=24,
        )
        language_box.pack(side="right")
        language_box.bind("<<ComboboxSelected>>", self._on_language_picked)

        self._checkbutton(
            behaviour, "Always on Top", self.toolbar.always_on_top,
            self.toolbar._toggle_always_on_top, bg=COLORS["surface"],
        ).pack(anchor="w", pady=(SPACING["tight"], 0))
        styled_label(
            behaviour, "Keeps the main toolbar above other windows.",
            font=FONTS["small"], fg=COLORS["text_secondary"],
            bg=COLORS["surface"],
        ).pack(anchor="w", padx=(20, 0), pady=(2, 0))
        styled_label(
            behaviour, "Changes apply immediately.",
            font=FONTS["small"], fg=COLORS["text_secondary"],
            bg=COLORS["surface"],
        ).pack(anchor="w", pady=(SPACING["normal"], 0))

        import_card, sap = surface_panel(frame)
        import_card.pack(fill="x")
        self._section_title(sap, "SAP", bg=COLORS["surface"])
        self._import_pg_var = tk.StringVar(
            value=str(saved.get("si_pg", "") or ""))
        self._import_system_var = tk.StringVar(
            value=str(saved.get("si_system", "PRD400") or ""))

        pg_row = tk.Frame(sap, bg=COLORS["surface"])
        pg_row.pack(fill="x", pady=(SPACING["tight"], 0))
        styled_label(
            pg_row, "Planner group", font=FONTS["body"],
            bg=COLORS["surface"],
        ).pack(side="left")
        pg_entry = styled_entry(
            pg_row, textvariable=self._import_pg_var, width=24)
        pg_entry.pack(side="right")
        pg_entry.bind("<FocusOut>", self._save_import_settings)
        pg_entry.bind("<Return>", self._save_import_settings)
        styled_label(
            sap, "Free text. Leave empty for the default planner group.",
            font=FONTS["small"], fg=COLORS["text_secondary"],
            bg=COLORS["surface"],
        ).pack(anchor="w", pady=(2, SPACING["normal"]))

        system_row = tk.Frame(sap, bg=COLORS["surface"])
        system_row.pack(fill="x")
        styled_label(
            system_row, "SAP system", font=FONTS["body"],
            bg=COLORS["surface"],
        ).pack(side="left")
        system_entry = styled_entry(
            system_row, textvariable=self._import_system_var, width=24)
        system_entry.pack(side="right")
        system_entry.bind("<FocusOut>", self._save_import_settings)
        system_entry.bind("<Return>", self._save_import_settings)
        styled_label(
            sap, "Free text. The SAP system identifier.",
            font=FONTS["small"], fg=COLORS["text_secondary"],
            bg=COLORS["surface"],
        ).pack(anchor="w", pady=(2, 0))
        return frame

    def _on_language_picked(self, _event=None):
        selected = self._language_var.get()
        codes = [i18n.AUTO_LANGUAGE, *i18n.available_languages()]
        labels = {
            code: (i18n.t("main.settings.language_auto")
                   if code == i18n.AUTO_LANGUAGE
                   else i18n.language_label(code))
            for code in codes
        }
        code = next(
            (candidate for candidate in codes
             if labels[candidate] == selected),
            i18n.AUTO_LANGUAGE,
        )
        i18n.set_language(code)
        self.toolbar._save_user_settings({"ui_language": code})
        self.toolbar.report_status(
            i18n.t("main.settings.language_note"), "info")

    def _save_import_settings(self, _event=None):
        """Persist the two SAP Import text fields where the import reads them."""
        try:
            pg = self._import_pg_var.get().strip()
            system = self._import_system_var.get().strip()
        except (AttributeError, tk.TclError):
            return
        current = user_settings.load_user_settings()
        if (str(current.get("si_pg", "")) == pg
                and str(current.get("si_system", "")) == system):
            return
        self.toolbar._save_user_settings(
            {"si_pg": pg, "si_system": system})
        self.toolbar.report_status("SAP settings saved", "info")

    # ---- tool windows tab ----------------------------------------------
    def _build_tools(self, parent) -> tk.Frame:
        frame = tk.Frame(parent, bg=COLORS["bg"])

        left = tk.Frame(frame, bg=COLORS["bg"], width=300)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        right = tk.Frame(frame, bg=COLORS["bg"])
        right.pack(side="left", fill="both", expand=True,
                   padx=(SPACING["comfortable"], 0))

        header = tk.Frame(left, bg=COLORS["bg"])
        header.pack(fill="x")
        self._section_title(header, "Tool windows")
        self._count_label = styled_label(
            header, "", font=FONTS["small"],
            fg=COLORS["text_secondary"], bg=COLORS["bg"],
        )
        self._count_label.pack(side="right")

        self._list = tk.Frame(left, bg=COLORS["bg"])
        self._list.pack(fill="both", expand=True, pady=(SPACING["normal"], 0))
        for key, _menu, name, description, _default, _opener in self.catalogue:
            self._build_tool_row(key, name, description)

        self._detail_host = tk.Frame(right, bg=COLORS["bg"])
        self._detail_host.pack(fill="both", expand=True)
        self._select_tool(self._selected_tool)
        self._refresh_counts()
        return frame

    def _build_tool_row(self, key: str, name: str, description: str):
        var = tk.BooleanVar(value=key in self.toolbar._tools_enabled)
        self._tool_vars[key] = var

        row = tk.Frame(self._list, bg=COLORS["bg"], cursor="hand2")
        row.pack(fill="x", pady=(0, 2))
        accent = tk.Frame(row, width=3, bg=COLORS["bg"])
        accent.pack(side="left", fill="y")
        body = tk.Frame(row, bg=COLORS["bg"])
        body.pack(side="left", fill="x", expand=True,
                  padx=(SPACING["normal"], SPACING["tight"]),
                  pady=SPACING["tight"])
        name_lbl = tk.Label(
            body, text=name, font=FONTS["header"], bg=COLORS["bg"],
            fg=COLORS["text"], anchor="w",
        )
        name_lbl.pack(fill="x")
        desc_lbl = tk.Label(
            body, text=description, font=FONTS["small"], bg=COLORS["bg"],
            fg=COLORS["text_secondary"], anchor="w", justify="left",
            wraplength=220,
        )
        desc_lbl.pack(fill="x")
        badge = tk.Label(
            row, text="ON", font=(FONTS["small"][0], 7, "bold"),
            bg=COLORS["success_light"], fg=COLORS["success"],
            padx=6, pady=2,
        )
        badge.pack(side="right", padx=(0, SPACING["tight"]))

        self._tool_rows[key] = {
            "row": row, "accent": accent, "body": body,
            "name": name_lbl, "desc": desc_lbl, "badge": badge,
        }
        for widget in (row, accent, body, name_lbl, desc_lbl, badge):
            widget.bind("<Button-1>",
                        lambda _e, k=key: self._select_tool(k))
            widget.bind("<Enter>", lambda _e, k=key: self._hover_row(k, True))
            widget.bind("<Leave>", lambda _e, k=key: self._hover_row(k, False))
        tk.Frame(self._list, height=1, bg=COLORS["border_light"]).pack(fill="x")
        self._refresh_row(key)

    def _hover_row(self, key: str, active: bool):
        if key == self._selected_tool:
            return
        parts = self._tool_rows.get(key)
        if parts is None:
            return
        bg = COLORS["surface_hover"] if active else COLORS["bg"]
        for name in ("row", "body", "name", "desc"):
            parts[name].configure(bg=bg)

    def _select_tool(self, key: str):
        if key not in self._tools:
            return
        self._selected_tool = key
        for tool_key, parts in self._tool_rows.items():
            selected = tool_key == key
            bg = COLORS["info_light"] if selected else COLORS["bg"]
            for name in ("row", "body", "name", "desc"):
                parts[name].configure(bg=bg)
            parts["accent"].configure(
                bg=COLORS["info"] if selected else COLORS["bg"])
        self._rebuild_detail()

    def _refresh_row(self, key: str):
        parts = self._tool_rows.get(key)
        var = self._tool_vars.get(key)
        if parts is None or var is None:
            return
        on = bool(var.get())
        paused = (key == "planner" and on
                  and not self.toolbar.power_user.get())
        if paused:
            parts["badge"].configure(
                text="PAUSED", bg=COLORS["warning_light"],
                fg=COLORS["warning"])
        else:
            parts["badge"].configure(
                text="ON" if on else "OFF",
                bg=COLORS["success_light"] if on else COLORS["surface"],
                fg=COLORS["success"] if on else COLORS["text_secondary"],
            )

    def _refresh_counts(self):
        if not hasattr(self, "_count_label"):
            return
        on = sum(
            1 for key, var in self._tool_vars.items()
            if var.get()
            and not (key == "planner" and not self.toolbar.power_user.get())
        )
        total = len(self._tool_vars)
        self._count_label.configure(text=f"{on} of {total} on")

    def _on_tool_toggled(self, key: str):
        chosen = [row[0] for row in self.catalogue
                  if self._tool_vars[row[0]].get()]
        self.toolbar._apply_enabled_tools(chosen)
        self.toolbar.report_status("Tool windows updated", "info")
        self._refresh_row(key)
        self._refresh_counts()
        self._refresh_detail_state()

    def _rebuild_detail(self):
        for child in self._detail_host.winfo_children():
            child.destroy()
        key = self._selected_tool
        row = self._tools.get(key)
        if row is None:
            return
        _menu, name, description, _default, opener = row[1:]
        details = TOOL_DETAILS.get(key, {})

        frame = tk.Frame(self._detail_host, bg=COLORS["bg"])
        frame.pack(fill="both", expand=True)

        header = tk.Frame(frame, bg=COLORS["bg"])
        header.pack(fill="x")
        tk.Label(
            header, text=name, font=(FONTS["header"][0], 12, "bold"),
            bg=COLORS["bg"], fg=COLORS["text"], anchor="w",
        ).pack(side="left")
        self._enabled_cb = self._checkbutton(
            header, "Enabled", self._tool_vars[key],
            lambda k=key: self._on_tool_toggled(k),
        )
        self._enabled_cb.pack(side="right")

        tk.Label(
            frame, text=description, font=FONTS["body"],
            bg=COLORS["bg"], fg=COLORS["text_secondary"],
            anchor="w", justify="left", wraplength=520,
        ).pack(fill="x", pady=(4, SPACING["normal"]))

        bullets = tk.Frame(frame, bg=COLORS["bg"])
        bullets.pack(fill="x")
        for bullet in details.get("bullets", ()):
            tk.Label(
                bullets, text=f"\u2022 {bullet}", font=FONTS["body"],
                bg=COLORS["bg"], fg=COLORS["text"], anchor="w",
                justify="left", wraplength=520,
            ).pack(fill="x", pady=1)

        self._build_preview(frame, details.get("screenshot"))
        self._detail_note = tk.Label(
            frame, text="", font=FONTS["small"], bg=COLORS["bg"],
            fg=COLORS["text_secondary"], anchor="w", justify="left",
            wraplength=520,
        )
        self._detail_note.pack(fill="x", pady=(SPACING["normal"], 0))

        actions = tk.Frame(frame, bg=COLORS["bg"])
        actions.pack(fill="x", pady=(SPACING["normal"], 0))
        self._open_btn = styled_button(
            actions, "Open now", command=lambda k=key: self._open_tool(k))
        self._open_btn.pack(side="left")
        self._power_btn = None
        if details.get("requires_power_user"):
            self._power_btn = styled_button(
                actions, "Enable Power User",
                command=self._enable_power_user,
            )
            self._power_btn.pack(side="left", padx=(SPACING["tight"], 0))
        self._current_opener = opener
        self._refresh_detail_state()

    def _refresh_detail_state(self):
        key = self._selected_tool
        row = self._tools.get(key)
        if row is None:
            return
        details = TOOL_DETAILS.get(key, {})
        var = self._tool_vars.get(key)
        if var is None or self._enabled_cb is None:
            return
        needs_power = bool(details.get("requires_power_user")
                           and not self.toolbar.power_user.get())
        if needs_power:
            self._enabled_cb.configure(
                state="disabled", fg=COLORS["text_disabled"])
        else:
            self._enabled_cb.configure(state="normal", fg=COLORS["text"])

        win = self.toolbar.tool_windows.get(key)
        try:
            is_open = win is not None and win.winfo_exists()
        except tk.TclError:
            is_open = False

        if needs_power:
            if var.get():
                note = ("Requires Power User. It stays enabled but is hidden "
                        "from the menu until Power User is on.")
            else:
                note = ("Requires Power User. Turn it on in Advanced, "
                        "then enable this tool.")
            color = COLORS["warning"]
        elif is_open and var.get():
            note = ("This tool is open now. Turning it off closes it.")
            color = COLORS["warning"]
        elif not var.get():
            note = "Enable this tool to open it."
            color = COLORS["text_secondary"]
        else:
            note = ""
            color = COLORS["text_secondary"]
        self._detail_note.configure(text=note, fg=color)

        enabled = bool(var.get()) and not needs_power
        self._open_btn.configure(
            state="normal" if enabled else "disabled",
            fg=COLORS["text"] if enabled else COLORS["text_disabled"],
        )
        if self._power_btn is not None:
            self._power_btn.configure(
                state="normal" if needs_power else "disabled",
                fg=COLORS["text"] if needs_power else COLORS["text_disabled"],
            )

    def _build_preview(self, parent, filename: str | None):
        # Fixed stage: the button below must not move when a tool's screenshot
        # has a different aspect ratio.
        frame = tk.Frame(
            parent, bg=COLORS["surface"], height=260,
            highlightbackground=COLORS["border"], highlightthickness=1,
        )
        frame.pack(fill="x", pady=(SPACING["normal"], 0))
        frame.pack_propagate(False)
        image = self._load_preview(filename)
        if image is not None:
            tk.Label(frame, image=image, bg=COLORS["surface"]).place(
                relx=0.5, rely=0.5, anchor="center")
        else:
            tk.Label(
                frame, text="Preview unavailable",
                font=FONTS["small"], bg=COLORS["surface"],
                fg=COLORS["text_secondary"],
            ).place(relx=0.5, rely=0.5, anchor="center")

    def _load_preview(self, filename: str | None) -> tk.PhotoImage | None:
        if not filename:
            return None
        if filename in self._preview_cache:
            return self._preview_cache[filename]
        image = None
        try:
            from PIL import Image, ImageTk

            path = SCREENSHOT_DIR / filename
            if path.exists():
                picture = Image.open(path)
                picture.thumbnail((480, 260), Image.Resampling.LANCZOS)
                image = ImageTk.PhotoImage(picture, master=self)
        except Exception:
            image = None
        self._preview_cache[filename] = image
        return image

    def _open_tool(self, key: str):
        var = self._tool_vars.get(key)
        if var is None or not var.get():
            return
        opener = getattr(self.toolbar, self._tools[key][5], None)
        if callable(opener):
            try:
                opener()
            except Exception as exc:
                self.toolbar.report_status(
                    f"Could not open {self._tools[key][2]}: {exc}", "error")
        self._refresh_detail_state()

    def _enable_power_user(self):
        if not self.toolbar.power_user.get():
            self.toolbar.power_user.set(True)
            self.toolbar._toggle_power_user()

    # ---- advanced tab ---------------------------------------------------
    def _build_advanced(self, parent) -> tk.Frame:
        frame = tk.Frame(parent, bg=COLORS["bg"])
        self._advanced_host = frame
        self._rebuild_advanced()
        return frame

    def _rebuild_advanced(self):
        host = self._advanced_host
        for child in host.winfo_children():
            child.destroy()
        developer_card, developer = surface_panel(host)
        developer_card.pack(fill="x", pady=(0, SPACING["comfortable"]))
        self._section_title(developer, "Developer", bg=COLORS["surface"])
        self._checkbutton(
            developer, "Power User", self.toolbar.power_user,
            self.toolbar._toggle_power_user, bg=COLORS["surface"],
        ).pack(anchor="w")
        if self.toolbar.power_user.get():
            self._checkbutton(
                developer, "Debug Mode", self.toolbar.debug_mode,
                self.toolbar._toggle_debug_mode, bg=COLORS["surface"],
            ).pack(anchor="w", pady=(SPACING["tight"], 0))
            self._checkbutton(
                developer, "Debug Console", self.toolbar.show_console,
                self.toolbar._toggle_console, bg=COLORS["surface"],
            ).pack(anchor="w", pady=(SPACING["tight"], 0))
            self._checkbutton(
                developer, "Fake SAP", self.toolbar.fake_sap,
                self.toolbar._toggle_fake, bg=COLORS["surface"],
            ).pack(anchor="w", pady=(SPACING["tight"], 0))
        else:
            styled_label(
                developer, "Developer toggles appear when Power User is on.",
                font=FONTS["small"], fg=COLORS["text_secondary"],
                bg=COLORS["surface"],
            ).pack(anchor="w", pady=(SPACING["tight"], 0))

        maintenance_card, maintenance = surface_panel(host)
        maintenance_card.pack(fill="x")
        self._section_title(
            maintenance, "Maintenance", bg=COLORS["surface"])
        row = tk.Frame(maintenance, bg=COLORS["surface"])
        row.pack(fill="x", pady=(SPACING["tight"], 0))
        styled_button(
            row, "Open data folder",
            command=self._open_data_folder,
        ).pack(side="left")
        styled_button(
            row, "Reset tool choices",
            command=self._reset_tool_choices,
        ).pack(side="left", padx=(SPACING["tight"], 0))

    def _open_data_folder(self):
        path = APP_ROOT / "data"
        try:
            path.mkdir(exist_ok=True)
            os.startfile(path)
        except Exception as exc:
            self.toolbar.report_status(
                f"Could not open data folder: {exc}", "error")

    def _reset_tool_choices(self):
        if not messagebox.askyesno(
            "Reset tool choices",
            "Show the default tool windows again?",
            parent=self,
        ):
            return
        defaults = [row[0] for row in self.catalogue if row[4]]
        for key, var in self._tool_vars.items():
            var.set(key in defaults)
        self.toolbar._apply_enabled_tools(defaults)
        self.toolbar.report_status("Tool windows reset to defaults", "info")
        for key in self._tool_rows:
            self._refresh_row(key)
        self._refresh_counts()
        self._refresh_detail_state()

    # ---- footer ---------------------------------------------------------
    def _build_footer(self, parent):
        row = styled_frame(parent)
        row.pack(side="bottom", fill="x", padx=SPACING["comfortable"],
                 pady=SPACING["normal"])
        tk.Frame(
            parent, height=1, bg=COLORS["border"],
        ).pack(
            side="bottom", fill="x",
            pady=(SPACING["loose"], 0),
        )
        styled_label(
            row, f"SAP Work Order Tool {self._version()}",
            font=FONTS["small"], fg=COLORS["text_secondary"],
        ).pack(side="left")
        styled_button(
            row, "Close", command=self.close,
        ).pack(side="right")
        styled_button(
            row, "Update",
            command=lambda: self.open_update_tab(force_check=True),
        ).pack(side="right", padx=(0, SPACING["tight"]))
        styled_button(
            row, "Quit", command=self.toolbar._on_close,
            fg=COLORS["error"],
        ).pack(side="right", padx=(0, SPACING["tight"]))

    @staticmethod
    def _version() -> str:
        try:
            return read_version(APP_ROOT)
        except Exception:
            return ""

    # ---- small helpers --------------------------------------------------
    @staticmethod
    def _section_title(parent, text: str, bg: str = COLORS["bg"]):
        styled_label(
            parent, text, font=FONTS["header"], bg=bg,
        ).pack(anchor="w", pady=(0, SPACING["tight"]))

    @staticmethod
    def _checkbutton(parent, text, variable, command,
                     bg: str = COLORS["bg"]):
        return tk.Checkbutton(
            parent, text=text, variable=variable, command=command,
            font=FONTS["body"], anchor="w",
            bg=bg, fg=COLORS["text"],
            activebackground=bg,
            activeforeground=COLORS["text"],
            selectcolor=COLORS["surface"],
            highlightthickness=0, bd=0,
        )
