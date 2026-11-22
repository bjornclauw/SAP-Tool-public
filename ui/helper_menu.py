"""HelperMenu -- the toolbar's Helpers fly-out.

One white panel that opens from the helper button on both the docked and
undocked toolbar. It lists the enabled tool windows, marks the open one, and
carries the Settings entry so the main toolbar needs no menu bar.
"""

from __future__ import annotations

import tkinter as tk

from core import i18n

from . import ui_styles
from .monitor import work_area_of

_SHORT_NAMES = {
    "purchase_request": "Purchase Request",
    "print_orders": "Print Orders",
    "sap_import": "SAP Import",
    "planner": "Planner",
    "revisions": "Revisions",
}


class HelperMenu(tk.Toplevel):
    """Card-style helper launcher, anchored under the toolbar's helper button."""

    WIDTH = 320

    def __init__(self, toolbar, catalogue):
        super().__init__(toolbar)
        self.toolbar = toolbar
        self.catalogue = catalogue
        self._closing = False
        self._tool_keys: list[str] = []

        self.withdraw()
        self.overrideredirect(True)
        # A mapped helper card must hold the docked bar visible; the dock's
        # owned-dialog check reads this flag even though the card is
        # overrideredirect (and therefore not always transient).
        self._dock_hold = True
        self.configure(
            bg=ui_styles.COLORS["surface"],
            highlightbackground=ui_styles.COLORS["border_dark"],
            highlightthickness=1,
        )
        try:
            self.transient(toolbar)
            self.attributes("-topmost", True)
        except tk.TclError:
            pass

        self._build()
        self.bind("<Escape>", lambda _e: self.close(), "+")
        self.bind("<FocusOut>", self._on_focus_out, "+")
        self._ready = False
        self._ready_id = self.after(100, self._mark_ready)
        self._show_under_toggle()

    def _mark_ready(self):
        self._ready_id = None
        self._ready = True
        self._close_if_unfocused()

    # ---- lifecycle ------------------------------------------------------
    def close(self):
        if self._closing:
            return
        self._closing = True
        if self._ready_id is not None:
            try:
                self.after_cancel(self._ready_id)
            except tk.TclError:
                pass
            self._ready_id = None
        if getattr(self.toolbar, "_helper_menu", None) is self:
            self.toolbar._helper_menu = None
        try:
            self.toolbar._set_helper_toggle_active(False)
        except Exception:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _on_focus_out(self, _event=None):
        if not self._ready:
            return
        self.after(80, self._close_if_unfocused)

    def _close_if_unfocused(self):
        try:
            if not self.winfo_exists():
                return
            focused = self.focus_get()
        except tk.TclError:
            return
        if focused is None or not str(focused).startswith(str(self)):
            self.close()

    def _show_under_toggle(self):
        self.update_idletasks()
        width = self.WIDTH
        height = self.winfo_reqheight()
        button = self.toolbar._helper_toggle
        try:
            x = button.winfo_rootx()
            # Drop below the whole toolbar, not below the button in the
            # quick-open row: the shortcuts/status rows would cover the card.
            y = (self.toolbar.winfo_rooty()
                 + self.toolbar.winfo_height() + 6)
        except tk.TclError:
            x, y = 100, 100
        mx, my, mw, mh = work_area_of(self.toolbar)
        x = max(mx + 4, min(x, mx + mw - width - 4))
        if y + height > my + mh - 4:
            try:
                y = max(
                    my + 4,
                    self.toolbar.winfo_rooty() - height - 6,
                )
            except tk.TclError:
                y = my + 4
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.deiconify()
        self.lift()
        self.focus_force()
        self.toolbar._set_helper_toggle_active(True)

    # ---- layout ---------------------------------------------------------
    def _build(self):
        body = tk.Frame(self, bg=ui_styles.COLORS["surface"])
        body.pack(
            fill="both", expand=True,
            padx=ui_styles.SPACING["comfortable"],
            pady=ui_styles.SPACING["comfortable"],
        )

        header = tk.Frame(body, bg=ui_styles.COLORS["surface"])
        header.pack(fill="x")
        title = tk.Frame(header, bg=ui_styles.COLORS["surface"])
        title.pack(side="left", fill="x", expand=True)
        tk.Label(
            title, text=i18n.t("helpers.title", "Helpers"),
            font=(ui_styles.FONTS["header"][0], 12, "bold"),
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
            anchor="w",
        ).pack(side="left")
        tk.Label(
            title, text=i18n.t("helpers.subtitle", "Open or focus a tool"),
            font=ui_styles.FONTS["small"],
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text_secondary"],
            anchor="w",
        ).pack(side="left", padx=(8, 0))
        tk.Button(
            header, text="\u2715", command=self.close,
            font=ui_styles.FONTS["body"],
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text_secondary"],
            activebackground=ui_styles.COLORS["surface_hover"],
            activeforeground=ui_styles.COLORS["error"],
            relief="flat", bd=0, highlightthickness=0,
            width=2, cursor="hand2",
        ).pack(side="right")

        for row in self.catalogue:
            key = row[0]
            if key not in self.toolbar._tools_enabled:
                continue
            if key == "planner" and not self.toolbar.power_user.get():
                continue
            self._build_tool_row(body, key, row[1], row[3])

        tk.Frame(
            body, height=1, bg=ui_styles.COLORS["border"],
        ).pack(fill="x", pady=(ui_styles.SPACING["tight"], 0))
        self._build_footer(body)

    def _build_tool_row(self, parent, key, label, description):
        name = i18n.t(
            f"tool.{key}.short", _SHORT_NAMES.get(key, label))
        description = i18n.t(f"tool.{key}.description", description)
        win = self.toolbar.tool_windows.get(key)
        try:
            is_open = win is not None and win.winfo_exists()
        except tk.TclError:
            is_open = False
        color = ui_styles.TOOL_COLORS.get(key, ui_styles.SUPPORT_COLOR)
        bg = (ui_styles.COLORS["surface_hover"] if is_open
              else ui_styles.COLORS["surface"])

        row = tk.Frame(parent, bg=bg, cursor="hand2")
        row.pack(fill="x", pady=(ui_styles.SPACING["tight"], 0))
        accent = tk.Frame(row, width=5, bg=color)
        accent.pack(side="left", fill="y")
        content = tk.Frame(row, bg=bg)
        content.pack(
            side="left", fill="x", expand=True,
            padx=(ui_styles.SPACING["normal"], 0),
            pady=ui_styles.SPACING["tight"],
        )
        name_lbl = tk.Label(
            content, text=name, font=ui_styles.FONTS["header"],
            bg=bg, fg=ui_styles.COLORS["text"], anchor="w",
        )
        name_lbl.pack(fill="x")
        desc_lbl = tk.Label(
            content, text=description, font=ui_styles.FONTS["small"],
            bg=bg, fg=ui_styles.COLORS["text_secondary"],
            anchor="w", justify="left", wraplength=235,
        )
        desc_lbl.pack(fill="x")
        side = tk.Label(
            row, text=i18n.t("helpers.open", "Open") if is_open else "",
            font=ui_styles.FONTS["small"],
            bg=bg, fg=ui_styles.COLORS["text_secondary"],
            padx=ui_styles.SPACING["tight"],
        )
        side.pack(side="right", anchor="n", pady=ui_styles.SPACING["tight"])

        self._tool_keys.append(key)
        for widget in (row, accent, content, name_lbl, desc_lbl, side):
            widget.bind("<Button-1>", lambda _e, k=key: self._launch(k))
            widget.bind(
                "<Enter>",
                lambda _e, r=row, b=content, n=name_lbl, d=desc_lbl, s=side,
                base=bg: self._hover_tool(r, b, n, d, s, True, base),
            )
            widget.bind(
                "<Leave>",
                lambda _e, r=row, b=content, n=name_lbl, d=desc_lbl, s=side,
                base=bg: self._hover_tool(r, b, n, d, s, False, base),
            )

    @staticmethod
    def _hover_tool(row, content, name, desc, side, active, base_bg):
        bg = (ui_styles.COLORS["surface_hover"] if active else base_bg)
        try:
            row.configure(bg=bg)
            content.configure(bg=bg)
            name.configure(bg=bg)
            desc.configure(bg=bg)
            side.configure(bg=bg)
        except tk.TclError:
            pass

    def _build_footer(self, parent):
        footer = tk.Frame(parent, bg=ui_styles.COLORS["surface"])
        footer.pack(fill="x", pady=(ui_styles.SPACING["tight"], 0))

        gear = tk.Label(
            footer, text="\u2699", bg=ui_styles.SUPPORT_COLOR, fg="#ffffff",
            font=(ui_styles.FONTS["body"][0], 9, "bold"),
            width=3, padx=2, pady=2, cursor="hand2",
        )
        gear.pack(side="left")
        settings = tk.Label(
            footer, text=i18n.t("settings.title", "Settings"),
            font=ui_styles.FONTS["body"],
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
            anchor="w", cursor="hand2", padx=8,
        )
        settings.pack(side="left", fill="x", expand=True)
        tools = tk.Label(
            footer, text=i18n.t("helpers.tool_windows", "Tool windows \u203a"),
            font=ui_styles.FONTS["small"],
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text_secondary"],
            cursor="hand2",
        )
        tools.pack(side="right")

        for widget in (gear, settings):
            widget.bind("<Button-1>", lambda _e: self._open_settings())
        tools.bind("<Button-1>", lambda _e: self._open_tool_windows())

    # ---- actions --------------------------------------------------------
    def _launch(self, key: str):
        self.close()
        row = next((row for row in self.catalogue if row[0] == key), None)
        if row is not None:
            self.toolbar._focus_or_open(key, row[5])

    def _open_settings(self):
        self.close()
        self.toolbar._open_dock_settings()

    def _open_tool_windows(self):
        self.close()
        self.toolbar._open_settings_tools()
