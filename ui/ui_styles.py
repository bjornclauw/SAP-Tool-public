"""UI Theme System -- Single source of truth for all styling.

Provides centralized colors, fonts, spacing, and helper functions for creating
consistent styled widgets throughout the application.

Usage:
    from ui_styles import styled_button, primary_button, styled_entry

    styled_button(master, "Click me")  # -> tk.Button with theme styles
    primary_button(master, "Submit")   # -> blue action button
"""

from __future__ import annotations

import tkinter as tk
from typing import Any

# ============================================================================
# THEME: Colors
# ============================================================================
COLORS: dict[str, str] = {
    # Brand / Primary
    "primary": "#5a9bce",
    "primary_hover": "#106ebe",
    "primary_active": "#006cc1",
    # Brand / Secondary (Helpers)
    "secondary": "#e8963f",
    "secondary_hover": "#d07a20",
    "secondary_active": "#b86a15",
    # Brand / Edit
    "edit": "#8e44ad",
    "edit_hover": "#7d3c98",
    "edit_active": "#6c3483",
    # Brand / Debug
    "debug": "#d13438",
    "debug_hover": "#b82828",
    "debug_active": "#a01d1d",
    # Brand / Order Search
    "order_search": "#2e7d32",
    "order_search_hover": "#236b27",
    "order_search_active": "#1b5e20",
    # Surfaces
    "bg": "#f5f5f5",  # Window / frame background
    "surface": "#ffffff",  # Cards, buttons, entries (raised surface)
    "surface_hover": "#f8f8f8",  # Hover state for surfaces
    # Text
    "text": "#1a1a1a",  # Primary text
    "text_secondary": "#666666",  # Secondary text
    "text_disabled": "#999999",  # Disabled text
    "text_on_primary": "#ffffff",  # Text on primary color
    # Borders
    "border": "#d0d0d0",  # Standard border
    "border_light": "#e0e0e0",  # Light border
    "border_dark": "#b0b0b0",  # Dark border for contrast
    # Semantic
    "success": "#107c10",  # Success state
    "success_light": "#e6f5e6",  # Success light background
    "success_flash": "#c6efce",  # Bright attention tint (order-driven updates)
    "error": "#d13438",  # Error state
    "error_light": "#fde7e9",  # Error light background
    "warning": "#ff8c00",  # Warning state
    "warning_light": "#fff3e0",  # Warning light background
    "info": "#0078d4",  # Info state
    "info_light": "#e1f0ff",  # Info light background
    # Debugger
    "debug_breakpoint": "#ffe6e6",
    "debug_active_bg": "#e8f4fd",  # Light blue for active row
    "debug_breakpoint_active": "#ffd6d6",
}

# ============================================================================
# THEME: Fonts
# ============================================================================
FONTS: dict[str, tuple] = {
    "title": ("Segoe UI", 10, "bold"),
    "header": ("Segoe UI", 9, "bold"),
    "body": ("Segoe UI", 9),
    "small": ("Segoe UI", 8),
    "mono": ("Consolas", 9),
}

# ============================================================================
# THEME: Window title-bar conventions
# ============================================================================
# The toolbar is primary (blue) and the debugger is debug (red). Every other
# window follows one rule: a tool window takes its own accent from
# TOOL_COLORS; every helper modal shares SUPPORT_COLOR. Placement is handled
# by ui/window_chrome.py, which centres each window on the toolbar's monitor.
TOOL_COLORS: dict[str, str] = {
    "purchase_request": "#e8963f",  # orange
    "print_orders": "#00796b",  # teal
    "sap_import": "#4c7d5b",  # neutral sage green
    "order_searcher": "#2e7d32",  # green
    "vendor_manager": "#795548",  # brown
}
SUPPORT_COLOR = "#546e7a"  # blue-grey: every helper modal

# ============================================================================
# THEME: Spacing
# ============================================================================
SPACING: dict[str, int] = {
    "tight": 4,
    "normal": 8,
    "comfortable": 12,
    "loose": 16,
}

