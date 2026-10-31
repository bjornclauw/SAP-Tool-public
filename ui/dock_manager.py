"""DockManager -- dock the toolbar to the top screen edge with auto-hide.

Full package without a system-tray icon (per request):

- "Dock top": snaps the toolbar to the top edge, centered, kept topmost.
- "Auto-hide" (needs dock): after the mouse leaves the window for a short
  dwell, the toolbar withdraws and a slim always-visible **peek strip**
  (primary-blue, full width, 14px, "SAP Toolbar -- hover or Ctrl+Alt+S")
  stays on the edge as the visual "I'm still here". Hover/click the strip
  to restore. This mirrors precedents: Windows taskbar auto-hide leaves a
  trigger strip, Din Quick Launch Toolbar shows a thin edge strip, and the
  peek-on-edge state machine (tucked / revealed, armed re-entry).
- Reliable re-show without tray: the peek strip (pointer) PLUS a global
  hotkey Ctrl+Alt+S via RegisterHotKey in a daemon thread (NULL hWnd +
  GetMessage loop, marshalled back with root.after -- Tkinter's mainloop
  can't see WM_HOTKEY itself). Real AppBar (SHAppBarMessage) was
  deliberately NOT used: YASB's issue #444 shows auto-hide + AppBar keeps
  the reserved space when hidden, and a true autohide AppBar allows only
  one per edge system-wide -- the fake dock avoids both traps.

Settings persist in data/user_settings.json: dock_enabled / dock_autohide.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path

from .monitor import work_area_of

HOTKEY_ID = 0x5A17
HOTKEY_MOD = 0x0001 | 0x0002  # MOD_ALT | MOD_CONTROL
HOTKEY_VK = 0x53  # 'S'
WM_HOTKEY = 0x0312
HIDE_DWELL_MS = 800
HOTKEY_POLL_MS = 100
PEEK_HEIGHT = 14
# Slide+fade show/hide: ~220ms at ~60fps. Professional, not playful --
# ease-in (accelerating) on hide, ease-out (settling) on show.
DOCK_ANIM_MS = 220
DOCK_ANIM_STEPS = 12
PEEK_FADE_MS = 150
PEEK_FADE_STEPS = 7
# Windows drops -topmost across withdraw/deiconify and when a helper takes
# focus; re-assert it periodically while docked.
TOPMOST_WATCH_MS = 2000
# A modal grab routes pointer events away from the pill, so poll the pointer
# while the pill is visible instead of relying on its <Enter> binding.
PEEK_HOVER_MS = 120
LOGO_PATH = Path(__file__).parent.parent / "assets" / "logo.png"


def load_logo_image(size: int, master=None):
    """BC logo resized to a square PhotoImage, or None if unavailable.

    Shared by the toolbar logo and the peek pill; callers must hold a
    reference to the returned image while it is displayed. ``master``
    pins the image to the right Tk interpreter -- without it the image
    lands on whatever root was created first, and re-creating windows
    (as the test-suite does) ends with "image pyimageN doesn't exist".
    """
    try:
        from PIL import Image, ImageTk  # type: ignore

        im = Image.open(LOGO_PATH).convert("RGBA")
        im.thumbnail((size, size), Image.Resampling.LANCZOS)
        if master is not None:
            return ImageTk.PhotoImage(im, master=master)
        return ImageTk.PhotoImage(im)
    except Exception:
        return None


class DockManager:
    def __init__(self, root: tk.Tk, settings: dict, save_callback=None,
                 on_decorated=None):
        self.root = root
        self._save_callback = save_callback
        # Called after decorations come back (undock): Windows drops the
        # pywinstyles titlebar color on every overrideredirect toggle, so
        # the owner must re-apply it. SapToolbar passes _set_titlebar_color.
        self.on_decorated = on_decorated
        self.dock_enabled = tk.BooleanVar(value=bool(settings.get("dock_enabled", False)))
        self.autohide = tk.BooleanVar(value=bool(settings.get("dock_autohide", False)))
        self.hotkey_available = _hotkey_supported()
        self._peek: tk.Toplevel | None = None
        self._peek_inner: tk.Frame | None = None
        self._peek_logo_img = None
        self._peek_status = ""
        self._peek_status_fg = "#ffffff"
        self._peek_status_label: tk.Label | None = None
        self._peek_sap_text = ""
        self._peek_sap_fg = "#e6f5e6"
        self._peek_sap_label: tk.Label | None = None
        self._hide_after: str | None = None
        self._hotkey_thread: threading.Thread | None = None
        self._hotkey_registered = False
        self._hotkey_error: str | None = None  # last "fail:..." from the loop
        self._hotkey_queue: queue.Queue[str] = queue.Queue()
        self._hotkey_poll_id: str | None = None
        self._destroyed = False
        self._toast: tk.Toplevel | None = None
        self._toast_after: str | None = None
        self._toast_msg = ""
        self._was_docked = False
        self._saved_geometry: str = ""
        # Slide+fade animation state. _anim_id is the pending frame (None
        # when idle); _anim_gen invalidates stale frames so a reversed
        # animation can never fight the one that replaced it.
        self._anim_id: str | None = None
        self._anim_gen = 0
        self._topmost_id: str | None = None
        self._peek_hover_id: str | None = None
        self._grab_watch_id: str | None = None

        root.bind("<Enter>", self._on_enter, "+")
        root.bind("<Leave>", self._on_leave, "+")

    # ---- public ------------------------------------------------------
    def apply(self):
        """(Re)apply dock + autohide state. Call after geometry is known."""
        now_docked = bool(self.dock_enabled.get())
        if now_docked:
            self._set_chrome(True)
            self._snap_top()
            self._ensure_hotkey()  # hotkey works whenever docked
            self._start_topmost_watch()
            if not self.autohide.get():
                self._destroy_peek()
                # Dock transitions stay crisp/instant; the slide+fade is
                # reserved for autohide hide/show and the hotkey toggle.
                self._show_main(animate=False)
        else:
            self._stop_topmost_watch()
            self._destroy_peek()
            self._show_main(animate=False)
            # Re-decorating recreates the native window, and Windows can
            # restore it to the pre-dock monitor. Pin the bar where it was.
            try:
                undock_pos = (self.root.winfo_rootx(), self.root.winfo_rooty())
            except Exception:
                undock_pos = None
            self._set_chrome(False)
            # _snap_top / animation frames may have locked an explicit
            # WxH; release it so the bar shrink-wraps from here on.
            if undock_pos is not None:
                self._release_size(*undock_pos)
            else:
                self._release_size()
            try:
                flag = getattr(self.root, "always_on_top", None)
                keep_top = bool(flag is not None and flag.get())
                self.root.attributes("-topmost", keep_top)
            except Exception:
                pass
        if now_docked and not self._was_docked:
            self._show_dock_toast()
        self._was_docked = now_docked
        self._save_meta()

    def set_dock_mode(self, on: bool):
        """Logo double-click path: entering dock mode turns auto-hide on."""
        self.dock_enabled.set(on)
        if on:
            self.autohide.set(True)
        self.apply()

    def _set_chrome(self, borderless: bool):
        """Docked = no title bar (overrideredirect); undocked = decorated."""
        try:
            current = bool(self.root.overrideredirect())
        except Exception:
            return
        if current == borderless:
            return
        was_hidden = self._is_hidden()
        try:
            self._destroy_peek()
            self.root.withdraw()
            self.root.overrideredirect(borderless)
            self.root.deiconify()
            self.root.update_idletasks()
            self._assert_topmost()
            if was_hidden:
                self._hide_to_peek(animate=False)
            if not borderless and callable(self.on_decorated):
                self.on_decorated()
        except Exception:
            pass

    def toggle_from_hotkey(self, animate: bool = True):
        if not (self.dock_enabled.get() and self.autohide.get()):
            try:
                self.root.lift()
                self.root.focus_force()
            except Exception:
                pass
            return
        if self._is_hidden():
            self._show_main(animate=animate)
        else:
            self._hide_to_peek(animate=animate)

    def destroy(self):
        self._destroyed = True
        self._cancel_anim()
        self._stop_topmost_watch()
        self._stop_grab_watch()
        try:
            if self._hide_after:
                self.root.after_cancel(self._hide_after)
        except Exception:
            pass
        try:
            if self._hotkey_poll_id:
                self.root.after_cancel(self._hotkey_poll_id)
        except Exception:
            pass
        self._hotkey_poll_id = None
        self._destroy_toast()
        # Release the system-wide key so a later instance (or relaunch)
        # can register it again; the orphaned daemon thread simply stops
        # receiving and dies with the process.
        try:
            import ctypes

            ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
        except Exception:
            pass
        self._hotkey_registered = False
        self._destroy_peek()
        self._peek_logo_img = None

    # ---- dock --------------------------------------------------------
    def _assert_topmost(self):
        """Re-apply topmost to the docked bar and the pill.

        Windows drops the attribute across withdraw/deiconify cycles and when
        another window focuses, so every transition (and a slow watch) calls
        this instead of relying on the one-off set at creation.
        """
        if not self.dock_enabled.get():
            return
        peek = self._peek
        # Windows can snap an overrideredirect Toplevel back to +0+0 when
        # -topmost is re-applied while the docked root is withdrawn; remember
        # the pill's geometry and put it back.
        geometry = None
        if peek is not None:
            try:
                if peek.winfo_exists():
                    geometry = peek.geometry()
            except Exception:
                geometry = None
        try:
            self.root.attributes("-topmost", True)
        except Exception:
            pass
        if peek is not None:
            try:
                if peek.winfo_exists():
                    peek.attributes("-topmost", True)
                    if geometry:
                        peek.geometry(geometry)
            except Exception:
                pass

    def _start_topmost_watch(self):
        if self._topmost_id is None:
            try:
                self._topmost_id = self.root.after(
                    TOPMOST_WATCH_MS, self._topmost_tick)
            except Exception:
                self._topmost_id = None

    def _topmost_tick(self):
        self._topmost_id = None
        if self._destroyed or not self.dock_enabled.get():
            return
        self._assert_topmost()
        # A helper that closed may have skipped the last hide; re-arm it so
        # the bar returns to its normal auto-hide behaviour.
        if (self.autohide.get() and not self._is_hidden()
                and self._hide_after is None):
            self._on_leave()
        if (self.autohide.get() and not self._is_hidden()
                and self._grab_active()):
            self._start_grab_watch()
        self._start_topmost_watch()

    def _stop_topmost_watch(self):
        if self._topmost_id:
            try:
                self.root.after_cancel(self._topmost_id)
            except Exception:
                pass
            self._topmost_id = None
        self._stop_grab_watch()

    def _snap_top(self):
        """Center on the top edge OF THE TOOLBAR'S OWN MONITOR (not the
        primary -- winfo_screenwidth lies on multi-monitor setups).
        Position-only: the bar keeps whatever size pack computes, so a
        tray unfolding later can grow it downward instead of squeezing
        its content to unmapped."""
        try:
            self.root.update_idletasks()
            w = self.root.winfo_width() or self.root.winfo_reqwidth() or 800
            mx, my, mw, _mh = work_area_of(self.root)
            x = mx + max(0, (mw - w) // 2)
            self.root.geometry(f"+{x}+{my}")
            self.root.update_idletasks()  # let the WM commit before we read back
            self.root.attributes("-topmost", True)
        except Exception:
            pass

    def _release_size(self, x: int | None = None, y: int | None = None):
        """Drop any explicit WxH (snap, animation frames) so pack
        shrink-wraps the bar again; optionally pin position after.
        Without this the toplevel keeps a stale fixed size and newly
        packed content (the tray) gets squeezed to unmapped."""
        try:
            self.root.geometry("")
            self.root.update_idletasks()
            if x is not None and y is not None:
                self.root.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _saved_xy(self):
        target = self._parse_geometry(self._saved_geometry or "")
        if target is not None:
            return target[2], target[3]
        return None

    # ---- dock toast --------------------------------------------------
    def _show_dock_toast(self, duration_ms: int = 3500):
        """Transient 'how to get me back' note under the bar -- replaces the
        old permanent hint label that ate a third of the utility row."""
        self._destroy_toast()
        self._toast_msg = "Docked — dbl-click logo to undock • Ctrl+Alt+S to show"
        try:
            toast = tk.Toplevel(self.root)
            toast.overrideredirect(True)
            toast.configure(bg="#1a1a1a")
            tk.Label(
                toast, text=self._toast_msg, bg="#1a1a1a", fg="#ffffff",
                font=(self._body_font(), 8), padx=12, pady=6,
            ).pack()
            toast.update_idletasks()
            w = toast.winfo_reqwidth()
            h = toast.winfo_reqheight()
            # Docked to top, so anchor under the bar on its own monitor; fall
            # back to a fixed offset when the bar is hidden (height 1).
            mx, my, mw, _mh = work_area_of(self.root)
            rx = mx + max(0, (mw - w) // 2)
            root_h = self.root.winfo_height()
            ry = (my + root_h + 8) if root_h > 1 else (my + 40)
            toast.geometry(f"{w}x{h}+{rx}+{ry}")
            toast.deiconify()
            toast.attributes("-topmost", True)
            toast.lift()
            self._toast = toast
            self._toast_after = self.root.after(duration_ms, self._destroy_toast)
            # Windows Tk can drop -topmost set before the window is mapped;
            # re-assert it once the toast is actually on screen.
            self.root.after(30, self._raise_toast)
        except Exception:
            self._toast = None

    def _raise_toast(self):
        if self._toast is None:
            return
        try:
            self._toast.attributes("-topmost", True)
            self._toast.lift()
        except Exception:
            pass

    def _destroy_toast(self):
        if self._toast_after:
            try:
                self.root.after_cancel(self._toast_after)
            except Exception:
                pass
            self._toast_after = None
        if self._toast is not None:
            try:
                self._toast.destroy()
            except Exception:
                pass
            self._toast = None

    @staticmethod
    def _body_font() -> str:
        try:
            from . import ui_styles

            return ui_styles.FONTS["body"][0]
        except Exception:
            return "Segoe UI"

    # ---- hide / show -------------------------------------------------
    # Slide+fade (~220ms): hide accelerates up off the top edge while
    # fading out; show settles back down while fading in. Every path takes
    # animate=False for an instant transition (tests, chrome changes).
    def _is_hidden(self) -> bool:
        return self._peek is not None and self._peek.winfo_exists()

    @staticmethod
    def _ease_in(t: float) -> float:
        return t * t * t

    @staticmethod
    def _ease_out(t: float) -> float:
        return 1.0 - (1.0 - t) ** 3

    @staticmethod
    def _parse_geometry(g: str):
        """'WxH+X+Y' -> (w, h, x, y), or None when unparseable."""
        import re

        try:
            m = re.match(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$", g or "")
            if not m:
                return None
            return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        except Exception:
            return None

    def _cancel_anim(self):
        """Stop any running slide+fade; stale frames no-op via generation."""
        self._anim_gen += 1
        if self._anim_id is not None:
            try:
                self.root.after_cancel(self._anim_id)
            except Exception:
                pass
            self._anim_id = None

    def _hide_to_peek(self, animate: bool = True):
        if self._is_hidden():
            return
        self._cancel_anim()
        if not animate:
            try:
                self._saved_geometry = self.root.geometry()
                self.root.withdraw()
            except Exception:
                return
            self._show_peek(animate=False)
            return
        try:
            self.root.update_idletasks()
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            x = self.root.winfo_rootx()
            y0 = self.root.winfo_rooty()
            if w <= 1 or h <= 1:
                raise ValueError("no geometry yet")
            self._saved_geometry = self.root.geometry()
        except Exception:
            try:
                self._saved_geometry = self.root.geometry()
                self.root.withdraw()
            except Exception:
                return
            self._show_peek(animate=False)
            return
        travel = y0 + h  # slide until fully above the top edge
        gen = self._anim_gen
        delay = max(1, DOCK_ANIM_MS // DOCK_ANIM_STEPS)

        def frame(i: int):
            if gen != self._anim_gen or self._destroyed:
                return
            self._anim_id = None
            t = min(1.0, i / DOCK_ANIM_STEPS)
            try:
                y = y0 - int(self._ease_in(t) * travel)
                self.root.geometry(f"{w}x{h}+{x}+{y}")
                self.root.attributes("-alpha", max(0.0, 1.0 - t))
            except Exception:
                t = 1.0
            if t < 1.0:
                try:
                    self._anim_id = self.root.after(delay, lambda: frame(i + 1))
                except Exception:
                    self._anim_id = None
                return
            try:
                self.root.withdraw()
                self.root.attributes("-alpha", 1.0)
            except Exception:
                pass
            self._show_peek()

        try:
            self._anim_id = self.root.after(delay, lambda: frame(1))
        except Exception:
            self._anim_id = None

    def _show_main(self, animate: bool = True, focus: bool = True):
        self._cancel_anim()
        self._destroy_peek()
        if not animate:
            try:
                self.root.deiconify()
                self.root.attributes("-alpha", 1.0)
                pos = self._saved_xy()
                if self.dock_enabled.get():
                    self._release_size()
                    self._snap_top()
                elif pos is not None:
                    self._release_size(*pos)
                else:
                    self._release_size()
                self.root.lift()
                if focus:
                    self.root.focus_force()
                self._assert_topmost()
            except Exception:
                pass
            return
        target = self._parse_geometry(self._saved_geometry or "")
        try:
            self.root.update_idletasks()
            w = self.root.winfo_width() or self.root.winfo_reqwidth()
            h = self.root.winfo_height() or self.root.winfo_reqheight()
            mx, my, mw, _mh = work_area_of(self.root)
        except Exception:
            w, h, mx, my, mw = 800, 200, 0, 0, 1920
        if target is None:
            x, y0 = mx + max(0, (mw - w) // 2), my
        else:
            _tw, _th, x, y0 = target
        gen = self._anim_gen
        delay = max(1, DOCK_ANIM_MS // DOCK_ANIM_STEPS)
        try:
            self.root.deiconify()
            try:
                already_mapped = bool(self.root.winfo_ismapped())
            except Exception:
                already_mapped = False
            if already_mapped:
                # Re-entry mid-hide: reverse from the current slide
                # position/opacity instead of jumping.
                sx, sy = self.root.winfo_rootx(), self.root.winfo_rooty()
                try:
                    sa = float(self.root.attributes("-alpha"))
                except Exception:
                    sa = 1.0
                sa = min(1.0, max(0.0, sa))
            else:
                sx, sy, sa = x, my - h, 0.0
                self.root.geometry(f"{w}x{h}+{sx}+{sy}")
                self.root.attributes("-alpha", 0.0)
            self.root.lift()
        except Exception:
            return

        def frame(i: int):
            if gen != self._anim_gen or self._destroyed:
                return
            self._anim_id = None
            t = min(1.0, i / DOCK_ANIM_STEPS)
            try:
                e = self._ease_out(t)
                y = sy + int(e * (y0 - sy))
                self.root.geometry(f"{w}x{h}+{x}+{y}")
                self.root.attributes("-alpha", min(1.0, sa + t * (1.0 - sa)))
            except Exception:
                t = 1.0
            if t < 1.0:
                try:
                    self._anim_id = self.root.after(delay, lambda: frame(i + 1))
                except Exception:
                    self._anim_id = None
                return
            try:
                # End released (never re-lock the saved WxH): the bar keeps
                # pack size so later tray changes can grow it.
                self._release_size()
                if self.dock_enabled.get():
                    self._snap_top()
                else:
                    pos = self._saved_xy()
                    if pos is not None:
                        self.root.geometry(f"+{pos[0]}+{pos[1]}")
                self.root.attributes("-alpha", 1.0)
                self.root.lift()
                if focus:
                    self.root.focus_force()
                self._assert_topmost()
            except Exception:
                pass

        try:
            self._anim_id = self.root.after(delay, lambda: frame(1))
        except Exception:
            self._anim_id = None

    def _grab_active(self) -> bool:
        """Whether a modal helper currently holds a grab.

        The bar still auto-hides; this only drives a faster pointer watch,
        because a grab can swallow the root's <Leave> event.
        """
        try:
            return self.root.grab_current() is not None
        except Exception:
            return False

    def _owned_dialog_open(self) -> bool:
        """True while a visible Toplevel is transient to the toolbar itself.

        Windows hides such dialogs when their owner withdraws, so the dock
        must stay visible while one is up. Dialogs transient to a tool window
        (e.g. PRH's order lookup) do not hold the bar.
        """
        try:
            for child in self.root.winfo_children():
                if not isinstance(child, tk.Toplevel):
                    continue
                try:
                    if not child.winfo_ismapped():
                        continue
                    if str(child.transient()) == str(self.root):
                        return True
                except tk.TclError:
                    continue
        except tk.TclError:
            pass
        return False

    def _on_leave(self, _event=None):
        if not (self.dock_enabled.get() and self.autohide.get()):
            return
        if self._is_hidden():
            return
        if self._owned_dialog_open():
            return
        self._cancel_pending_hide()
        try:
            self._hide_after = self.root.after(HIDE_DWELL_MS, self._maybe_hide)
        except Exception:
            pass

    def _on_enter(self, _event=None):
        # Pointer back mid-hide: reverse the slide smoothly from wherever
        # the bar is instead of snapping or letting it withdraw under the
        # cursor. No focus steal -- the pointer is already here.
        if self._anim_id is not None:
            self._show_main(focus=False)
        self._cancel_pending_hide()

    def _cancel_pending_hide(self):
        if self._hide_after:
            try:
                self.root.after_cancel(self._hide_after)
            except Exception:
                pass
            self._hide_after = None

    def _start_grab_watch(self):
        if self._grab_watch_id is None and not self._destroyed:
            try:
                self._grab_watch_id = self.root.after(
                    PEEK_HOVER_MS, self._grab_watch_tick)
            except Exception:
                self._grab_watch_id = None

    def _stop_grab_watch(self):
        if self._grab_watch_id:
            try:
                self.root.after_cancel(self._grab_watch_id)
            except Exception:
                pass
            self._grab_watch_id = None

    def _grab_watch_tick(self):
        """A modal grab can swallow <Leave>; poll the pointer so autohide
        still follows the mouse while a helper is open."""
        self._grab_watch_id = None
        if self._destroyed or not (
                self.dock_enabled.get() and self.autohide.get()):
            return
        if self._is_hidden() or not self._grab_active():
            return
        try:
            px, py = self.root.winfo_pointerx(), self.root.winfo_pointery()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            inside = (
                rx - 2 <= px <= rx + rw + 2
                and ry - 2 <= py <= ry + rh + 2
            )
        except Exception:
            inside = True
        if inside:
            self._cancel_pending_hide()
        elif self._hide_after is None:
            self._on_leave()
        try:
            self._grab_watch_id = self.root.after(
                PEEK_HOVER_MS, self._grab_watch_tick)
        except Exception:
            self._grab_watch_id = None

    def _maybe_hide(self):
        self._hide_after = None
        if not (self.dock_enabled.get() and self.autohide.get()):
            return
        if self._is_hidden():
            return
        if self._owned_dialog_open():
            return
        try:
            px, py = self.root.winfo_pointerx(), self.root.winfo_pointery()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            if rx - 2 <= px <= rx + rw + 2 and ry - 2 <= py <= ry + rh + 2:
                return  # pointer came back -- stay visible (armed re-entry)
        except Exception:
            pass
        self._hide_to_peek()

    # ---- peek pill ---------------------------------------------------
    def set_peek_status(self, text: str, fg: str | None = None):
        """Text on the collapsed docked pill (e.g. auto upload running).

        Stored while the pill is hidden too, so the next show includes it. While
        the pill is up the label is packed/unpacked and the pill resized live,
        so a status that appears after the pill was shown still lands.
        """
        self._peek_status = text or ""
        if fg:
            self._peek_status_fg = fg
        label = self._peek_status_label
        if label is None:
            return
        try:
            if not label.winfo_exists():
                self._peek_status_label = None
                return
            label.config(text=self._peek_status, fg=self._peek_status_fg)
            if self._peek_status and label.winfo_manager() == "":
                label.pack(side="left", padx=(12, 0))
            elif not self._peek_status and label.winfo_manager() != "":
                label.pack_forget()
            self._resize_peek()
        except Exception:
            self._peek_status_label = None

    def set_peek_sap_state(self, text: str, fg: str | None = None):
        """The SAP connection/activity marker on the collapsed pill.

        Sits alongside the auto-upload status; stored while hidden so the next
        show already carries it.
        """
        self._peek_sap_text = text or ""
        if fg:
            self._peek_sap_fg = fg
        label = self._peek_sap_label
        if label is None:
            return
        try:
            if not label.winfo_exists():
                self._peek_sap_label = None
                return
            label.config(text=self._peek_sap_text, fg=self._peek_sap_fg)
            if self._peek_sap_text and label.winfo_manager() == "":
                label.pack(side="left", padx=(12, 0))
            elif not self._peek_sap_text and label.winfo_manager() != "":
                label.pack_forget()
            self._resize_peek()
        except Exception:
            self._peek_sap_label = None

    def _resize_peek(self):
        """Re-center and re-fit the pill after its content changed."""
        if self._peek is None or self._peek_inner is None:
            return
        try:
            self._peek.update_idletasks()
            mx, my, mw, _mh = work_area_of(self.root)
            w = self._peek_inner.winfo_reqwidth() + 20
            h = max(self._peek_inner.winfo_reqheight() + 4, PEEK_HEIGHT)
            self._peek.geometry(f"{w}x{h}+{mx + max(0, (mw - w) // 2)}+{my}")
        except Exception:
            pass

    def _show_peek(self, animate: bool = True):
        """Text-sized pill centered on the top edge (not full width).

        Fades in over ~150ms on the animated path so it hands off smoothly
        from the bar sliding out.
        """
        from . import ui_styles

        self._destroy_peek()
        try:
            mx, my, mw, _mh = work_area_of(self.root)
            peek = tk.Toplevel(self.root)
            peek.overrideredirect(True)
            peek.attributes("-topmost", True)
            peek.configure(bg=ui_styles.COLORS["primary"])
            inner = tk.Frame(peek, bg=ui_styles.COLORS["primary"])
            inner.pack(padx=10, pady=2)
            self._peek_inner = inner
            logo_img = load_logo_image(14, master=inner)
            if logo_img is not None:
                self._peek_logo_img = logo_img  # hold a ref while shown
                tk.Label(
                    inner, image=logo_img, bg=ui_styles.COLORS["primary"],
                    cursor="hand2",
                ).pack(side="left", padx=(0, 6))
            tk.Label(
                inner,
                text="SAP Toolbar — Ctrl+Alt+S",
                bg=ui_styles.COLORS["primary"],
                fg="#ffffff",
                font=(ui_styles.FONTS["body"][0], 8, "bold"),
                cursor="hand2",
            ).pack(side="left")
            # SAP state/activity marker, alongside the auto-upload status.
            self._peek_sap_label = tk.Label(
                inner,
                text=self._peek_sap_text,
                bg=ui_styles.COLORS["primary"],
                fg=self._peek_sap_fg,
                font=(ui_styles.FONTS["body"][0], 8, "bold"),
                cursor="hand2",
            )
            if self._peek_sap_text:
                self._peek_sap_label.pack(side="left", padx=(12, 0))
            # The status label always exists (so a status arriving after the
            # pill is shown can land); it is packed only while non-empty.
            self._peek_status_label = tk.Label(
                inner,
                text=self._peek_status,
                bg=ui_styles.COLORS["primary"],
                fg=self._peek_status_fg,
                font=(ui_styles.FONTS["body"][0], 8, "bold"),
                cursor="hand2",
            )
            if self._peek_status:
                self._peek_status_label.pack(side="left", padx=(12, 0))
            peek.update_idletasks()
            w = inner.winfo_reqwidth() + 20
            h = max(inner.winfo_reqheight() + 4, PEEK_HEIGHT)
            peek.geometry(f"{w}x{h}+{mx + max(0, (mw - w) // 2)}+{my}")
            # Bound on the pill: child Enter/click events bubble to it.
            peek.bind("<Enter>", lambda _e: self._show_main())
            peek.bind("<Button-1>", lambda _e: self._show_main())
            self._peek = peek
            self._assert_topmost()
            self._start_peek_hover_watch()
            if animate:
                delay = max(1, PEEK_FADE_MS // PEEK_FADE_STEPS)
                try:
                    peek.attributes("-alpha", 0.0)
                except Exception:
                    return

                def fade(i: int):
                    try:
                        if self._peek is not peek or not peek.winfo_exists():
                            return
                        peek.attributes("-alpha", min(1.0, i / PEEK_FADE_STEPS))
                    except Exception:
                        return
                    if i < PEEK_FADE_STEPS and not self._destroyed:
                        try:
                            self.root.after(delay, lambda: fade(i + 1))
                        except Exception:
                            pass

                try:
                    self.root.after(delay, lambda: fade(1))
                except Exception:
                    pass
        except Exception:
            self._peek = None
            try:
                self.root.deiconify()
            except Exception:
                pass

    def _start_peek_hover_watch(self):
        self._stop_peek_hover_watch()
        try:
            self._peek_hover_id = self.root.after(
                PEEK_HOVER_MS, self._peek_hover_tick)
        except Exception:
            self._peek_hover_id = None

    def _stop_peek_hover_watch(self):
        if self._peek_hover_id:
            try:
                self.root.after_cancel(self._peek_hover_id)
            except Exception:
                pass
            self._peek_hover_id = None

    def _peek_hover_tick(self):
        """Restore the bar when the pointer reaches the pill.

        A modal helper's grab routes pointer events to the helper, so the
        pill's <Enter> binding never fires while one is open. Polling the
        pointer keeps hover-to-restore working in that case.
        """
        self._peek_hover_id = None
        peek = self._peek
        if peek is None or self._destroyed:
            return
        try:
            if not peek.winfo_exists():
                return
            px, py = self.root.winfo_pointerx(), self.root.winfo_pointery()
            x, y = peek.winfo_rootx(), peek.winfo_rooty()
            w, h = peek.winfo_width(), peek.winfo_height()
            if x <= px <= x + w and y <= py <= y + h:
                self._show_main()
                self._start_grab_watch()
                return
        except Exception:
            return
        self._start_peek_hover_watch()

    def _destroy_peek(self):
        self._stop_peek_hover_watch()
        if self._peek is not None:
            try:
                self._peek.destroy()
            except Exception:
                pass
            self._peek = None
        self._peek_inner = None
        self._peek_status_label = None
        self._peek_sap_label = None

    # ---- global hotkey (daemon thread, no tray needed) ---------------
    # The thread only waits on WM_HOTKEY and drops "toggle" into a queue;
    # a main-thread after() poll drains it. Tk widgets must never be touched
    # off the main thread (the old cross-thread after() is why the key
    # silently did nothing). Registration happens once, on first dock, and
    # stays until exit -- re-registering per toggle races threads.
    def _ensure_hotkey(self):
        if not self.hotkey_available:
            self._report("Ctrl+Alt+S unavailable on this system", "warning")
            return
        if self._hotkey_thread is not None and self._hotkey_thread.is_alive():
            self._hotkey_registered = True
        elif not self._hotkey_registered:
            t = threading.Thread(target=self._hotkey_loop, daemon=True)
            t.start()
            self._hotkey_thread = t
            self._hotkey_registered = True  # confirmed/denied via queue
        if self._hotkey_poll_id is None and not self._destroyed:
            self._hotkey_poll_id = self.root.after(HOTKEY_POLL_MS, self._poll_hotkey)

    def _stop_hotkey(self):
        """Kept for API compatibility; registration now lives till exit."""

    def _poll_hotkey(self):
        self._hotkey_poll_id = None
        try:
            while True:
                msg = self._hotkey_queue.get_nowait()
                if msg == "toggle":
                    self.toggle_from_hotkey()
                elif msg.startswith("fail:"):
                    self._hotkey_registered = False
                    self._hotkey_error = msg
                    self._report(
                        f"Ctrl+Alt+S not registered ({msg[5:]})", "warning"
                    )
        except queue.Empty:
            pass
        finally:
            if not self._destroyed:
                try:
                    self._hotkey_poll_id = self.root.after(HOTKEY_POLL_MS, self._poll_hotkey)
                except Exception:
                    self._hotkey_poll_id = None

    def _hotkey_loop(self):
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            if not user32.RegisterHotKey(None, HOTKEY_ID, HOTKEY_MOD, HOTKEY_VK):
                err = ctypes.windll.kernel32.GetLastError()
                self._hotkey_queue.put(f"fail:GetLastError={err}")
                return
            try:
                msg = wintypes.MSG()
                while user32.GetMessageA(ctypes.byref(msg), None, 0, 0) != 0:
                    if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                        self._hotkey_queue.put("toggle")
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageA(ctypes.byref(msg))
            finally:
                try:
                    user32.UnregisterHotKey(None, HOTKEY_ID)
                except Exception:
                    pass
        except Exception as e:  # noqa: BLE001 -- must never kill silently again
            try:
                self._hotkey_queue.put(f"fail:{e}")
            except Exception:
                pass

    def _report(self, text: str, kind: str):
        report = getattr(self.root, "report_status", None)
        if callable(report):
            try:
                report(text, kind)
            except Exception:
                pass

    # ---- config ------------------------------------------------------
    def _save_meta(self):
        """The owner persists dock_enabled/dock_autohide in user settings."""
        if self._save_callback is not None:
            try:
                self._save_callback()
            except Exception:
                pass


def _hotkey_supported() -> bool:
    try:
        import ctypes

        ctypes.windll.user32  # noqa: B018 -- presence check
        return True
    except Exception:
        return False
