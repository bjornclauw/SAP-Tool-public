"""Check-for-updates dialog.

A helper modal: support accent, centred on the window that opened it. Git work
runs off the Tk thread and comes back through a queue, so only the main thread
touches widgets. The logic itself lives in ``core.updater``.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from core import updater

from . import ui_styles
from .ui_styles import primary_button, styled_button, styled_frame, styled_label
from .window_chrome import show_centered

_MAX_COMMITS = 8
_MAX_NOTES = 3
_MAX_NOTE_LINES = 6


class UpdateWindow(tk.Toplevel):
    """Installed vs upstream version, release notes, update and one-step revert.

    A note about the manager: the check, update and revert work runs off the Tk
    thread and comes back through a queue; only the main thread touches widgets.
    """

    _instance: UpdateWindow | None = None

    @classmethod
    def open(
        cls,
        parent: tk.Misc,
        is_busy: Callable[[], str | None],
        on_exit: Callable[[], None],
        check_fn: Callable[[], updater.UpdateCheck] | None = None,
        update_fn: Callable[[], str] | None = None,
        previous_fn: Callable[[], dict | None] | None = None,
        revert_fn: Callable[[], str] | None = None,
    ) -> UpdateWindow:
        existing = cls._instance
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return existing
        cls._instance = cls(
            parent, is_busy, on_exit, check_fn, update_fn, previous_fn, revert_fn)
        return cls._instance

    def __init__(
        self,
        parent: tk.Misc,
        is_busy: Callable[[], str | None],
        on_exit: Callable[[], None],
        check_fn: Callable[[], updater.UpdateCheck] | None = None,
        update_fn: Callable[[], str] | None = None,
        previous_fn: Callable[[], dict | None] | None = None,
        revert_fn: Callable[[], str] | None = None,
    ):
        super().__init__(parent)
        self._is_busy = is_busy
        self._on_exit = on_exit
        self._check_fn = check_fn or (lambda: updater.check_for_update())
        self._update_fn = update_fn or (lambda: updater.apply_update())
        self._previous_fn = previous_fn
        self._revert_fn = revert_fn or (lambda: updater.revert_update())
        self._check: updater.UpdateCheck | None = None
        self._previous: dict | None = None
        self._working = False
        self._queue: queue.Queue = queue.Queue()
        self._drain_id: str | None = None

        self.withdraw()
        self.title("Check for updates")
        self.resizable(False, False)
        self.config(bg=ui_styles.COLORS["bg"])
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self.update_idletasks()
        show_centered(self, ui_styles.SUPPORT_COLOR)
        self._drain()
        self._start_check()

    def destroy(self):
        if self._drain_id is not None:
            try:
                self.after_cancel(self._drain_id)
            except tk.TclError:
                pass
            self._drain_id = None
        if UpdateWindow._instance is self:
            UpdateWindow._instance = None
        super().destroy()

    # ---- layout --------------------------------------------------------

    def _build(self):
        pad = ui_styles.SPACING["comfortable"]
        body = styled_frame(self)
        body.pack(fill="both", expand=True, padx=pad, pady=pad)

        styled_label(body, "SAP Work Order Tool", font=ui_styles.FONTS["title"]).pack(
            anchor="w"
        )

        versions = styled_frame(body)
        versions.pack(fill="x", pady=(ui_styles.SPACING["normal"], 0))
        self._installed_label = styled_label(versions, "Installed: …")
        self._installed_label.pack(anchor="w")
        self._latest_label = styled_label(versions, "Latest: …")
        self._latest_label.pack(anchor="w")

        self._commit_label = styled_label(
            body, "", font=ui_styles.FONTS["small"],
            fg=ui_styles.COLORS["text_secondary"], justify="left",
        )
        self._commit_label.pack(anchor="w", pady=(ui_styles.SPACING["tight"], 0))

        self._notes_frame = styled_frame(body)
        self._notes_text = tk.Text(
            self._notes_frame, height=8, width=54, wrap="word",
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
            font=ui_styles.FONTS["small"], relief="flat", bd=0,
            highlightthickness=0, padx=6, pady=4, state="disabled",
            cursor="arrow",
        )
        notes_scroll = ttk.Scrollbar(
            self._notes_frame, orient="vertical", command=self._notes_text.yview)
        self._notes_text.configure(yscrollcommand=notes_scroll.set)
        notes_scroll.pack(side="right", fill="y")
        self._notes_text.pack(side="left", fill="both", expand=True)

        self._status_label = styled_label(body, "", wraplength=380)
        self._status_label.pack(anchor="w", pady=(ui_styles.SPACING["normal"], 0))

        actions = styled_frame(self)
        actions.pack(fill="x", padx=pad, pady=(0, pad))
        self._update_button = primary_button(
            actions, "Update & restart", command=self._start_update, state="disabled",
        )
        self._update_button.pack(side="left")
        self._revert_button = styled_button(
            actions, text="Revert", command=self._start_revert)
        self._close_button = styled_button(
            actions, text="Close", command=self.destroy)
        self._close_button.pack(side="left", padx=ui_styles.SPACING["normal"])

    # ---- check ---------------------------------------------------------

    def _start_check(self):
        self._set_status("Checking for updates…")
        self._spawn(self._run_check)

    def _run_check(self):
        check = self._check_fn()
        previous = self._previous_fn() if self._previous_fn is not None else None
        self._call(self._apply_check, check, previous)

    def _apply_check(self, check: updater.UpdateCheck,
                     previous: dict | None = None):
        self._check = check
        self._previous = previous
        self._installed_label.config(text=f"Installed: {check.local or 'unknown'}")
        self._latest_label.config(text=f"Latest: {check.remote or 'unknown'}")

        notes = getattr(check, "notes", ())
        if notes:
            self._commit_label.pack_forget()
            self._render_notes(notes)
        elif check.commits:
            lines = [f"• {commit.subject} ({commit.date})" for commit in check.commits[:_MAX_COMMITS]]
            remaining = len(check.commits) - _MAX_COMMITS
            if remaining > 0:
                lines.append(f"… and {remaining} more")
            self._commit_label.config(text="\n".join(lines))

        self._show_revert(previous)

        if check.error:
            self._set_status(check.error, "error")
            return
        if check.newer:
            self._set_status(
                f"Version {check.remote or 'newer'} is available. "
                "Your settings and scripts are kept."
            )
            self._update_button.config(state="normal")
        else:
            self._set_status("You are up to date.")
            self._update_button.config(state="disabled")

    def _render_notes(self, notes):
        lines = []
        for note in notes[:_MAX_NOTES]:
            lines.append(f"{note.version}  ·  {note.date}")
            shown = note.lines[:_MAX_NOTE_LINES]
            lines.extend(f"• {item}" for item in shown)
            if len(note.lines) > len(shown):
                lines.append("…")
            lines.append("")
        try:
            self._notes_text.config(
                state="normal", height=max(12, min(16, len(lines))))
            self._notes_text.delete("1.0", "end")
            self._notes_text.insert("1.0", "\n".join(lines).rstrip())
            self._notes_text.config(state="disabled")
            self._notes_frame.pack(
                fill="x", pady=(ui_styles.SPACING["tight"], 0),
                before=self._status_label)
        except tk.TclError:
            pass

    def _show_revert(self, previous: dict | None):
        version = (previous or {}).get("from_version")
        if not version:
            self._revert_button.pack_forget()
            return
        self._revert_button.config(
            text=f"Revert to {version}", state="normal")
        if self._revert_button.winfo_manager() == "":
            self._revert_button.pack(
                side="left", padx=ui_styles.SPACING["normal"],
                before=self._close_button)

    # ---- update --------------------------------------------------------

    def _start_update(self):
        if self._working or self._check is None or not self._check.newer:
            return
        blocker = self._is_busy()
        if blocker:
            self._set_status(blocker, "warning")
            return
        self._working = True
        self._update_button.config(state="disabled")
        self._revert_button.config(state="disabled")
        self._set_status("Updating…")
        self._spawn(self._run_update)

    def _run_update(self):
        try:
            version = self._update_fn()
        except Exception as exc:
            self._call(self._fail, str(exc))
            return
        self._call(self._restart, version)

    def _start_revert(self):
        if self._working or not self._previous:
            return
        blocker = self._is_busy()
        if blocker:
            self._set_status(blocker, "warning")
            return
        self._working = True
        self._update_button.config(state="disabled")
        self._revert_button.config(state="disabled")
        version = self._previous.get("from_version", "the previous version")
        self._set_status(f"Restoring {version}…")
        self._spawn(self._run_revert)

    def _run_revert(self):
        try:
            version = self._revert_fn()
        except Exception as exc:
            self._call(self._fail, str(exc))
            return
        self._call(self._restart, version, "Reverted")

    def _restart(self, version: str, verb: str = "Updated"):
        self._set_status(f"{verb} to {version}. Restarting…")
        try:
            updater.relaunch()
        except Exception as exc:
            self._fail(f"Could not restart: {exc}")
            return
        try:
            self.destroy()
        except tk.TclError:
            pass
        self._on_exit()

    def _fail(self, message: str):
        self._working = False
        self._set_status(f"Update failed: {message}", "error")
        if self._check is not None and self._check.newer:
            self._update_button.config(state="normal")
        if self._previous and self._previous.get("from_version"):
            self._revert_button.config(state="normal")

    # ---- helpers -------------------------------------------------------

    def _set_status(self, message: str, kind: str = "info"):
        try:
            color = {
                "error": ui_styles.COLORS["error"],
                "warning": ui_styles.COLORS["warning"],
                "info": ui_styles.COLORS["text_secondary"],
            }[kind]
            self._status_label.config(text=message, fg=color)
        except tk.TclError:
            pass

    def _spawn(self, target: Callable[[], None]):
        threading.Thread(target=target, daemon=True).start()

    def _call(self, callback: Callable, *args):
        """Hand work from a worker thread to the Tk thread via a queue."""
        self._queue.put((callback, args))

    def _drain(self):
        try:
            while True:
                callback, args = self._queue.get_nowait()
                callback(*args)
        except queue.Empty:
            pass
        try:
            self._drain_id = self.after(50, self._drain)
        except tk.TclError:
            self._drain_id = None
