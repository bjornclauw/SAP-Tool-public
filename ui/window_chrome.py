"""One convention for window title-bar colour and first-show placement.

Title bars follow one rule (see ``ui_styles``):

    toolbar        primary blue      (SapToolbar)
    debugger       debug red         (DebuggerWindow)
    tool windows   their own accent  (ui_styles.TOOL_COLORS)
    helper modals  ui_styles.SUPPORT_COLOR

Tool windows open centred on the work area of the monitor that holds the
main toolbar -- docked or undocked. Helper modals open centred on the monitor
of the window that launched them (their immediate owner), so a modal launched
from a tool window on a second screen stays on that screen. Both fall back to
the root window when no owner can be resolved.
"""

from __future__ import annotations

import re
import tkinter as tk

from .monitor import work_area_of


def root_window(widget: tk.Misc) -> tk.Misc:
    """Walk the master chain up to the application root (the toolbar)."""
    node: tk.Misc = widget
    parent = getattr(node, "master", None)
    while parent is not None:
        node = parent
        parent = getattr(node, "master", None)
    return node


def apply_titlebar_color(window: tk.Toplevel, color: str) -> None:
    """Colour the Windows title bar with white caption text (no-op elsewhere)."""
    try:
        import pywinstyles

        pywinstyles.change_header_color(window, color=color)
        pywinstyles.change_title_color(window, color="white")
    except Exception:
        pass


def owner_window(window: tk.Misc) -> tk.Misc:
    """The window that launched ``window``: its nearest Tk/Toplevel master.

    Helper modals use this to stay on the monitor of the tool window they were
    opened from. A modal created from an inner frame still resolves to the
    containing window.
    """
    parent = getattr(window, "master", None)
    while parent is not None:
        if isinstance(parent, (tk.Tk, tk.Toplevel)):
            return parent
        parent = getattr(parent, "master", None)
    return root_window(window)


def _reference(window: tk.Misc, reference: tk.Misc | None) -> tk.Misc:
    return reference if reference is not None else owner_window(window)


def center_on_toolbar(window: tk.Toplevel, reference: tk.Misc | None = None) -> None:
    """Position-only centring on the reference window's monitor, while withdrawn.

    ``reference`` defaults to the launching window (the immediate owner), which
    is the toolbar for a tool window and the tool window for a helper modal.
    Skipped for zoomed windows and on any error, so the WM cascade stays the
    fallback.
    """
    try:
        if window.state() == "zoomed":
            return
        ref = _reference(window, reference)
        window.update_idletasks()
        w, h = window.winfo_width(), window.winfo_height()
        if w <= 1 or h <= 1:
            return
        mx, my, mw, mh = work_area_of(ref)
        x = mx + max(0, (mw - w) // 2)
        y = my + max(0, (mh - h) // 2)
        x = max(mx, min(x, mx + mw - w)) if mw > w else mx
        y = max(my, min(y, my + mh - h)) if mh > h else my
        window.geometry(f"+{x}+{y}")
    except Exception:
        pass


def correct_for_decorations(window: tk.Toplevel, reference: tk.Misc | None = None) -> None:
    """Nudge the client-area centre onto the work-area centre.

    geometry() positions the client area while winfo_* measures the outer
    frame, so titlebar/border thickness would otherwise leave the window a
    hair low -- measured live, no theme/DPI tables needed.
    """
    try:
        if window.state() == "zoomed":
            return
        ref = _reference(window, reference)
        window.update_idletasks()
        w, h = window.winfo_width(), window.winfo_height()
        if w <= 1 or h <= 1:
            return
        match = re.match(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$", window.geometry())
        if not match:
            return
        cx, cy = int(match.group(3)), int(match.group(4))
        mx, my, mw, mh = work_area_of(ref)
        dx = (mx + (mw - w) // 2) - window.winfo_rootx()
        dy = (my + (mh - h) // 2) - window.winfo_rooty()
        if dx or dy:
            window.geometry(f"+{cx + dx}+{cy + dy}")
    except Exception:
        pass


def show_centered(window: tk.Toplevel, color: str, reference: tk.Misc | None = None) -> None:
    """Reveal a built window: centre on its owner's monitor and colour it."""
    ref = _reference(window, reference)
    try:
        window.transient(ref)
    except Exception:
        pass
    center_on_toolbar(window, ref)
    window.deiconify()
    window.lift()
    window.focus_force()
    correct_for_decorations(window, ref)
    apply_titlebar_color(window, color)
