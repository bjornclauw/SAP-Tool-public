"""UpdatePane -- the Update tab inside the Settings window.

The check, update and revert work runs off the Tk thread and comes back
through a queue, so only the main thread touches widgets. The logic itself
lives in ``core.updater``.

Layout: a wide "What's new" card on the left and a narrower status/actions
card on the right. That keeps the release notes readable while the action the
user must take stays in one predictable place.
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from core import updater

from . import ui_styles
from .ui_styles import (
    primary_button,
    styled_button,
    styled_label,
    surface_panel,
)

_MAX_COMMITS = 8
_MAX_NOTES = 3
_MAX_NOTE_LINES = 6

_STATUS_COLORS = {
    "info": ui_styles.COLORS["info"],
    "success": ui_styles.COLORS["success"],
    "warning": ui_styles.COLORS["warning"],
    "error": ui_styles.COLORS["error"],
    "muted": ui_styles.COLORS["text_secondary"],
}


class UpdatePane(tk.Frame):
    """Installed vs upstream version, release notes, update and one-step revert."""

    def __init__(
        self,
        master: tk.Misc,
        is_busy: Callable[[], str | None],
        on_exit: Callable[[], None],
        check_fn: Callable[[], updater.UpdateCheck] | None = None,
        update_fn: Callable[[], str] | None = None,
        previous_fn: Callable[[], dict | None] | None = None,
        revert_fn: Callable[[], str] | None = None,
    ):
        super().__init__(master, bg=ui_styles.COLORS["bg"])
        self._is_busy = is_busy
        self._on_exit = on_exit
        self._check_fn = check_fn or (lambda: updater.check_for_update())
        self._update_fn = update_fn or (lambda: updater.apply_update())
        self._previous_fn = previous_fn
        self._revert_fn = revert_fn or (lambda: updater.revert_update())
        self._check: updater.UpdateCheck | None = None
        self._previous: dict | None = None
        self._working = False
        self._checking = False
        self._checked = False
        self._queue: queue.Queue = queue.Queue()
        self._drain_id: str | None = None

        self._build()
        self.bind("<Destroy>", self._on_destroy, "+")
        self._start_drain()

    # ---- lifecycle ------------------------------------------------------
    def close(self):
        if self._drain_id is not None:
            try:
                self.after_cancel(self._drain_id)
            except tk.TclError:
                pass
            self._drain_id = None

    def _on_destroy(self, event):
        if event.widget is self:
            self.close()

    def ensure_checked(self):
        if not self._checked and not self._checking:
            self.start_check()

    # ---- layout --------------------------------------------------------
    def _build(self):
        content = tk.Frame(self, bg=ui_styles.COLORS["bg"])
        content.pack(fill="both", expand=True)
        content.grid_columnconfigure(0, weight=1)
        content.grid_columnconfigure(1, weight=0, minsize=330)
        content.grid_rowconfigure(0, weight=1)

        notes_card, notes_inner = self._card(content)
        notes_card.grid(row=0, column=0, sticky="nsew",
                        padx=(0, ui_styles.SPACING["comfortable"]))
        self._notes_title = styled_label(
            notes_inner, "Release notes", font=ui_styles.FONTS["header"],
            bg=ui_styles.COLORS["surface"])
        self._notes_title.pack(anchor="w")
        self._notes_subtitle = styled_label(
            notes_inner, "They appear when a newer version is available.",
            font=ui_styles.FONTS["small"],
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text_secondary"],
        )
        self._notes_subtitle.pack(anchor="w", pady=(2, ui_styles.SPACING["normal"]))

        self._notes_body = tk.Frame(notes_inner, bg=ui_styles.COLORS["surface"])
        self._notes_body.pack(fill="both", expand=True)

        self._notes_wrap = tk.Frame(
            self._notes_body, bg=ui_styles.COLORS["surface"])
        self._notes_text = tk.Text(
            self._notes_wrap, height=12, width=54, wrap="word",
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
            font=ui_styles.FONTS["body"], relief="flat", bd=0,
            highlightthickness=0, padx=0, pady=0, state="disabled",
            cursor="arrow",
        )
        self._notes_text.tag_configure(
            "version", font=(ui_styles.FONTS["header"][0], 9, "bold"))
        self._notes_text.tag_configure(
            "muted", foreground=ui_styles.COLORS["text_secondary"])
        notes_scroll = ttk.Scrollbar(
            self._notes_wrap, orient="vertical", command=self._notes_text.yview)
        self._notes_text.configure(yscrollcommand=notes_scroll.set)
        notes_scroll.pack(side="right", fill="y")
        self._notes_text.pack(side="left", fill="both", expand=True)

        self._commit_label = styled_label(
            self._notes_body, "", font=ui_styles.FONTS["body"],
            bg=ui_styles.COLORS["surface"], justify="left", wraplength=500,
        )
        self._notes_empty = styled_label(
            self._notes_body, "", font=ui_styles.FONTS["body"],
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text_secondary"], justify="center",
            wraplength=500,
        )

        status_card, status_inner = self._card(content)
        status_card.grid(row=0, column=1, sticky="nsew")
        head = tk.Frame(status_inner, bg=ui_styles.COLORS["surface"])
        head.pack(fill="x")
        self._status_dot = tk.Label(
            head, text="\u25cf", font=ui_styles.FONTS["body"],
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text_secondary"],
        )
        self._status_dot.pack(side="left", padx=(0, 6))
        self._headline_label = tk.Label(
            head, text="Checking for updates…",
            font=(ui_styles.FONTS["header"][0], 11, "bold"),
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
            anchor="w",
        )
        self._headline_label.pack(side="left", fill="x", expand=True)

        self._status_label = styled_label(
            status_inner, "Looking at the public install for a newer release.",
            font=ui_styles.FONTS["body"],
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text_secondary"], justify="left",
            wraplength=280,
        )
        self._status_label.pack(anchor="w", pady=(6, 10))

        versions = tk.Frame(status_inner, bg=ui_styles.COLORS["surface"])
        versions.pack(fill="x")
        self._installed_label = styled_label(
            versions, "Installed: …", font=ui_styles.FONTS["body"],
            bg=ui_styles.COLORS["surface"],
        )
        self._installed_label.pack(anchor="w")
        self._latest_label = styled_label(
            versions, "Latest: …", font=ui_styles.FONTS["body"],
            bg=ui_styles.COLORS["surface"],
        )
        self._latest_label.pack(anchor="w")

        self._checked_label = styled_label(
            status_inner, "", font=ui_styles.FONTS["small"],
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text_secondary"],
        )
        self._checked_label.pack(anchor="w", pady=(4, 0))

        actions = tk.Frame(status_inner, bg=ui_styles.COLORS["surface"])
        actions.pack(fill="x", pady=(ui_styles.SPACING["normal"], 0))
        self._update_button = primary_button(
            actions, "Update & restart", command=self._start_update,
            state="disabled",
        )
        self._update_button.pack(fill="x")
        self._revert_button = styled_button(
            actions, text="Revert", command=self._start_revert)
        self._check_button = styled_button(
            actions, text="Check again", command=self.start_check)
        self._check_button.pack(fill="x", pady=(6, 0))

        self._safety_label = styled_label(
            status_inner,
            "Updates are blocked while a SAP script or auto-upload is running. "
            "Settings, scripts and local data are kept.",
            font=ui_styles.FONTS["small"],
            bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text_secondary"],
            justify="left", wraplength=280,
        )
        self._safety_label.pack(side="bottom", anchor="w", pady=(10, 0))

        self._render_empty("Checking the public install…", "Please wait.")
        self._set_update_state(False)

    def _card(self, parent):
        return surface_panel(parent)

    # ---- check ---------------------------------------------------------
    def start_check(self):
        if self._working or self._checking:
            return
        self._checking = True
        self._checked = False
        self._set_update_state(False)
        self._set_headline("Checking for updates…", "muted")
        self._set_status("Looking at the public install for a newer release.")
        self._checked_label.config(text="")
        self._render_empty("Checking the public install…", "Please wait.")
        self._spawn(self._run_check)

    def _run_check(self):
        check = self._check_fn()
        previous = self._previous_fn() if self._previous_fn is not None else None
        self._call(self._apply_check, check, previous)

    def _apply_check(self, check: updater.UpdateCheck,
                     previous: dict | None = None):
        self._checking = False
        self._checked = True
        self._check = check
        self._previous = previous
        self._installed_label.config(
            text=f"Installed: {check.local or 'unknown'}")
        self._latest_label.config(
            text=f"Latest: {check.remote or 'unknown'}")
        self._checked_label.config(
            text=f"Checked at {time.strftime('%H:%M')}")

        notes = getattr(check, "notes", ())
        commits = getattr(check, "commits", ())
        if notes:
            self._render_notes(notes)
        elif commits:
            self._render_commits(commits)
        elif check.error:
            self._render_empty(
                "Release notes could not be loaded.",
                "Check the connection and try again.")
        elif not check.newer:
            self._render_empty(
                "You're on the latest version.",
                "No newer release notes.")
        else:
            self._render_empty(
                "No release notes for this version.",
                "The latest commits are shown when available.")

        self._show_revert(previous)

        if check.error:
            self._set_headline("Could not check for updates", "error")
            self._set_status(check.error, "error")
            self._set_update_state(False)
            return
        if check.newer:
            self._set_headline("Update available", "info")
            self._set_status("A newer version is ready to install.")
            self._set_update_state(True)
        else:
            self._set_headline("You're up to date", "success")
            self._set_status("Everything is up to date.", "success")
            self._set_update_state(False)

    def _render_notes(self, notes):
        self._notes_title.config(text="What's new")
        self._notes_subtitle.config(
            text="Release notes from the public install.")
        self._hide_notes_body()
        try:
            self._notes_text.config(state="normal", height=12)
            self._notes_text.delete("1.0", "end")
            for note in notes[:_MAX_NOTES]:
                self._notes_text.insert(
                    "end", f"{note.version}  ·  {note.date}\n", "version")
                shown = note.lines[:_MAX_NOTE_LINES]
                for item in shown:
                    self._notes_text.insert("end", f"• {item}\n")
                if len(note.lines) > len(shown):
                    self._notes_text.insert("end", "…\n", "muted")
                self._notes_text.insert("end", "\n")
            self._notes_text.config(state="disabled")
            self._notes_wrap.pack(fill="both", expand=True)
        except tk.TclError:
            pass

    def _render_commits(self, commits):
        self._notes_title.config(text="Changes since your version")
        self._notes_subtitle.config(text="Commits in the public install.")
        self._hide_notes_body()
        lines = [f"• {commit.subject} ({commit.date})"
                 for commit in commits[:_MAX_COMMITS]]
        remaining = len(commits) - _MAX_COMMITS
        if remaining > 0:
            lines.append(f"… and {remaining} more")
        self._commit_label.config(text="\n".join(lines))
        self._commit_label.pack(fill="both", expand=True)

    def _render_empty(self, message: str,
                      subtitle: str = "No newer release notes."):
        self._notes_title.config(text="Release notes")
        self._notes_subtitle.config(text=subtitle)
        self._hide_notes_body()
        self._notes_empty.config(text=message)
        self._notes_empty.pack(fill="both", expand=True)

    def _hide_notes_body(self):
        for widget in (self._notes_wrap, self._commit_label, self._notes_empty):
            try:
                if widget.winfo_manager():
                    widget.pack_forget()
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
                fill="x", pady=(6, 0), before=self._check_button)

    # ---- update --------------------------------------------------------
    def _start_update(self):
        if self._working or self._check is None or not self._check.newer:
            return
        blocker = self._is_busy()
        if blocker:
            self._set_headline("Update blocked", "warning")
            self._set_status(blocker, "warning")
            return
        self._working = True
        self._set_update_state(False)
        self._revert_button.config(state="disabled")
        self._check_button.config(state="disabled")
        self._set_headline("Updating…", "warning")
        self._set_status("Installing the new version. The app will restart.")
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
            self._set_headline("Revert blocked", "warning")
            self._set_status(blocker, "warning")
            return
        self._working = True
        self._set_update_state(False)
        self._revert_button.config(state="disabled")
        self._check_button.config(state="disabled")
        version = self._previous.get("from_version", "the previous version")
        self._set_headline("Restoring…", "warning")
        self._set_status(f"Restoring {version}. The app will restart.")
        self._spawn(self._run_revert)

    def _run_revert(self):
        try:
            version = self._revert_fn()
        except Exception as exc:
            self._call(self._fail, str(exc))
            return
        self._call(self._restart, version, "Reverted")

    def _restart(self, version: str, verb: str = "Updated"):
        self._set_headline("Restarting…", "success")
        self._set_status(f"{verb} to {version}. Restarting…")
        try:
            updater.relaunch()
        except Exception as exc:
            self._fail(f"Could not restart: {exc}")
            return
        self._on_exit()

    def _fail(self, message: str):
        self._working = False
        self._set_headline("Update failed", "error")
        self._set_status(f"Update failed: {message}", "error")
        self._check_button.config(state="normal")
        if self._check is not None and self._check.newer:
            self._set_update_state(True)
        if self._previous and self._previous.get("from_version"):
            self._revert_button.config(state="normal")

    # ---- helpers -------------------------------------------------------
    def _set_update_state(self, enabled: bool):
        try:
            self._update_button.config(
                state="normal" if enabled else "disabled",
                bg=(ui_styles.COLORS["primary"] if enabled
                    else ui_styles.COLORS["border_light"]),
                fg=("#ffffff" if enabled
                    else ui_styles.COLORS["text_disabled"]),
                activebackground=(ui_styles.COLORS["primary_hover"]
                                  if enabled
                                  else ui_styles.COLORS["border_light"]),
                activeforeground=("#ffffff" if enabled
                                  else ui_styles.COLORS["text_disabled"]),
            )
        except tk.TclError:
            pass

    def _set_headline(self, text: str, kind: str = "info"):
        try:
            color = _STATUS_COLORS.get(kind, ui_styles.COLORS["text"])
            self._headline_label.config(text=text, fg=color)
            self._status_dot.config(fg=color)
        except tk.TclError:
            pass

    def _set_status(self, message: str, kind: str = "info"):
        try:
            color = _STATUS_COLORS.get(kind, ui_styles.COLORS["text_secondary"])
            self._status_label.config(text=message, fg=color)
        except tk.TclError:
            pass

    def _spawn(self, target: Callable[[], None]):
        threading.Thread(target=target, daemon=True).start()

    def _call(self, callback: Callable, *args):
        """Hand work from a worker thread to the Tk thread via a queue."""
        self._queue.put((callback, args))

    def _start_drain(self):
        try:
            self._drain_id = self.after(50, self._drain)
        except tk.TclError:
            self._drain_id = None

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
