"""Custom menu bar framework.

Replaces the native OS menu bar with a fully custom frame using Menubutton
widgets. This gives complete control over styling (background, colors, fonts,
hover effects) across all platforms.

The dropdowns themselves are native ``tk.Menu`` popups. They are only used on
the undocked toolbar; the docked toolbar uses its in-window helper rail
instead, so no native menu ever competes with a topmost borderless owner.

Usage:
    from custom_menu import CustomMenuBar
    menu_bar = CustomMenuBar(root)
    menu_bar.add_menu("Tools", [
        ("Purchase Request Helper", self._open_purchase_request),
    ])
    menu_bar.pack(side="top", fill="x")
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

from . import ui_styles


class MenuBarItem:
    """A single menu item in a dropdown; children turn it into a cascade."""

    def __init__(
        self,
        label: str,
        command: Callable | None = None,
        separator: bool = False,
        variable: tk.Variable | None = None,
        children: list[MenuBarItem] | None = None,
        onvalue=None,
        offvalue=None,
        radiobutton: bool = False,
    ):
        self.label = label
        self.command = command
        self.separator = separator
        self.variable = variable
        self.children = children
        self.onvalue = onvalue
        self.offvalue = offvalue
        self.radiobutton = radiobutton


class CustomMenuBar:
    """A fully custom menu bar built from frames and Menubuttons."""

    def __init__(self, master: tk.Misc, height: int = 30):
        self.master = master
        self.height = height
        self._menus: dict[str, tk.Menu] = {}

        self.frame = tk.Frame(
            master,
            bg=ui_styles.COLORS["bg"],
            height=height,
        )
        self.frame.pack_propagate(False)
        self.frame.pack(side="top", fill="x")

    def get_menu(self, label: str) -> tk.Menu | None:
        """Return the dropdown menu for a top-level label (test hook)."""
        return self._menus.get(label)

    def _fill_menu(self, menu: tk.Menu, items: list[MenuBarItem] | list[tuple[str, Callable]]) -> None:
        """Populate a menu: separators, checkbuttons (variable set),
        cascades (children set), plain commands otherwise."""
        normalized: list[MenuBarItem] = [
            MenuBarItem(label=item[0], command=item[1]) if isinstance(item, tuple) else item
            for item in items
        ]
        for item in normalized:
            if item.separator:
                menu.add_separator()
            elif item.children:
                sub = tk.Menu(
                    menu,
                    tearoff=0,
                    bg=ui_styles.COLORS["surface"],
                    fg=ui_styles.COLORS["text"],
                    activebackground=ui_styles.COLORS["primary"],
                    activeforeground="#ffffff",
                    font=ui_styles.FONTS["body"],
                )
                self._fill_menu(sub, item.children)
                menu.add_cascade(
                    label=item.label, menu=sub, font=ui_styles.FONTS["body"],
                )
            elif item.radiobutton and item.variable is not None:
                menu.add_radiobutton(
                    label=item.label,
                    variable=item.variable,
                    value=item.onvalue,
                    command=item.command if item.command is not None else lambda: None,
                    font=ui_styles.FONTS["body"],
                )
            elif item.variable is not None:
                kwargs = {
                    "label": item.label,
                    "variable": item.variable,
                    "command": item.command if item.command is not None else lambda: None,
                    "font": ui_styles.FONTS["body"],
                }
                if item.onvalue is not None:
                    kwargs["onvalue"] = item.onvalue
                if item.offvalue is not None:
                    kwargs["offvalue"] = item.offvalue
                menu.add_checkbutton(**kwargs)
            else:
                menu.add_command(
                    label=item.label,
                    command=item.command if item.command is not None else lambda: None,
                    font=ui_styles.FONTS["body"],
                    underline=0,
                )

    def add_menu(self, label: str, items: list[MenuBarItem] | list[tuple[str, Callable]]) -> None:
        """Add a dropdown menu to the menu bar."""
        btn = tk.Menubutton(
            self.frame,
            text=label,
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text"],
            activebackground=ui_styles.COLORS["surface"],
            activeforeground=ui_styles.COLORS["text"],
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=ui_styles.FONTS["body"],
            padx=12,
            pady=4,
        )
        btn.pack(side="left", fill="y")

        dropdown = tk.Menu(
            btn,
            tearoff=0,
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text"],
            activebackground=ui_styles.COLORS["primary"],
            activeforeground="#ffffff",
            borderwidth=1,
            relief="flat",
            font=ui_styles.FONTS["body"],
        )
        btn.config(menu=dropdown)
        self._menus[label] = dropdown
        self._fill_menu(dropdown, items)

    def pack(self, **kwargs) -> None:
        self.frame.pack(**kwargs)

    def grid(self, **kwargs) -> None:
        self.frame.grid(**kwargs)
