"""Per-team Print Orders color overrides."""

from __future__ import annotations

import tkinter as tk
from tkinter import colorchooser

from core import i18n
from tools.print_team_colors import team_color
from ui import ui_styles
from ui.ui_styles import (
    SPACING,
    primary_button,
    styled_button,
    styled_frame,
    styled_label,
)
from ui.window_chrome import show_centered


def _key(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _foreground(color: str) -> str:
    value = color.lstrip("#")
    channels = [int(value[index:index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [channel / 12.92 if channel <= .04045
              else ((channel + .055) / 1.055) ** 2.4 for channel in channels]
    luminance = .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2]
    return "#1a1a1a" if luminance > .45 else "#ffffff"


class TeamColorDialog(tk.Toplevel):
    """Edit saved team colors without changing the generated defaults."""

    def __init__(self, parent, teams: list[str], overrides: dict,
                 on_save):
        super().__init__(parent)
        self.teams = list(teams)
        self.overrides = dict(overrides)
        self.on_save = on_save
        self.withdraw()
        self.title(i18n.t("print_orders.team_colors_title", "Team colours"))
        self.configure(bg=ui_styles.COLORS["bg"])
        self.geometry("440x360")
        self.minsize(420, 340)
        self.resizable(False, False)
        self.bind("<Escape>", lambda _event: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._build()
        self._select(0)
        show_centered(self, ui_styles.SUPPORT_COLOR, parent)
        self.grab_set()

    def _build(self):
        outer = styled_frame(self)
        outer.pack(fill="both", expand=True, padx=SPACING["comfortable"],
                   pady=SPACING["comfortable"])

        row = styled_frame(outer)
        row.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(row, exportselection=False)
        self.listbox.insert("end", *self.teams)
        self.listbox.pack(side="left", fill="both", expand=True)
        scroll = tk.Scrollbar(row, orient="vertical", command=self.listbox.yview)
        scroll.pack(side="right", fill="y")
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.bind("<<ListboxSelect>>", lambda _event: self._refresh())

        side = styled_frame(outer)
        side.pack(fill="x", pady=(SPACING["normal"], 0))
        self.preview = styled_label(
            side, "", font=ui_styles.FONTS["header"], anchor="center")
        self.preview.pack(fill="x", ipady=SPACING["loose"])

        buttons = styled_frame(outer)
        buttons.pack(fill="x", pady=(SPACING["normal"], 0))
        styled_button(
            buttons, i18n.t("print_orders.team_color_choose", "Choose colour…"),
            command=self._choose,
        ).pack(side="left")
        styled_button(
            buttons, i18n.t("print_orders.team_color_reset", "Reset to automatic"),
            command=self._reset,
        ).pack(side="left", padx=(SPACING["tight"], 0))
        primary_button(
            buttons, i18n.t("print_orders.team_color_save", "Save"),
            command=self._save,
        ).pack(side="right")

    def _selected_team(self) -> str | None:
        selection = self.listbox.curselection()
        return self.teams[selection[0]] if selection else None

    def _select(self, index: int):
        if self.teams:
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(index)
        self._refresh()

    def _refresh(self):
        team = self._selected_team()
        if not team:
            return
        color = team_color(team, self.overrides)
        self.preview.configure(
            text=f"{team}\n{color}",
            bg=color,
            fg=_foreground(color),
        )

    def _choose(self):
        team = self._selected_team()
        if not team:
            return
        initial = team_color(team, self.overrides)
        result = colorchooser.askcolor(parent=self, initialcolor=initial)
        if not result or not result[1]:
            return
        for name in list(self.overrides):
            if _key(name) == _key(team):
                del self.overrides[name]
        self.overrides[team] = result[1]
        self._refresh()

    def _reset(self):
        team = self._selected_team()
        if not team:
            return
        for name in list(self.overrides):
            if _key(name) == _key(team):
                del self.overrides[name]
        self._refresh()

    def _save(self):
        if callable(self.on_save):
            self.on_save(dict(self.overrides))
        self.destroy()
