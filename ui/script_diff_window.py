"""ScriptDiffWindow -- review a tool's per-user script overrides.

Opened from the Settings tool catalogue. Shows the modified scripts of one
tool and a unified diff of the current stock script against the user's saved
(and possibly re-applied) script.
"""

from __future__ import annotations

import difflib
import re
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, ttk

from core import config as cfg
from core import i18n, script_overrides, updater

from . import ui_styles
from .ui_styles import (
    COLORS,
    FONTS,
    SPACING,
    styled_button,
    styled_frame,
    styled_label,
)
from .window_chrome import show_centered


class ScriptDiffWindow(tk.Toplevel):
    """One diff window at a time, owned by the Settings modal."""

    WIDTH = 920
    HEIGHT = 640

    def __init__(
        self,
        parent: tk.Misc,
        tool_key: str,
        on_close: Callable[[], None] | None = None,
        on_change: Callable[[], None] | None = None,
        config_data: dict | None = None,
    ):
        super().__init__(parent)
        self.tool_key = tool_key
        self.on_close = on_close
        self.on_change = on_change
        self.config_data = config_data
        self.shipped = cfg.load_shipped_config()
        self.buttons = self.shipped.get("customcomponents", {}).get(
            "buttons", {})
        self.statuses = script_overrides.statuses_for_tool(
            tool_key, shipped_config=self.shipped)
        self._ids = list(self.statuses)
        self._selected = 0
        self._closing = False

        if not self._ids:
            self.destroy()
            return

        self.withdraw()
        self.title(i18n.t("script_diff.title", "Script changes"))
        self.configure(bg=COLORS["bg"])
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        self.minsize(760, 520)
        self.resizable(True, True)
        self.bind("<Escape>", lambda _e: self.close(), "+")
        self.protocol("WM_DELETE_WINDOW", self.close)
        try:
            self.transient(parent)
        except tk.TclError:
            pass

        self._build()
        show_centered(self, ui_styles.SUPPORT_COLOR, parent)
        self._select(0)

    # ---- lifecycle ------------------------------------------------------
    def close(self):
        if self._closing:
            return
        self._closing = True
        try:
            if callable(self.on_close):
                try:
                    self.on_close()
                except Exception:
                    pass
        finally:
            self.destroy()

    # ---- layout ---------------------------------------------------------
    def _build(self):
        outer = styled_frame(self)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=COLORS["bg"])
        header.pack(fill="x", padx=SPACING["comfortable"],
                    pady=(SPACING["comfortable"], SPACING["normal"]))
        title = tk.Frame(header, bg=COLORS["bg"])
        title.pack(side="left", fill="x", expand=True)
        name = i18n.t(
            f"tool.{self.tool_key}.name",
            self.tool_key.replace("_", " ").title())
        styled_label(
            title, name, font=(FONTS["title"][0], 14, "bold"),
            bg=COLORS["bg"],
        ).pack(anchor="w")
        styled_label(
            title,
            i18n.t("script_diff.subtitle",
                   "Stock script vs your saved script"),
            font=FONTS["body"], fg=COLORS["text_secondary"], bg=COLORS["bg"],
        ).pack(anchor="w", pady=(2, 0))
        tk.Button(
            header, text="\u2715", command=self.close, font=FONTS["body"],
            bg=COLORS["bg"], fg=COLORS["text_secondary"],
            activebackground=COLORS["surface_hover"],
            activeforeground=COLORS["error"],
            relief="flat", bd=0, highlightthickness=0, width=2,
            cursor="hand2",
        ).pack(side="right", anchor="n")

        tk.Frame(outer, height=1, bg=COLORS["border"]).pack(
            fill="x", padx=SPACING["comfortable"])

        body = tk.Frame(outer, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=SPACING["comfortable"],
                  pady=SPACING["comfortable"])

        left = tk.Frame(body, bg=COLORS["bg"], width=270)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        styled_label(
            left, i18n.t("script_diff.scripts", "Scripts"),
            font=FONTS["header"], bg=COLORS["bg"],
        ).pack(anchor="w", pady=(0, SPACING["tight"]))
        self._list = tk.Listbox(
            left, exportselection=False, activestyle="none",
            font=FONTS["body"], bg=COLORS["surface"], fg=COLORS["text"],
            selectbackground=COLORS["info_light"], selectforeground=COLORS["text"],
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=COLORS["border"], highlightcolor=COLORS["info"],
            cursor="hand2",
        )
        self._list.pack(fill="both", expand=True)
        self._list.bind("<<ListboxSelect>>", self._on_list_select)
        for button_id in self._ids:
            _script, status = self.statuses[button_id]
            marker = self._status_text(status)
            name = self._script_name(button_id)
            self._list.insert("end", f"{name}  \u2014  {marker}")

        right = tk.Frame(body, bg=COLORS["bg"])
        right.pack(side="left", fill="both", expand=True,
                   padx=(SPACING["comfortable"], 0))

        header_row = tk.Frame(right, bg=COLORS["bg"])
        header_row.pack(fill="x", pady=(0, SPACING["tight"]))
        self._stock_label = styled_label(
            header_row, self._stock_title(), font=FONTS["small"],
            fg=COLORS["text_secondary"], bg=COLORS["bg"],
        )
        self._stock_label.pack(side="left")
        self._view_edit = i18n.t("script_diff.view_edit", "Your edit")
        self._view_stock = i18n.t("script_diff.view_stock", "Current stock")
        self._view_var = tk.StringVar(value=self._view_edit)
        view_box = ttk.Combobox(
            header_row, textvariable=self._view_var, state="readonly",
            values=[self._view_edit, self._view_stock],
            font=FONTS["small"], width=18,
        )
        view_box.pack(side="right")
        view_box.bind("<<ComboboxSelected>>", self._on_view_changed)

        self._banner = styled_label(
            right,
            i18n.t("script_diff.review_banner",
                   "The shipped script changed under your edit. "
                   "Review the diff and reset it if the stock version is "
                   "what you want."),
            font=FONTS["small"], fg=COLORS["warning"], bg=COLORS["warning_light"],
            justify="left", anchor="w",
        )

        text_frame = tk.Frame(
            right, bg=COLORS["surface"],
            highlightbackground=COLORS["border"], highlightthickness=1,
        )
        self._text_frame = text_frame
        text_frame.pack(fill="both", expand=True)
        self._text = tk.Text(
            text_frame, wrap="none", font=FONTS["mono"],
            bg=COLORS["surface"], fg=COLORS["text"],
            relief="flat", bd=0, highlightthickness=0,
            padx=SPACING["normal"], pady=SPACING["normal"],
            state="disabled", cursor="arrow",
        )
        vscroll = tk.Scrollbar(
            text_frame, orient="vertical", command=self._text.yview)
        hscroll = tk.Scrollbar(
            text_frame, orient="horizontal", command=self._text.xview)
        self._text.configure(
            yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)
        vscroll.pack(side="right", fill="y")
        hscroll.pack(side="bottom", fill="x")
        self._text.pack(side="left", fill="both", expand=True)
        self._text.tag_configure("del", foreground=COLORS["error"])
        self._text.tag_configure("add", foreground=COLORS["success"])
        self._text.tag_configure("hunk", foreground=COLORS["info"])
        self._text.tag_configure("meta", foreground=COLORS["text_secondary"])

        footer = styled_frame(outer)
        footer.pack(side="bottom", fill="x", padx=SPACING["comfortable"],
                    pady=SPACING["normal"])
        styled_button(
            footer, i18n.t("script_diff.reset_script", "Reset this script"),
            command=self._reset_selected, fg=COLORS["error"],
        ).pack(side="left")
        self._status_label = styled_label(
            footer, "", font=FONTS["small"], fg=COLORS["success"],
            bg=COLORS["bg"],
        )
        self._status_label.pack(side="left", padx=(SPACING["tight"], 0))
        styled_button(
            footer, i18n.t("script_diff.close", "Close"), command=self.close,
        ).pack(side="right")
        styled_button(
            footer,
            i18n.t("script_diff.reset_tool",
                   "Reset all scripts for this tool"),
            command=self._reset_tool, fg=COLORS["error"],
        ).pack(side="right", padx=(0, SPACING["tight"]))

    # ---- reset ----------------------------------------------------------
    def _reset_selected(self):
        if not self._ids:
            return
        button_id = self._ids[self._selected]
        try:
            removed = script_overrides.clear_overrides([button_id])
        except OSError as exc:
            messagebox.showerror(
                i18n.t("script_diff.reset_title", "Reset scripts"),
                str(exc), parent=self)
            return
        if removed == 0:
            return
        self._restore_live([button_id])
        self._reload()
        try:
            alive = bool(self.winfo_exists())
        except tk.TclError:
            alive = False
        if alive:
            self._status_label.config(
                text=i18n.t("script_diff.reset_done",
                            "Script reset to the shipped default."))
            self._notify_change()

    def _reset_tool(self):
        if not self._ids:
            return
        if not messagebox.askyesno(
            i18n.t("script_diff.reset_title", "Reset scripts"),
            i18n.t(
                "script_diff.reset_confirm",
                "Restore the shipped scripts for this tool? "
                "Your script changes will be removed."),
            parent=self,
        ):
            return
        button_ids = list(self._ids)
        try:
            script_overrides.clear_overrides(button_ids)
        except OSError as exc:
            messagebox.showerror(
                i18n.t("script_diff.reset_title", "Reset scripts"),
                str(exc), parent=self)
            return
        self._restore_live(button_ids)
        self.close()

    def _restore_live(self, button_ids):
        if self.config_data is None:
            return
        script_overrides.restore_in_config(
            self.config_data, button_ids, shipped_config=self.shipped)

    def _notify_change(self):
        if callable(self.on_change):
            try:
                self.on_change()
            except Exception:
                pass

    def _reload(self):
        self.statuses = script_overrides.statuses_for_tool(
            self.tool_key, shipped_config=self.shipped)
        self._ids = list(self.statuses)
        self._list.delete(0, "end")
        for button_id in self._ids:
            _script, status = self.statuses[button_id]
            self._list.insert(
                "end",
                f"{self._script_name(button_id)}  \u2014  "
                f"{self._status_text(status)}")
        if not self._ids:
            self.close()
            return
        self._select(0)

    # ---- rendering ------------------------------------------------------
    def _on_list_select(self, _event=None):
        selection = self._list.curselection()
        if selection:
            self._select(int(selection[0]))

    def _on_view_changed(self, _event=None):
        self._render_selected()

    def _select(self, index: int):
        self._selected = index
        self._list.selection_clear(0, "end")
        self._list.selection_set(index)
        self._list.activate(index)
        _effective, status = self.statuses[self._ids[index]]
        if status == script_overrides.STATUS_REVIEW:
            if not self._banner.winfo_manager():
                self._banner.pack(anchor="w", fill="x",
                                  pady=(0, SPACING["tight"]),
                                  before=self._text_frame)
            self._view_var.set(self._view_stock)
        else:
            self._banner.pack_forget()
            self._view_var.set(self._view_edit)
        self._status_label.config(text="")
        self._render_selected()

    def _render_selected(self):
        if not self._ids:
            return
        button_id = self._ids[self._selected]
        effective, _status = self.statuses[button_id]
        entries = script_overrides.load_overrides()
        entry = entries.get(button_id)
        entry = entry if isinstance(entry, dict) else {}
        stock = str(self.buttons.get(button_id, {}).get("script", ""))
        base = script_overrides.normalize_script(
            entry.get("base_script", ""))
        user = script_overrides.normalize_script(entry.get("script", ""))

        if self._view_var.get() == self._view_stock:
            left, right = stock, effective
            left_name = i18n.t("script_diff.stock_file", "stock")
        else:
            left, right = base if entry else stock, user if entry else effective
            left_name = i18n.t("script_diff.base_file", "base")
        self._render_diff(
            left, right, left_name,
            i18n.t("script_diff.your_script", "your script"))

    def _render_diff(self, left: str, right: str, left_name: str,
                     right_name: str):
        diff = list(difflib.unified_diff(
            left.splitlines(),
            right.splitlines(),
            fromfile=left_name,
            tofile=right_name,
            lineterm="",
        ))
        if not diff:
            diff = [i18n.t("script_diff.no_difference",
                           "(no difference from the shipped script)")]
        old_no = 0
        new_no = 0
        self._text.config(state="normal")
        self._text.delete("1.0", "end")
        for line in diff:
            number = ""
            tag = ""
            if line.startswith("@@"):
                match = re.match(
                    r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                if match:
                    old_no, new_no = int(match.group(1)), int(match.group(2))
                tag = "hunk"
            elif line.startswith(("+++", "---")):
                tag = "meta"
            elif line.startswith("+"):
                number = self._line_numbers("", new_no)
                new_no += 1
                tag = "add"
            elif line.startswith("-"):
                number = self._line_numbers(old_no, "")
                old_no += 1
                tag = "del"
            elif line.startswith(" "):
                number = self._line_numbers(old_no, new_no)
                old_no += 1
                new_no += 1
            self._text.insert("end", number, "meta")
            self._text.insert("end", line + "\n", tag)
        self._text.config(state="disabled")

    @staticmethod
    def _line_numbers(old, new) -> str:
        return f"{old:>4} {new:>4}  "

    def _status_text(self, status: str) -> str:
        if status == script_overrides.STATUS_REVIEW:
            return i18n.t("script_diff.default_changed", "Default changed")
        return i18n.t("script_diff.modified", "Modified")

    def _script_name(self, button_id: str) -> str:
        entry = self.buttons.get(button_id)
        if isinstance(entry, dict):
            return str(entry.get("script_name") or button_id)
        return button_id

    def _stock_title(self) -> str:
        # Only the version file: no Git call on the Tk thread.
        version = ""
        try:
            version = updater.read_version(updater.APP_ROOT)
        except Exception:
            version = ""
        return i18n.t(
            "script_diff.stock", "Stock {version}", version=version)