# ============================================================================
# THEME: Button Styles
# ============================================================================
BUTTON_STYLES: dict[str, dict[str, Any]] = {
    "secondary": {
        "font": FONTS["body"],
        "bg": COLORS["surface"],
        "fg": COLORS["text"],
        "activebackground": COLORS["border_light"],
        "activeforeground": COLORS["text"],
        "bd": 1,
        "borderwidth": 1,
        "highlightthickness": 1,
        "highlightbackground": COLORS["border"],
        "highlightcolor": COLORS["border"],
        "relief": "solid",
        "pady": 4,
        "padx": 8,
    },
    "primary": {
        "font": FONTS["body"],
        "bg": COLORS["primary"],
        "fg": COLORS["text_on_primary"],
        "activebackground": COLORS["primary_hover"],
        "activeforeground": COLORS["text_on_primary"],
        "bd": 0,
        "borderwidth": 0,
        "highlightthickness": 0,
        "highlightbackground": COLORS["primary"],
        "highlightcolor": COLORS["primary"],
        "relief": "flat",
        "pady": 4,
        "padx": 8,
    },
}

# ============================================================================
# THEME: Entry Styles
# ============================================================================
ENTRY_STYLES: dict[str, dict[str, Any]] = {
    "default": {
        "font": FONTS["body"],
        "bg": COLORS["surface"],
        "fg": COLORS["text"],
        "insertbackground": COLORS["text"],
        "selectbackground": COLORS["primary"],
        "selectforeground": COLORS["text_on_primary"],
        "bd": 1,
        "highlightthickness": 1,
        "highlightbackground": COLORS["border"],
        "highlightcolor": COLORS["primary"],
    },
}

# ============================================================================
# THEME: Label Styles
# ============================================================================
LABEL_STYLES: dict[str, dict[str, Any]] = {
    "default": {
        "font": FONTS["body"],
        "bg": COLORS["bg"],
        "fg": COLORS["text"],
    },
    "header": {
        "font": FONTS["header"],
        "bg": COLORS["bg"],
        "fg": COLORS["text"],
    },
    "secondary": {
        "font": FONTS["body"],
        "bg": COLORS["bg"],
        "fg": COLORS["text_secondary"],
    },
}

# ============================================================================
# THEME: Frame Styles
# ============================================================================
FRAME_STYLES: dict[str, dict[str, Any]] = {
    "default": {
        "bg": COLORS["bg"],
    },
    "surface": {
        "bg": COLORS["surface"],
    },
    "card": {
        "bg": COLORS["surface"],
        "relief": "solid",
        "bd": 1,
        "highlightthickness": 1,
        "highlightbackground": COLORS["border_light"],
    },
    "status_bar": {
        "bg": COLORS["surface"],
        "relief": "sunken",
        "bd": 1,
    },
}

# ============================================================================
# THEME: Checkbox Styles
# ============================================================================
CHECKBOX_STYLES: dict[str, dict[str, Any]] = {
    "default": {
        "font": FONTS["body"],
        "bg": COLORS["bg"],
        "fg": COLORS["text"],
        "activebackground": COLORS["bg"],
        "activeforeground": COLORS["text"],
        "selectcolor": COLORS["bg"],
    },
}

# ============================================================================
# THEME: Text Widget Styles
# ============================================================================
TEXT_STYLES: dict[str, dict[str, Any]] = {
    "default": {
        "font": FONTS["body"],
        "bg": COLORS["bg"],
        "fg": COLORS["text"],
        "insertbackground": COLORS["text"],
        "selectbackground": COLORS["primary"],
        "selectforeground": COLORS["text_on_primary"],
        "bd": 1,
        "highlightthickness": 1,
        "highlightbackground": COLORS["border"],
    },
}

# ============================================================================
# PUBLIC API
# ============================================================================
__all__ = [
    # Theme data
    "COLORS",
    "FONTS",
    "SPACING",
    "BUTTON_STYLES",
    "ENTRY_STYLES",
    "LABEL_STYLES",
    "FRAME_STYLES",
    "CHECKBOX_STYLES",
    "TEXT_STYLES",
    "TOOL_COLORS",
    "SUPPORT_COLOR",
    # Helper functions
    "styled_button",
    "primary_button",
    "styled_entry",
    "styled_label",
    "styled_frame",
    "card_frame",
    "styled_checkbox",
    "styled_text",
    "styled_status_bar",
]


# --- Helpers ---------------------------------------------------------------


def _apply_defaults(defaults: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Merge user kwargs over defaults, returning a new dict."""
    merged = defaults.copy()
    merged.update(kwargs)
    return merged


# --- Button Helpers --------------------------------------------------------


def styled_button(master: tk.Misc, text: str, **kwargs: Any) -> tk.Button:
    """Create a styled secondary button with theme styling."""
    defaults = BUTTON_STYLES["secondary"].copy()
    defaults.update(kwargs)
    return tk.Button(master, text=text, **defaults)


def primary_button(master: tk.Misc, text: str, **kwargs: Any) -> tk.Button:
    """Create a primary action button with theme styling."""
    defaults = BUTTON_STYLES["primary"].copy()
    defaults.update(kwargs)
    return tk.Button(master, text=text, **defaults)


# --- Entry Helpers ---------------------------------------------------------


def styled_entry(master: tk.Misc, **kwargs: Any) -> tk.Entry:
    """Create a styled entry field with theme styling."""
    defaults = ENTRY_STYLES["default"].copy()
    defaults.update(kwargs)
    return tk.Entry(master, **defaults)


# --- Label Helpers ---------------------------------------------------------


def styled_label(master: tk.Misc, text: str, **kwargs: Any) -> tk.Label:
    """Create a styled label with theme styling."""
    # Auto-select header style if font is header
    if "font" in kwargs and kwargs["font"] == FONTS["header"]:
        defaults = LABEL_STYLES["header"].copy()
    elif "fg" in kwargs and kwargs["fg"] == COLORS["text_secondary"]:
        defaults = LABEL_STYLES["secondary"].copy()
    else:
        defaults = LABEL_STYLES["default"].copy()

    defaults.update(kwargs)
    return tk.Label(master, text=text, **defaults)


# --- Frame Helpers ---------------------------------------------------------


def styled_frame(master: tk.Misc, **kwargs: Any) -> tk.Frame:
    """Create a styled frame with theme styling."""
    defaults = FRAME_STYLES["default"].copy()
    defaults.update(kwargs)
    return tk.Frame(master, **defaults)


def card_frame(master: tk.Misc, **kwargs: Any) -> tk.Frame:
    """Raised white card for grouping a row/section (toolbar strips etc.)."""
    defaults = FRAME_STYLES["card"].copy()
    defaults.update(kwargs)
    return tk.Frame(master, **defaults)


# --- Checkbox Helpers ------------------------------------------------------


def styled_checkbox(master: tk.Misc, text: str, **kwargs: Any) -> tk.Checkbutton:
    """Create a styled checkbox with theme styling."""
    defaults = CHECKBOX_STYLES["default"].copy()
    defaults.update(kwargs)
    return tk.Checkbutton(master, text=text, **defaults)


# --- Text Widget Helpers ---------------------------------------------------


def styled_text(master: tk.Misc, **kwargs: Any) -> tk.Text:
    """Create a styled text widget with theme styling."""
    defaults = TEXT_STYLES["default"].copy()
    defaults.update(kwargs)
    return tk.Text(master, **defaults)


# --- Status Bar Helpers ----------------------------------------------------


def styled_status_bar(master: tk.Misc, **kwargs: Any) -> tk.Frame:
    """Create a styled status bar frame with theme styling."""
    defaults = FRAME_STYLES["status_bar"].copy()
    defaults.update(kwargs)
    return tk.Frame(master, **defaults)
