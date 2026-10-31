"""SapImportWindow — main UI + orchestration (Phase 1 split, no behavior change)."""

from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import ttk

from core import config as cfg
from core import user_settings
from ui import ui_styles
from ui.custom_menu import CustomMenuBar, MenuBarItem
from ui.custom_widgets import BaseToolWindow, CustomButton
from ui.ui_styles import styled_frame, styled_label

from . import grid_config
from .api_client import ImportApiClient
from .dialogs import EditRowDialog, ask_free_text, show_failure_dialog
from .failures import clear_failure, has_failure, record_failure
from .grid import ImportGrid
from .models import (
    CANCEL_REQUESTED,
    CANCEL_STATUSES,
    CANCELLED,
    FAILED_STATUSES,
    FINISHED_STATUSES,
    ITERATED_STATUSES,
    PIC_URL_BASE,
    PICS_FOLDER,
    STATUS_COLUMN_MAPPING,
    STATUSES,
    auto_upload_candidate,
    is_image_file,
)
from .options import (
    dropdown_lists,
    load_options,
    load_wo_tree,
)
from .options_popup import OptionsPopup
from .sap_reply import is_success, parse_created_number

# The statuses auto-assign may touch: work SAP has not been told about yet.
# Completed (6), already-added (7), processing (1) and cancelled (10) rows keep
# whatever revision SAP was given, so the local row never diverges from it.
_ASSIGNABLE_STATUSES = frozenset({"0", "3", "8", "9"})

# SAP Import's own accent, distinct from Print Orders' teal and the app-wide
# primary blue.
_ACCENT = ui_styles.TOOL_COLORS["sap_import"]
_ACCENT_HOVER = "#3f51b5"

_DEFAULT_IMPORT_COLUMNS = ["Auto", "Status", "_id", "PG", "FLOC", "WP", "STEXT",
                           "WO_TYPE", "WO_KEY", "PRIOR", "WO", "USTATUS", "PICS",
                           "SAP_NUMBER", "Bericht"]

# Columns shipped after the saved per-user layout: if a saved layout predates
# one, it is surfaced (not left silently invisible). Once saved it stays put.
_NEW_COLUMNS = ("Auto",)


class SapImportWindow(BaseToolWindow):
    def __init__(self, parent, engine, api_client: ImportApiClient | None = None,
                 is_fake=None, counter=None, on_queue_changed=None):
        super().__init__(parent, engine, titlebar_color=ui_styles.TOOL_COLORS["sap_import"])
        self.title("SAP Import — Work Orders")
        self.geometry("1280x780")
        self.minsize(950, 550)

        # API errors can arrive from the background fetch worker; queue those
        # and let the main-thread poll paint them.
        self._api_messages: queue.Queue = queue.Queue()
        # Every server call goes through this one client seam (see api_client).
        self._api = api_client or ImportApiClient(
            report_status=self._api_report, auth=self._auth_override()
        )
        # Shared with the toolbar: records each list response so the toolbar's
        # pending counter can reuse it, and guards against concurrent fetches.
        self._counter = counter
        # Under Fake SAP the server flag is meaningless, so no flag call is made
        # (see docs/decisions/0005-upload-flag-semantics.md). The toolbar passes a
        # live getter so toggling Fake SAP takes effect without reopening.
        self._is_fake = is_fake or (lambda: False)
        # Fired after the local queue is written, so the toolbar's pending
        # counter reflects a fetch/upload immediately.
        self._on_queue_changed = on_queue_changed

        self._rows_data: list[dict] = []
        self._auto_enabled = False
        self._auto_paused = False
        self._auto_assign_enabled = False
        self._auto_id = None
        self._processing = False
        self._stop_requested = False
        self._fetching = False
        self._image_download_running = False
        self._pending_queue: list[dict] = []
        self._done_count = 0
        self._total = 0
        self._progress_reset_id = None

        # The active downtime week: register + REV dropdown follow it. Defaults
        # to the current ISO week; the ‹ › stepper moves it (no restart persistence).
        self._active_dt = datetime.now()

        self._build_menu_bar()
        self._saved_settings = self._current_settings()
        self._build_action_row()
        self._build_meta_row()
        self._build_progress_area()
        self._build_grid()
        self._build_status_bar()
        # The SAP engine blocks on its STA worker while a script runs; pumping
        # Tk events there keeps Send interactive (same hook PRH uses).
        self.engine.process_events = self._toolbar.update
        self._show()

        os.makedirs(PICS_FOLDER, exist_ok=True)
        self._load_queue()
        if self._rows_data:
            self._rebuild_grid()
            self.report_status(f"Restored {len(self._rows_data)} queued items", "info")

    # ---- persistence --------------------------------------------------------
    def _queue_path(self) -> Path:
        return Path(__file__).parent.parent.parent / "data" / "si_queue.json"

    def _load_queue(self):
        p = self._queue_path()
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._rows_data = [d for d in data if isinstance(d, dict)]
                # Retired status 66 (FORCEREN) is the cancel request now.
                for row in self._rows_data:
                    if str(row.get("Status")) == "66":
                        row["Status"] = CANCEL_REQUESTED
        except Exception:
            pass

    def _save_queue(self):
        try:
            p = self._queue_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self._rows_data, indent=2, ensure_ascii=False),
                         encoding="utf-8")
            callback = getattr(self, "_on_queue_changed", None)
            if callback is not None:
                try:
                    callback()
                except Exception:
                    pass
        except Exception:
            pass

    # ---- layout ---------------------------------------------------------------

    def _build_week_row(self):
        # kept for compat — now delegates to _build_meta_row
        pass

    def _build_meta_row(self):
        # A — merged week + filter on calm #f8f9fa, single line
        row = tk.Frame(self, bg="#f8f9fa", highlightbackground="#dee2e6", highlightthickness=1)
        row.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                 pady=(ui_styles.SPACING["tight"], 0), ipady=5)
        # left: active week stepper + revisions
        left = tk.Frame(row, bg="#f8f9fa")
        left.pack(side="left")
        tk.Button(left, text="\u2039", command=lambda: self._shift_active_week(-1),
                  bg="white", fg="#495057", relief="solid", bd=1, padx=6, cursor="hand2",
                  font=(ui_styles.FONTS["body"][0], 8)).pack(side="left", padx=(0, 4))
        self._week_label_var = tk.StringVar()
        tk.Label(left, textvariable=self._week_label_var, bg="#f8f9fa", fg="#212529",
                 font=(ui_styles.FONTS["body"][0], 9, "bold")).pack(side="left", padx=(0, 4))
        tk.Button(left, text="\u203a", command=lambda: self._shift_active_week(1),
                  bg="white", fg="#495057", relief="solid", bd=1, padx=6, cursor="hand2",
                  font=(ui_styles.FONTS["body"][0], 8)).pack(side="left", padx=(0, 4))
        self._week_rev_var = tk.StringVar()
        self._rev_label = tk.Label(left, textvariable=self._week_rev_var, bg="#f8f9fa",
                                   fg="#6c757d", font=(ui_styles.FONTS["body"][0], 8))
        self._rev_label.pack(side="left", padx=(2, 6))
        tk.Button(left, text="✎", command=self._open_revisions_register,
                  bg="white", fg="#495057", relief="solid", bd=1, padx=6, cursor="hand2",
                  font=(ui_styles.FONTS["body"][0], 8)).pack(side="left")
        self._refresh_week_row()
        ttk.Separator(row, orient="vertical").pack(side="left", fill="y", padx=10, pady=2)
        # center: filter + status
        tk.Label(row, text="Filter:", bg="#f8f9fa", fg="#6c757d",
                 font=ui_styles.FONTS["body"]).pack(side="left", padx=(0, 4))
        self._filter_var = tk.StringVar()
        self._filter_var.trace_add("write", lambda *_: self._rebuild_grid())
        tk.Entry(row, textvariable=self._filter_var, width=22,
                 bg="white", fg="#212529", relief="solid", bd=1, highlightthickness=0).pack(
                     side="left", ipady=2)
        tk.Label(row, text="Status:", bg="#f8f9fa", fg="#6c757d",
                 font=ui_styles.FONTS["body"]).pack(side="left", padx=(8, 4))
        self._filter_status_var = tk.StringVar(value="All")
        status_filter_values = ["All"] + [
            f"{status.label} ({code})" for code, status in STATUSES.items()
        ]
        cb = ttk.Combobox(row, textvariable=self._filter_status_var, width=18,
                          values=status_filter_values,
                          state="readonly", font=(ui_styles.FONTS["body"][0], 8))
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._rebuild_grid())
        tk.Button(row, text="✕", command=self._clear_filter,
                  bg="white", fg="#6c757d", relief="solid", bd=1, cursor="hand2",
                  font=(ui_styles.FONTS["body"][0], 8), padx=6).pack(side="left", padx=(6, 0))
        # right: hint
        tk.Label(row, text="↕ sort · REV click for dropdown", bg="#f8f9fa", fg="#adb5bd",
                 font=(ui_styles.FONTS["body"][0], 7)).pack(side="right", padx=8)
        self._meta_row = row

    # ---- active downtime week (register + REV dropdown follow it) -------------

    def _active_week_key(self) -> str:
        from .revisions import week_key
        return week_key(getattr(self, "_active_dt", None))

    def _shift_active_week(self, weeks: int):
        self._active_dt += timedelta(weeks=weeks)
        self._on_register_changed()

    def _open_revisions_register(self):
        from .week_popup import RevisionsRegisterPopup
        popup = RevisionsRegisterPopup(self, week=self._active_week_key())
        popup.bind("<Destroy>", lambda e: self._on_register_changed() if e.widget == popup else None)

    def _on_register_changed(self):
        """The active week's register (or the week itself) changed."""
        self._refresh_week_row()
        self._refresh_dropdowns()
        if self._auto_assign_on() and getattr(self, "_rows_data", []):
            if self._apply_auto_assign_to(self._rows_data):
                self._rebuild_grid()
                self._save_queue()

    def _refresh_week_row(self):
        from .revisions import get_revisions, parse_week_key
        wk = self._active_week_key()
        yr, wn = parse_week_key(wk)
        revs = get_revisions(wk)
        rev_txt = ", ".join(revs) if revs else "— none —"
        if hasattr(self, "_week_label_var"):
            self._week_label_var.set(f"W{wn:02d} · {yr}")
        if hasattr(self, "_week_rev_var"):
            self._week_rev_var.set(f"Revisions: {rev_txt}")
        if hasattr(self, "_rev_label"):
            self._rev_label.config(fg="#004085" if revs else "#6c757d")

    def _get_rev_options(self) -> list[str]:
        from .revisions import get_revisions
        opts = get_revisions(self._active_week_key())
        return opts if opts else []

    def _build_action_row(self):
        # A — primary | secondary | switch, grouped with separator
        row = styled_frame(self)
        row.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                 pady=ui_styles.SPACING["tight"])
        # left: primary
        primary = styled_frame(row)
        primary.pack(side="left")
        self._fetch_btn = None
        self._fetch_btn_text = "Fetch New"
        for btn_id, text, cmd in (
            ("si_fetch_btn", "\u27f3 Fetch New", self._fetch_new),
            ("si_send_btn", "\u25b6 Send to SAP", self._on_send_clicked),
        ):
            btn = CustomButton(primary, "buttons", btn_id, text=text, command=cmd,
                               bg=_ACCENT, fg="#ffffff",
                               activebackground=_ACCENT_HOVER)
            btn.pack(side="left", padx=(0, ui_styles.SPACING["tight"]))
            if btn_id == "si_fetch_btn":
                self._fetch_btn = btn
                self._fetch_btn_text = text
            elif btn_id == "si_send_btn":
                self._send_btn = btn
        ttk.Separator(row, orient="vertical").pack(side="left", fill="y", padx=ui_styles.SPACING["comfortable"], pady=2)
        # middle: secondary muted outline
        secondary = styled_frame(row)
        secondary.pack(side="left")
        for btn_id, text, cmd in (
            ("si_reset_btn", "↻ Reset Failed", self._reset_failed),
            ("si_clear_btn", "✕ Clear Completed", self._clear_completed),
            ("si_clear_list_btn", "Clear List", self._clear_list),
        ):
            btn = tk.Button(secondary, text=text, command=cmd,
                            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text_secondary"],
                            activebackground=ui_styles.COLORS["bg"], relief="solid", bd=1,
                            padx=10, pady=4, cursor="hand2", font=ui_styles.FONTS["body"])
            btn.pack(side="left", padx=(0, ui_styles.SPACING["tight"]))

        # right: Auto-assign toggle + the Auto upload marker button (same
        # off/running/paused cycle as the toolbar's Import marker).
        right = styled_frame(row)
        right.pack(side="right")
        # Live count of the grid's row selection (bulk-edit affordance).
        self._sel_label = tk.Label(right, text="", bg=ui_styles.COLORS["bg"],
                                   fg=_ACCENT,
                                   font=ui_styles.FONTS["body"])
        self._sel_label.pack(side="left", padx=(0, 12))
        tk.Label(right, text="Auto-assign", bg=ui_styles.COLORS["bg"],
                 fg=ui_styles.COLORS["text_secondary"], font=ui_styles.FONTS["body"]).pack(side="left", padx=(0, 4))
        self._assign_var = tk.BooleanVar(value=self._auto_assign_enabled)
        ttk.Checkbutton(right, variable=self._assign_var,
                        command=self._toggle_auto_assign).pack(side="left", padx=(0, 14))
        self._auto_btn = tk.Button(
            right, text="▶ Auto upload OFF", command=self._cycle_auto_upload,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
            activebackground=ui_styles.COLORS["bg"], relief="solid", bd=1,
            padx=10, pady=3, cursor="hand2", font=ui_styles.FONTS["body"])
        self._auto_btn.pack(side="left")
        self._sync_auto_switch()

    def _build_menu_bar(self):
        # The settings the old form row held: bound to the engine variables,
        # exposed here instead of as free-text entries above the grid.
        self._pg_var = self.bind_entry("si_pg")
        self._upload_pics_var = self.bind_entry("si_upload_pics")
        self._system_var = self.bind_entry("si_system")
        # Per-machine overrides live outside the tracked config.
        saved = user_settings.load_user_settings()
        for key, var in (("si_pg", self._pg_var),
                         ("si_upload_pics", self._upload_pics_var),
                         ("si_system", self._system_var)):
            value = saved.get(key)
            if value:
                var.set(value)
        pg_items = [
            MenuBarItem(value, variable=self._pg_var, onvalue=value, radiobutton=True)
            for value in load_options().get("PG", [])
        ]
        self.menu_bar = CustomMenuBar(self, height=28)
        self.menu_bar.add_menu("Settings", [
            MenuBarItem("Planner group", children=pg_items),
            MenuBarItem("Upload pictures", variable=self._upload_pics_var,
                        onvalue="YES", offvalue="NO"),
            MenuBarItem("SAP system\u2026", self._edit_system),
            MenuBarItem("", separator=True),
            MenuBarItem("Columns\u2026", self._configure_columns),
            MenuBarItem("Dropdown values\u2026", self._open_options_popup),
            MenuBarItem("WO types\u2026", self._open_wo_type_popup),
            MenuBarItem("Edit SAP script", children=[
                MenuBarItem("Notification (IW21)\u2026",
                            lambda: self._edit_script("si_create_notification")),
                MenuBarItem("Work order (IW31)\u2026",
                            lambda: self._edit_script("si_create_workorder")),
                MenuBarItem("Upload picture\u2026",
                            lambda: self._edit_script("si_upload_pic")),
            ]),
        ])
        self.menu_bar.pack(side="top", fill="x")

    def _current_settings(self) -> dict[str, str]:
        return {
            "si_pg": self._pg_var.get(),
            "si_upload_pics": self._upload_pics_var.get(),
            "si_system": self._system_var.get(),
        }

    def _persist_settings(self):
        """Write the three user settings back, but only when they changed.

        Auto upload and auto-assign stay session-only; these three are setup,
        not run state, so they survive a restart.
        """
        try:
            current = self._current_settings()
        except tk.TclError:
            return
        # Validate before writing: planner group and system may not be blank,
        # and upload pictures is a yes/no choice.
        for key in ("si_pg", "si_system"):
            value = current[key].strip()
            current[key] = value or self._saved_settings[key]
        current["si_upload_pics"] = (
            "YES" if current["si_upload_pics"].strip().upper() == "YES" else "NO")
        if current == self._saved_settings:
            return
        try:
            user_settings.update_user_settings(current)
            self._saved_settings = current
        except Exception:
            pass

    def _edit_system(self):
        from tkinter import simpledialog
        value = simpledialog.askstring("SAP system", "SAP system:",
                                       initialvalue=self._system_var.get(),
                                       parent=self)
        if value is not None:
            self._system_var.set(value.strip())

    def _sync_auto_switch(self):
        """Paint the Auto upload marker button from the current state."""
        btn = getattr(self, "_auto_btn", None)
        if btn is None:
            return
        state = self.auto_upload_state()
        if state == "running":
            btn.config(text="⏸ Auto upload ON", bg=ui_styles.COLORS["success"],
                       fg=ui_styles.COLORS["text_on_primary"])
        elif state == "paused":
            btn.config(text="▶ Auto upload PAUSED", bg=ui_styles.COLORS["warning"],
                       fg=ui_styles.COLORS["text_on_primary"])
        else:
            btn.config(text="▶ Auto upload OFF", bg=ui_styles.COLORS["surface"],
                       fg=ui_styles.COLORS["text"])

    def _build_filter_row(self):
        # deprecated — merged into _build_meta_row for A; keep no-op for compat
        pass

    def _clear_filter(self):
        self._filter_var.set("")
        self._filter_status_var.set("All")

    def _row_matches_filter(self, row: dict) -> bool:
        q = self._filter_var.get().strip().lower() if hasattr(self, "_filter_var") else ""
        if q:
            hay = " ".join(str(row.get(k, "")) for k in ("FLOC", "STEXT", "LTEXT", "_id", "PG", "WP")).lower()
            if q not in hay:
                return False
        status_f = self._filter_status_var.get() if hasattr(self, "_filter_status_var") else "All"
        if status_f != "All":
            code = status_f.split("(")[-1].rstrip(")") if "(" in status_f else ""
            if row.get("Status", "0") != code:
                return False
        return True

    def _set_fetch_busy(self, busy: bool):
        """Fetch owns the busy state; Send is disabled while it runs."""
        button = getattr(self, "_fetch_btn", None)
        if button is not None:
            try:
                button.config(
                    text="\u2026 Fetching" if busy else getattr(
                        self, "_fetch_btn_text", "Fetch New"),
                    state="disabled" if busy else "normal",
                )
            except tk.TclError:
                pass
        send = getattr(self, "_send_btn", None)
        if send is not None and not self._processing:
            try:
                send.config(state="disabled" if busy else "normal")
            except tk.TclError:
                pass

    def _on_send_clicked(self):
        if self._processing:
            self._stop_requested = True
            try:
                self.engine.stop()
            except Exception:
                pass
            if self._auto_id:
                try:
                    self.after_cancel(self._auto_id)
                except Exception:
                    pass
                self._auto_id = None
            self.report_status("Stopping after current item...", "warning")
            return
        self._send_to_sap()

    def _build_progress_area(self):
        pf = styled_frame(self)
        pf.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                pady=ui_styles.SPACING["tight"])
        # thicker bar
        style = ttk.Style()
        style.configure("Import.Horizontal.TProgressbar", thickness=12)
        self._prog = ttk.Progressbar(pf, orient="horizontal", mode="determinate",
                                     style="Import.Horizontal.TProgressbar")
        self._prog.pack(fill="x", expand=True, side="left")

        self._prog_label = styled_label(
            pf, "0 / 0", font=ui_styles.FONTS["body"],
            fg=ui_styles.COLORS["text_secondary"])
        self._prog_label.pack(side="left", padx=(ui_styles.SPACING["tight"], 0))

    def _build_grid(self):
        container = styled_frame(self)
        container.pack(fill="both", expand=True,
                       padx=ui_styles.SPACING["comfortable"],
                       pady=(0, ui_styles.SPACING["tight"]))

        # tksheet draws only the visible cells, so a 1000+ row queue scrolls
        # without a per-row rebuild (see docs/decisions/0004).
        self._grid = ImportGrid(
            container,
            on_edit=self._edit_row,
            on_menu=self._show_row_menu,
            on_dropdown=self._on_cell_dropdown,
            on_column_resized=self._on_column_resized,
            on_select=self._on_selection_changed,
        )
        self._apply_column_config()

    def _grid_defaults(self) -> dict:
        """The seed layout from the tracked config."""
        return cfg.get_grid_cfg(self.config_data, "si_import_grid")

    def _grid_settings(self) -> dict:
        """The live layout: the config seed with the runtime overrides applied."""
        settings = grid_config.load_settings(self._grid_defaults())
        for column in _NEW_COLUMNS:
            if (column not in settings["column_order"]
                    and column not in settings["visible_columns"]):
                settings["column_order"].insert(0, column)
                settings["visible_columns"].insert(0, column)
        return settings

    def _column_display_names(self) -> dict:
        return self._grid_settings()["column_display_names"]

    def _apply_column_config(self):
        settings = self._grid_settings()
        visible = settings["visible_columns"]
        order = settings["column_order"]
        vis_set = set(visible)
        cols = [c for c in order if c in vis_set] or visible
        if not cols:
            cols = list(_DEFAULT_IMPORT_COLUMNS)

        self._grid.set_columns(cols, settings["column_display_names"],
                               settings["column_widths"])
        self._grid.set_dropdowns(self._dropdown_options())
        self._grid.set_wo_key_map(load_wo_tree())

    def _on_column_resized(self, column: str, width: int):
        """Remember a dragged column width so it survives re-renders/restarts."""
        settings = self._grid_settings()
        settings["column_widths"][column] = int(width)
        grid_config.save_settings(settings)

    def _configure_columns(self):
        """Show/hide, reorder and rename the grid's columns, then persist."""
        from ui.edit_modal import EditModal

        settings = self._grid_settings()
        visible = settings["visible_columns"]
        order = settings["column_order"]
        available = list(dict.fromkeys(
            list(order) + list(visible) + list(self._grid.columns)
            + list(_DEFAULT_IMPORT_COLUMNS)))
        current = {
            "visible_columns": visible or available[:],
            "column_order": order or available,
            "column_display_names": settings["column_display_names"],
        }

        def save_callback(new_data: dict):
            merged = self._grid_settings()
            merged.update(new_data)
            grid_config.save_settings(merged)
            self._apply_column_config()
            self.report_status("Import columns updated", "success")

        EditModal(self, "columnconfig", current, save_callback,
                  title="Import columns", available_columns=available)

    # ---- API helpers ----------------------------------------------------------

    def _fake_sap(self) -> bool:
        """True while the toolbar is in Fake SAP mode (no real flag backend)."""
        getter = getattr(self, "_is_fake", None)
        return bool(getter()) if getter else False

    def _auth_override(self) -> str | None:
        """Resolve the Authorization header: env first, else the client's
        untracked private-file fallback."""
        return os.environ.get("BCSYS_AUTH") or None

    def _api_report(self, text: str, kind: str = "info"):
        """Paint API messages on the main thread; queue them from a worker."""
        if threading.current_thread() is threading.main_thread():
            self.report_status(text, kind)
        else:
            self._api_messages.put((text, kind))

    def _drain_api_messages(self):
        while True:
            try:
                text, kind = self._api_messages.get_nowait()
            except queue.Empty:
                return
            self.report_status(text, kind)

    # ---- fetch from API -------------------------------------------------------

    def _fetch_new(self):
        if self._processing:
            self.report_status("Already processing", "warning")
            return
        if getattr(self, "_fetching", False):
            self.report_status("Already fetching", "warning")
            return

        self.report_status("Fetching new imports...", "info")
        pg = self.engine.get_variable("si_pg") or "WP1"

        # Share the fetch guard with the toolbar's counter poll and record the
        # response so the poll reuses it instead of making a second request.
        counter = getattr(self, "_counter", None)
        if counter is not None and not counter.begin():
            self.report_status("Another fetch is in progress", "warning")
            return
        self._fetching = True
        self._set_fetch_busy(True)
        results: queue.Queue = queue.Queue()

        def worker():
            # Network only: no Tk, counter or row state off the main thread.
            try:
                result = self._api.get_list("WO", pg)
            except Exception:
                result = None
            results.put(result)

        threading.Thread(target=worker, daemon=True).start()
        self._poll_fetch(results)

    def _poll_fetch(self, results):
        if not self.winfo_exists():
            return
        self._drain_api_messages()
        try:
            result = results.get_nowait()
        except queue.Empty:
            self.after(100, lambda: self._poll_fetch(results))
            return
        self._finish_fetch(result)

    def _finish_fetch(self, result):
        self._fetching = False
        self._set_fetch_busy(False)
        self._drain_api_messages()

        counter = getattr(self, "_counter", None)
        try:
            if counter is not None:
                if result is None:
                    counter.note_request(False)
                else:
                    counter.record(result.get("data", []))
        finally:
            if counter is not None:
                counter.end()

        if result is None:
            return

        data_list = result.get("data", [])
        if not data_list:
            self.report_status("No new items found", "info")
            self._update_progress()
            return

        existing_ids = {r["_id"] for r in self._rows_data if r.get("_id")}
        new_count = 0
        upgraded_count = 0
        for item in data_list:
            item_id = item.get("_id", "")
            if item_id and item_id in existing_ids:
                continue
            row = {"Status": "0"}
            pics_urls = []
            for api_key, col_key in STATUS_COLUMN_MAPPING.items():
                val = item.get(api_key)
                if val is None:
                    row[col_key] = ""
                    continue
                if api_key == "PICS" and isinstance(val, list):
                    urls = [f"{PIC_URL_BASE}{pic['key']}"
                            for pic in val if isinstance(pic, dict) and pic.get("key")]
                    pics_urls.extend(urls)
                    row[col_key] = ", ".join(urls)
                elif api_key == "PICS_REP" and isinstance(val, str):
                    rep_urls = [u.strip() for u in val.split(",") if u.strip()]
                    pics_urls.extend(rep_urls)
                    row[col_key] = ", ".join(rep_urls)
                else:
                    row[col_key] = str(val) if val is not None else ""
            if pics_urls:
                row["PICS"] = ", ".join(pics_urls)
                row["#PICS"] = str(len(pics_urls))
            if row.get("REV"):
                # Keep the revision the server sent as provenance, then move any
                # old backlog revision to the active week's year.
                row["_original_rev"] = row["REV"]
                upgraded = self._backlog_target(row)
                if upgraded != row["REV"]:
                    upgraded_count += 1
                row["REV"] = upgraded
            self._rows_data.append(row)
            existing_ids.add(item_id)
            new_count += 1

        if self._auto_assign_on():
            self._apply_auto_assign_to(self._rows_data)
        self._rebuild_grid()
        self._save_queue()
        message = f"Fetched {new_count} new items (total: {len(self._rows_data)})"
        if upgraded_count:
            message += f" — {upgraded_count} year-upgraded"
        self.report_status(message, "success")
        self._download_images_async()

    # ---- send to SAP (async) --------------------------------------------------

    def _auto_upload_rows(self) -> list[dict]:
        """Rows the 2-minute auto upload may process, ignoring their status.

        Approved work (``USTATUS == "60RE"``), rows this week's register
        auto-assigned, and cancellation requests (which must never wait for a
        manual Send). Manual Send ignores this filter and takes every pending
        row.
        """
        return [r for r in self._rows_data if auto_upload_candidate(r)]

    def _send_to_sap(self, auto_mode: bool = False):
        if self._processing:
            return
        if getattr(self, "_fetching", False):
            self.report_status("Still fetching — wait for it to finish", "warning")
            return
        rows = self._auto_upload_rows() if auto_mode else self._rows_data
        pending = [r for r in rows
                   if str(r.get("Status", "0")) in ITERATED_STATUSES]
        if not pending:
            self.report_status("No pending items to process", "info")
            return

        self._start_processing(pending)

    def _reset_failed(self):
        changed = 0
        for r in self._rows_data:
            if r.get("Status") == "3":
                r["Status"] = "0"
                r["Bericht"] = ""
                clear_failure(r)
                changed += 1
        if changed:
            self._rebuild_grid()
            self._save_queue()
            self.report_status(f"Reset {changed} failed items to Pending", "info")
        else:
            self.report_status("No failed items to reset", "info")

    def _start_processing(self, items: list[dict]):
        self._cancel_progress_reset()
        self._processing = True
        self._stop_requested = False
        self._pending_queue = list(items)
        self._total = len(items)
        self._done_count = 0

        self._send_btn = self._find_send_btn()
        if self._send_btn:
            self._send_btn.config(text="\u25a0 Stop", state="normal")
        fetch = getattr(self, "_fetch_btn", None)
        if fetch is not None:
            try:
                fetch.config(state="disabled")
            except tk.TclError:
                pass

        self._prog["maximum"] = self._total
        self._prog["value"] = 0
        self._prog_label.config(text=f"0 / {self._total}")
        self.report_status(f"Processing 0 / {self._total} ...", "info")

        self._process_next()

    def _find_send_btn(self):
        for child in self.winfo_children():
            btn = self._find_widget_by_id(child, "si_send_btn")
            if btn:
                return btn
        return None

    def _find_widget_by_id(self, widget, target_id):
        if hasattr(widget, "button_id") and widget.button_id == target_id:  # type: ignore[attr-defined]
            return widget
        for ch in getattr(widget, "winfo_children", lambda: [])():
            result = self._find_widget_by_id(ch, target_id)
            if result:
                return result
        return None

    def _process_next(self):
        if self._stop_requested:
            return self._finish_processing()

        if not self._pending_queue:
            return self._finish_processing()

        row = self._pending_queue.pop(0)
        # The status the row was queued as decides the path: a cancel request
        # never reaches SAP (see docs/decisions/0005-upload-flag-semantics.md).
        status_val = str(row.get("Status", "0"))
        row["Status"] = "1"
        self._update_row(row)
        self.report_status(f"Processing ({self._done_count + 1}/{self._total})...", "info")

        row_id = row.get("_id", "")
        table_id = row.get("tableId", "")
        wo = row.get("WO", "")

        if status_val in CANCEL_STATUSES:
            self._process_cancel(row)
            self._on_row_done(row)
            return

        # Preflight: fail fast if required fields missing (no SAP roundtrip)
        if not (row.get("FLOC", "").strip() and row.get("STEXT", "").strip()):
            row["Status"] = "3"
            row["Bericht"] = "Missing FLOC/STEXT"
            self._on_row_done(row)
            return

        if row_id and table_id and not self._fake_sap() and status_val != "9":
            flag = self._api.get_flag(row_id)
            if flag == -1:
                row["Status"] = "8"
                row["Bericht"] = "API error (getFlag)"
                self._on_row_done(row)
                return
            if flag == 1:
                row["Status"] = "7"
                self._on_row_done(row)
                return

        self.engine.set_variable("si_floc", row.get("FLOC", ""))
        self.engine.set_variable("si_wp", row.get("WP", ""))
        self.engine.set_variable("si_pg", row.get("PG", ""))
        self.engine.set_variable("si_stext", row.get("STEXT", ""))
        self.engine.set_variable("si_ltext", row.get("LTEXT", ""))
        self.engine.set_variable("si_prior", row.get("PRIOR", ""))
        self.engine.set_variable("si_user", row.get("USER", ""))
        self.engine.set_variable("si_wo_type", row.get("WO_TYPE", ""))
        self.engine.set_variable("si_wo_key", row.get("WO_KEY", ""))
        self.engine.set_variable("si_rev", row.get("REV", ""))
        self.engine.set_variable("si_ustatus", row.get("USTATUS", ""))
        self.engine.set_variable("si_wo_text", self._combined_text(row))
        self.engine.set_variable("si_pics", row.get("PICS", ""))
        self.engine.set_variable("si_wo", wo)
        self.engine.set_variable("si_id", row_id)
        self.engine.set_variable("si_table_id", table_id)
        self.engine.set_variable("si_status", "")
        self.engine.set_variable("si_notif_number", "")
        self.engine.set_variable("si_sbar_text", "")
        self.engine.set_variable("si_sbar_type", "")

        if wo == "0":
            self.run_script("si_create_notification",
                            on_success=lambda r=row: self._on_notification_created(r),
                            on_error=self._error_handler(row))
        else:
            self.run_script("si_create_workorder",
                            on_success=lambda r=row: self._on_workorder_created(r),
                            on_error=self._error_handler(row))

    @staticmethod
    def _combined_text(row: dict) -> str:
        """STEXT + CR + LTEXT -- what Excel writes into the order text field."""
        stext = row.get("STEXT", "")
        ltext = row.get("LTEXT", "")
        return f"{stext}\r{ltext}" if ltext else stext

    def _on_notification_created(self, row: dict):
        # Pictures attach before the save (Excel order), then save.
        self._upload_pics_for_row(row, is_notification=True)
        self.run_script("si_save_notification",
                        on_success=lambda r=row: self._on_notification_done(r),
                        on_error=self._error_handler(row))

    def _on_workorder_created(self, row: dict):
        self._upload_pics_for_row(row, is_notification=False)
        self.run_script("si_finalize_workorder",
                        on_success=lambda r=row: self._on_workorder_done(r),
                        on_error=self._error_handler(row))

    def _error_handler(self, row: dict):
        def handler(script_name: str, message: str, line_no, raw: str):
            self._on_upload_error(row, script_name, message, line_no, raw)
        return handler

    def _on_upload_error(self, row: dict, script_name: str, message: str,
                         line_no, raw: str):
        """A failing SAP line: keep the message, script, line and its text."""
        record_failure(row, script_name, message, line_no, raw)
        self._on_row_done(row)

    def _on_notification_done(self, row: dict):
        self._finish_upload(row, "si_save_notification", "notification")

    def _on_workorder_done(self, row: dict):
        self._finish_upload(row, "si_finalize_workorder", "workorder")

    def _finish_upload(self, row: dict, script: str, kind: str):
        """Read SAP's status-bar reply: success/failure and the created number."""
        text = self.engine.get_variable("si_sbar_text") or ""
        sbar_type = self.engine.get_variable("si_sbar_type") or ""
        if not is_success(sbar_type, text):
            record_failure(row, script, text or "SAP save failed", None, "")
            self._on_row_done(row)
            return
        clear_failure(row)
        row["SAP_NUMBER"] = parse_created_number(text)
        row["SAP_KIND"] = kind
        row["Bericht"] = text or (
            "WO created" if kind == "workorder" else "Notification created")
        flagged = self._flag_on_server(row)
        row["Status"] = "6" if flagged else "2"
        self._on_row_done(row)

    def _on_row_done(self, row: dict):
        self._update_row(row)
        self._save_queue()
        self._done_count += 1
        self._prog["value"] = self._done_count
        self._prog_label.config(text=f"{self._done_count} / {self._total}")
        self.after(30, self._process_next)

    def _finish_processing(self):
        self._processing = False
        self._stop_requested = False
        if self._send_btn:
            self._send_btn.config(text="\u25b6 Send to SAP", state="normal")
        fetch = getattr(self, "_fetch_btn", None)
        if fetch is not None:
            try:
                fetch.config(state="normal")
            except tk.TclError:
                pass

        done = sum(1 for r in self._rows_data
                   if r.get("Status") in FINISHED_STATUSES)
        failed = sum(1 for r in self._rows_data if r.get("Status") in FAILED_STATUSES)
        if failed:
            self.report_status(f"{done} done, {failed} failed -- use \u21ba Reset Failed to retry",
                               "warning")
        else:
            self.report_status(f"All {done} items processed", "success")

        # Keep the finished bar visible for five seconds; the next run starts
        # from zero over its own row set.
        self._prog["value"] = self._done_count
        self._prog["maximum"] = self._total or 1
        self._prog_label.config(text=f"{self._done_count} / {self._total}")
        self._schedule_progress_reset()

    def _schedule_progress_reset(self):
        self._cancel_progress_reset()
        self._progress_reset_id = self.after(5000, self._reset_progress)

    def _cancel_progress_reset(self):
        if self._progress_reset_id:
            try:
                self.after_cancel(self._progress_reset_id)
            except Exception:
                pass
            self._progress_reset_id = None

    def _reset_progress(self):
        self._progress_reset_id = None
        self._update_progress()

    def _upload_pics_for_row(self, row: dict, is_notification: bool):
        upload_pics = self.engine.get_variable("si_upload_pics") or "NO"
        pics_str = row.get("PICS", "")
        if upload_pics.upper() != "YES" or not pics_str:
            return
        script = "si_upload_pic_notification" if is_notification else "si_upload_pic"
        for url in (u.strip() for u in pics_str.split(",") if u.strip()):
            filename = url.rstrip("/").split("/")[-1]
            name_no_ext = filename.rsplit(".", 1)[0] if "." in filename else filename
            filepath = os.path.join(PICS_FOLDER, filename)
            if not os.path.exists(filepath):
                self._api.download(url, filepath)
            self.engine.set_variable("si_pic_name", name_no_ext)
            self.engine.set_variable("si_pic_folder", PICS_FOLDER)
            self.engine.set_variable("si_pic_filename", filename)
            self.engine.set_variable("si_pic_uploaded", "")
            self.run_script(script)

    def _process_cancel(self, row: dict):
        """A cancel request never reaches SAP: flag the server, then mark 10.

        If the flag API fails the row becomes 2 ("OK, ERR reply") instead, so
        the cancellation is visible and the row can reappear for a retry.
        """
        row["Bericht"] = "Geannuleerd"
        if not self._flag_on_server(row):
            row["Status"] = "2"  # Geannuleerd || Flag API failed
        else:
            row["Status"] = CANCELLED

    # ---- edit dialog ----------------------------------------------------------

    def _edit_row(self, row: dict):
        def on_save():
            if self._auto_assign_on():
                self._apply_auto_assign_to([row])
            self._update_row(row)
            self._save_queue()
        EditRowDialog(
            self, row, on_save=on_save,
            rev_options=self._get_rev_options(),
            dropdowns=self._dropdown_options(),
            wo_tree=load_wo_tree(),
            on_open_in_sap=lambda r=row: self._open_in_sap(r),
        )

    def _open_in_sap(self, row: dict):
        """Open the created document in SAP (IW22 notification / IW32 order)."""
        number = str(row.get("SAP_NUMBER", "")).strip()
        if not number:
            self.report_status("No SAP number on this row yet — nothing to open", "warning")
            return
        self.engine.set_variable("si_sap_number", number)
        self.engine.set_variable("si_sap_kind", str(row.get("SAP_KIND", "")))
        self.run_script("si_open_in_sap")

    def _on_cell_dropdown(self, row: dict, column: str, value: str):
        """A constrained cell's in-cell picker chose `value`."""
        if column == "REV":
            self._set_row_rev(row, value)
            return
        if str(row.get(column, "")) == value:
            return
        row[column] = value
        self._update_row(row)
        self._save_queue()
        self.report_status(f"{column} set to {value}", "info")

    def _dropdown_options(self) -> dict[str, list[str]]:
        """In-cell picker lists: the hierarchy for WO_TYPE/WO_KEY, the register
        for REV, the JSON for the rest."""
        options = dropdown_lists(self._get_rev_options())
        # A blank first entry lets REV be cleared to "no revision"; it must not
        # leak into the register used for auto-assign matching.
        options["REV"] = [""] + [rev for rev in options.get("REV", []) if rev]
        return options

    def _open_options_popup(self, key: str | None = None):
        OptionsPopup(self, key=key, on_close=self._refresh_dropdowns)

    def _open_wo_type_popup(self):
        from .wo_type_popup import WoTypePopup
        WoTypePopup(self, on_close=self._refresh_dropdowns)

    def _refresh_dropdowns(self):
        if hasattr(self, "_grid"):
            self._grid.set_dropdowns(self._dropdown_options())
            self._grid.set_wo_key_map(load_wo_tree())

    def _set_row_rev(self, row: dict, value: str):
        self._apply_rev_to_row(row, value)
        self._update_row(row)
        self._save_queue()
        self.report_status(f"REV set to {value}", "info")

    @staticmethod
    def _apply_rev_to_row(row: dict, value: str):
        if row.get("REV") != value:
            # A deliberate pick is left alone by auto-assign; the revision the
            # server sent stays recoverable for "restore original".
            row["_auto_assigned"] = False
            row["_auto_assign_skip"] = True
        row["REV"] = value

    def _show_error_detail(self, row: dict):
        """A small read-only window naming the failing SAP line."""
        show_failure_dialog(self, row)

    def _restore_original_rev(self, row: dict):
        if not row.get("_original_rev"):
            self.report_status("No original revision recorded for this row", "warning")
            return
        # Restores this year's backlog revision, not the server's raw (possibly
        # last-year) code; that stays visible as the arrow's right-hand side.
        target = self._backlog_target(row)
        row["REV"] = target
        row["_auto_assigned"] = False
        row["_auto_assign_skip"] = True
        self._update_row(row)
        self._save_queue()
        self.report_status(f"REV restored to {target}", "info")

    def _reset_rev_to_default(self, row: dict):
        """Drop the manual REV override so the Auto-assign toggle governs again.

        With Auto-assign ON the row takes the register revision (and becomes
        auto-upload eligible); with it OFF the row returns to its backlog
        revision and is not auto-uploaded. Either way it is no longer
        "overridden", so a later toggle of the checkbox reaches it.
        """
        if str(row.get("Status", "0")) not in _ASSIGNABLE_STATUSES:
            self.report_status("Row already handled by SAP — revision not changed",
                               "warning")
            return
        row["_auto_assign_skip"] = False
        row["_auto_assigned"] = False
        if self._auto_assign_on():
            self._apply_auto_assign_to([row])
        else:
            row["REV"] = self._backlog_target(row)
        self._update_row(row)
        self._save_queue()
        self.report_status("Revision reset to the Auto-assign default", "info")

    # ---- right-click row actions -------------------------------------------

    def _show_row_menu(self, row_dict: dict, event):
        # A multi-row selection gets the bulk menu instead of the single-row
        # one, so a right-click can't quietly act on just the clicked row.
        selected = self._selected_rows()
        if len(selected) > 1 and any(row is row_dict for row in selected):
            self._show_bulk_menu(selected, event)
            return
        menu = tk.Menu(self, tearoff=0, bg=ui_styles.COLORS["surface"],
                       fg=ui_styles.COLORS["text"],
                       activebackground=ui_styles.COLORS["primary"],
                       activeforeground="#ffffff")
        status = row_dict.get("Status", "0")
        if has_failure(row_dict):
            menu.add_command(
                label="\u26a0 Show error detail",
                command=lambda r=row_dict: self._show_error_detail(r))
            menu.add_separator()
        if row_dict.get("_auto_assigned"):
            # Currently on the register revision: offer the way back.
            menu.add_command(
                label=f"\u21ba Restore backlog revision ({self._backlog_target(row_dict)})",
                command=lambda r=row_dict: self._restore_original_rev(r))
            menu.add_separator()
        elif row_dict.get("_auto_assign_skip") and str(status) in _ASSIGNABLE_STATUSES:
            # Overridden by hand: let the Auto-assign toggle decide again.
            menu.add_command(
                label="\u21ba Reset revision (Auto-assign default)",
                command=lambda r=row_dict: self._reset_rev_to_default(r))
            menu.add_separator()
        # Quick status actions: Cancel / Force Upload / Pending. Each is hidden
        # for the status(es) it would produce, so the other two show for a
        # matching row and all three show for any other status (e.g. Error).
        quick = (
            ("\u2716 Cancel Order (te annuleren)", self._cancel_row,
             {CANCEL_REQUESTED, CANCELLED}),
            ("\u26a1 Force Upload (skip server flag check)", self._force_upload,
             {"9"}),
            ("\u21ba Set Pending", self._set_pending, {"0"}),
        )
        for label, action, hidden_when in quick:
            if status not in hidden_when:
                menu.add_command(label=label,
                                 command=lambda r=row_dict, a=action: a(r))
        menu.add_separator()
        # full status control submenu
        status_menu = tk.Menu(menu, tearoff=0, bg=ui_styles.COLORS["surface"],
                              fg=ui_styles.COLORS["text"],
                              activebackground=ui_styles.COLORS["primary"],
                              activeforeground="#ffffff")
        for code, st in STATUSES.items():
            status_menu.add_command(
                label=f"{code} · {st.label}" + ("  ✓" if code == status else ""),
                command=lambda r=row_dict, c=code, lb=st.label: self._set_status(r, c, lb))
        menu.add_cascade(label="Set Status →", menu=status_menu)
        menu.post(event.x_root, event.y_root)

    def _show_bulk_menu(self, rows: list[dict], event):
        """Context menu for a multi-row selection: Status / WP / Ustatus / REV."""
        menu = tk.Menu(self, tearoff=0, bg=ui_styles.COLORS["surface"],
                       fg=ui_styles.COLORS["text"],
                       activebackground=ui_styles.COLORS["primary"],
                       activeforeground="#ffffff")
        menu.add_command(label=f"Bulk edit {len(rows)} row(s)", state="disabled")
        menu.add_separator()

        options = load_options()
        menu.add_cascade(label="Status →", menu=self._bulk_submenu(
            menu, [(f"{code} · {st.label}", code) for code, st in STATUSES.items()],
            "Status"))
        menu.add_cascade(label="Work center (WP) →", menu=self._bulk_submenu(
            menu, [(v, v) for v in options.get("WP", [])], "WP"))
        menu.add_cascade(label="User status (USTATUS) →", menu=self._bulk_submenu(
            menu, [(v, v) for v in options.get("USTATUS", [])], "USTATUS"))

        menu.add_command(label="Revisie… (free text)",
                         command=lambda r=rows: self._bulk_rev(r))
        menu.add_separator()
        menu.add_command(label="Clear selection", command=self._grid.clear_selection)
        menu.post(event.x_root, event.y_root)

    def _bulk_submenu(self, parent, choices: list[tuple[str, str]], column: str) -> tk.Menu:
        """One 'apply this value to the selection' submenu."""
        sub = tk.Menu(parent, tearoff=0, bg=ui_styles.COLORS["surface"],
                      fg=ui_styles.COLORS["text"],
                      activebackground=ui_styles.COLORS["primary"],
                      activeforeground="#ffffff")
        for label, value in choices:
            sub.add_command(
                label=label,
                command=lambda v=value: self._bulk_set(
                    column, v, self._selected_rows(), label))
        return sub

    def _cancel_row(self, row: dict):
        # Just a request: the row is iterated later, which flags the server and
        # marks it 10 — nothing is sent to SAP (see
        # docs/decisions/0005-upload-flag-semantics.md).
        if self._processing:
            self.report_status("Stop processing first", "warning")
            return
        row_id = row.get("_id", "")
        row["Status"] = CANCEL_REQUESTED
        row["Bericht"] = ""
        clear_failure(row)
        self._update_row(row)
        self._save_queue()
        self._update_progress()
        self.report_status(f"Item {row_id[:12]} — cancellation requested", "warning")

    def _force_upload(self, row: dict):
        row["Status"] = "9"
        self._update_row(row)
        self._save_queue()
        self._update_progress()
        self.report_status("Item set to Force Upload (9) — will skip server flag check", "info")

    def _set_pending(self, row: dict):
        self._set_status(row, "0", "Pending")

    def _set_status(self, row: dict, code: str, label: str = ""):
        self._apply_status_to_row(row, code)
        self._update_row(row)
        self._save_queue()
        self._update_progress()
        self.report_status(f"Status → {code} · {label or code}", "info")

    @staticmethod
    def _apply_status_to_row(row: dict, code: str):
        row["Status"] = code
        # Going back to pending / cancel-request / force clears the message.
        if code in ("0", CANCEL_REQUESTED, "9"):
            row["Bericht"] = ""
            clear_failure(row)

    # ---- bulk edit --------------------------------------------------------

    def _on_selection_changed(self):
        """Grid selection changed: refresh the 'N selected' hint."""
        label = getattr(self, "_sel_label", None)
        if label is None or not hasattr(self, "_grid"):
            return
        count = len(self._grid.selected_rows())
        label.config(text=f"{count} selected" if count else "")

    def _selected_rows(self) -> list[dict]:
        return self._grid.selected_rows() if hasattr(self, "_grid") else []

    def _bulk_set(self, column: str, value: str, rows: list[dict], label: str = ""):
        """Apply one value to every selected row.

        Status and REV keep their single-row rules (message clearing, and the
        auto-assign skip that lets a hand-typed revision stick); WP / Ustatus
        are plain fields.
        """
        if not rows:
            return
        for row in rows:
            if column == "Status":
                self._apply_status_to_row(row, value)
            elif column == "REV":
                self._apply_rev_to_row(row, value)
            else:
                row[column] = value
            self._update_row(row)
        self._save_queue()
        self._update_progress()
        self._on_selection_changed()
        self.report_status(f"{label or column} → {value} on {len(rows)} row(s)", "info")

    def _bulk_rev(self, rows: list[dict]):
        """REV bulk is free text (any custom revision), with the active week's
        register offered as suggestions."""
        value = ask_free_text(
            self, "Bulk revision",
            f"Revision for {len(rows)} selected row(s):",
            initial=str(rows[0].get("REV", "")) if rows else "",
            suggestions=self._get_rev_options())
        if value:
            self._bulk_set("REV", value, rows, "Revisie")

    def _reset_one(self, row: dict):
        # Local-only: the API has no unflag endpoint, so a line that was already
        # marked uploaded is re-sent through Force Upload (see
        # docs/decisions/0005-upload-flag-semantics.md) instead of being cleared
        # on the server.
        clear_failure(row)
        if row.get("_id"):
            row["Status"] = "9"
            row["Bericht"] = ""
            self._update_row(row)
            self._save_queue()
            self._update_progress()
            self.report_status("Undo is local — item set to Force Upload (9)", "warning")
            return
        row["Status"] = "0"
        row["Bericht"] = ""
        self._update_row(row)
        self._save_queue()
        self._update_progress()
        self.report_status("Item reset to Pending", "info")

    def _edit_script(self, button_id: str):
        """Open EditModal for a SAP GUI script config."""
        from ui.edit_modal import EditModal

        data = self.config_data.get("customcomponents", {}).get("buttons", {}).get(button_id, {})
        if not data:
            self.report_status(f"No config for {button_id}", "warning")
            return

        def save_callback(new_data: dict):
            self.config_data["customcomponents"]["buttons"][button_id].update(new_data)
            cfg.save_config(self.config_data)
            self.report_status(f"Saved {button_id} config", "success")

        EditModal(self, "buttons", data, save_callback, title=button_id)

    def _flag_on_server(self, row: dict) -> bool:
        """Mark the row uploaded on the server. False when the API call failed.

        Fake SAP never sets the flag: an emulator run must not mark a row
        uploaded on the real server.
        """
        if self._fake_sap():
            self._save_queue()
            return True
        ok = True
        if row.get("_id"):
            ok = self._api.flag_added(row["_id"], "WO") and ok
        if row.get("PREV_ORDER_ID"):
            ok = self._api.flag_added(row["PREV_ORDER_ID"], "PO") and ok
        if not ok:
            existing = row.get("Bericht", "")
            row["Bericht"] = (existing + " || " if existing else "") + "Flag API failed"
            self.report_status(
                f"Flag API failed for {str(row.get('_id', ''))[:12]} — row needs attention",
                "warning")
        self._save_queue()
        return ok

    # ---- image downloads ------------------------------------------------------

    def _image_download_jobs(self):
        """(jobs, had_any_url) for cache misses and corrupt cache entries."""
        urls_seen = set()
        jobs = []
        for row in self._rows_data:
            for url in (u.strip() for u in row.get("PICS", "").split(",") if u.strip()):
                if url in urls_seen:
                    continue
                urls_seen.add(url)
                filepath = os.path.join(PICS_FOLDER, url.rstrip("/").split("/")[-1])
                # A file that exists but won't decode is a stale/corrupt cache,
                # so it is re-fetched rather than shown as an unreadable image.
                if is_image_file(filepath):
                    continue
                jobs.append((url, filepath))
        return jobs, bool(urls_seen)

    def _report_download_result(self, ok: int, failed: int, had_urls: bool):
        if not had_urls:
            self.report_status("No image URLs in fetched data", "info")
        elif failed:
            self.report_status(f"Downloaded {ok} image(s), {failed} failed", "warning")
        else:
            self.report_status(f"Downloaded {ok} image(s)", "info")

    def _download_images(self):
        """Synchronous download (explicit callers and tests)."""
        jobs, had_urls = self._image_download_jobs()
        ok = failed = 0
        for url, filepath in jobs:
            if self._api.download(url, filepath):
                ok += 1
            else:
                failed += 1
        self._report_download_result(ok, failed, had_urls)

    def _download_images_async(self):
        """Download after a fetch without holding up the Tk event loop."""
        if getattr(self, "_image_download_running", False):
            return
        jobs, had_urls = self._image_download_jobs()
        if not jobs:
            self._report_download_result(0, 0, had_urls)
            return
        self._image_download_running = True
        results: queue.Queue = queue.Queue()

        def worker():
            ok = failed = 0
            for url, filepath in jobs:
                try:
                    if self._api.download(url, filepath):
                        ok += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
            results.put((ok, failed, had_urls))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_image_downloads(results)

    def _poll_image_downloads(self, results):
        if not self.winfo_exists():
            return
        try:
            ok, failed, had_urls = results.get_nowait()
        except queue.Empty:
            self.after(150, lambda: self._poll_image_downloads(results))
            return
        self._image_download_running = False
        self._report_download_result(ok, failed, had_urls)

    # ---- clear ----------------------------------------------------------------

    def _clear_completed(self):
        before = len(self._rows_data)
        self._rows_data = [
            r for r in self._rows_data
            if r.get("Status") in ("0", "3", CANCEL_REQUESTED, "9")
        ]
        self._rebuild_grid()
        self._save_queue()
        cleared = before - len(self._rows_data)
        self.report_status(f"Cleared {cleared} completed items", "info")

    def _clear_list(self):
        if self._processing:
            self.report_status("Stop processing first", "warning")
            return
        if not self._rows_data:
            self.report_status("The list is already empty", "info")
            return
        from tkinter import messagebox
        count = len(self._rows_data)
        if not messagebox.askyesno(
            "Clear list", f"Remove all {count} rows from the Import list?", parent=self
        ):
            return
        self._rows_data = []
        self._rebuild_grid()
        self._save_queue()
        self.report_status(f"Cleared {count} rows", "info")

    # ---- auto-assign revisions ------------------------------------------------

    def _backlog_target(self, row: dict) -> str:
        """The row's backlog revision moved to the active week's year.

        Built from the revision the server sent, so un-assigning a row and
        restoring it both land on this year's backlog revision, never last
        year's. Non-backlog revisions come back unchanged.
        """
        from .revisions import parse_week_key, upgrade_backlog_year
        year = parse_week_key(self._active_week_key())[0]
        return upgrade_backlog_year(row.get("_original_rev") or row.get("REV", ""),
                                    year)

    def _auto_assign_on(self) -> bool:
        return getattr(self, "_auto_assign_enabled", False)

    def _toggle_auto_assign(self):
        self._set_auto_assign(bool(self._assign_var.get()))

    def _set_auto_assign(self, enabled: bool):
        self._auto_assign_enabled = bool(enabled)
        if hasattr(self, "_assign_var"):
            self._assign_var.set(self._auto_assign_enabled)
        if not self._auto_assign_enabled:
            changed = self._revert_auto_assign(getattr(self, "_rows_data", []))
            if changed:
                self._rebuild_grid()
                self._save_queue()
                self.report_status(f"Auto-assign OFF — {changed} row(s) restored", "info")
            else:
                self.report_status("Auto-assign OFF", "info")
            return
        changed = self._apply_auto_assign_to(self._rows_data)
        self._rebuild_grid()
        self._save_queue()
        self.report_status(f"Auto-assign ON — {changed} row(s) assigned", "info")

    def _revert_auto_assign(self, rows: list[dict]) -> int:
        """Un-assign rows whose revision SAP has not been told about yet.

        Completed/processing/already-added/cancelled-on-server rows keep the
        revision they were uploaded with; their ``current ← original`` arrow is
        history, not a pending transfer.
        """
        changed = 0
        for row in rows:
            if not row.get("_auto_assigned"):
                continue
            if str(row.get("Status", "0")) not in _ASSIGNABLE_STATUSES:
                continue
            target = self._backlog_target(row)
            if row.get("REV") != target:
                row["REV"] = target
                changed += 1
            row["_auto_assigned"] = False
        return changed

    def _apply_auto_assign_to(self, rows: list[dict]) -> int:
        """Rewrite matching production-line backlog rows in place.

        The revision the server sent stays as ``_original_rev`` so the grid and
        the modal can show both and the user can restore it. Rows a user has set
        or restored by hand carry ``_auto_assign_skip``, and rows SAP has already
        been told about (status not in ``_ASSIGNABLE_STATUSES``) keep the
        revision they were uploaded with; both are left alone. Returns how many
        rows changed.
        """
        from .revisions import match_downtime_revision

        register = self._get_rev_options()
        changed = 0
        for row in rows:
            if row.get("_auto_assign_skip"):
                continue
            if str(row.get("Status", "0")) not in _ASSIGNABLE_STATUSES:
                continue
            source = self._backlog_target(row)
            matched = match_downtime_revision(source, register)
            if matched is not None:
                if row.get("REV") != matched or not row.get("_auto_assigned"):
                    if not row.get("_original_rev"):
                        row["_original_rev"] = row.get("REV", "")
                    row["REV"] = matched
                    row["_auto_assigned"] = True
                    changed += 1
            elif row.get("_auto_assigned"):
                # The register no longer covers this line: go back to this
                # year's backlog revision (an auto-assignment is never stale).
                if row.get("REV") != source:
                    row["REV"] = source
                    changed += 1
                row["_auto_assigned"] = False
        return changed

    # ---- auto timer -----------------------------------------------------------

    def _cycle_auto_upload(self):
        """The window's Auto upload marker: OFF -> ON -> PAUSED -> ON."""
        if not self._auto_enabled:
            self._toggle_auto()
        else:
            self.toggle_auto_upload_pause()

    def _toggle_auto(self):
        if self._auto_enabled:
            self._stop_auto_upload()
            self.report_status("Auto upload OFF", "info")
        else:
            self._auto_enabled = True
            self._auto_paused = False
            self.report_status("Auto upload ON (every 120s)", "info")
            self._auto_tick()
        self._sync_auto_switch()

    def auto_upload_state(self) -> str:
        """For the toolbar marker: "off", "running" or "paused"."""
        if not self._auto_enabled:
            return "off"
        return "paused" if self._auto_paused else "running"

    def toggle_auto_upload_pause(self):
        """Pause/resume the auto-upload timer (window button or toolbar marker)."""
        if not self._auto_enabled:
            return
        if self._auto_paused:
            self._auto_paused = False
            self.report_status("Auto upload resumed", "info")
            self._auto_tick()
        else:
            self._auto_paused = True
            if self._auto_id:
                self.after_cancel(self._auto_id)
                self._auto_id = None
            self.report_status("Auto upload paused — SAP left alone", "warning")
        self._sync_auto_switch()

    def _auto_tick(self):
        if not self._auto_enabled or self._auto_paused:
            return
        was_processing = self._processing
        self._fetch_new()
        if not was_processing:
            if getattr(self, "_fetching", False):
                # Fetch is asynchronous now: upload only after it lands.
                self._wait_then_send()
            else:
                self._send_to_sap(auto_mode=True)
        self._auto_id = self.after(120000, self._auto_tick)

    def _wait_then_send(self):
        if not self.winfo_exists():
            return
        if getattr(self, "_fetching", False):
            self.after(200, self._wait_then_send)
            return
        if not self._processing:
            self._send_to_sap(auto_mode=True)

    def destroy(self):
        # Closing the window stops auto upload; nothing survives a restart.
        if getattr(self, "_fetching", False):
            self._fetching = False
            counter = getattr(self, "_counter", None)
            if counter is not None:
                try:
                    counter.end()
                except Exception:
                    pass
        self._persist_settings()
        self._stop_auto_upload()
        self._cancel_progress_reset()
        super().destroy()

    def _stop_auto_upload(self):
        self._auto_enabled = False
        self._auto_paused = False
        if self._auto_id:
            try:
                self.after_cancel(self._auto_id)
            except Exception:
                pass
            self._auto_id = None

    # ---- grid helpers ---------------------------------------------------------

    def _visible_rows(self) -> list[dict]:
        if hasattr(self, "_filter_var"):
            return [r for r in self._rows_data if self._row_matches_filter(r)]
        return list(self._rows_data)

    def _update_row(self, row: dict):
        # Updated in place when the row is on screen; any change to which rows
        # are shown (a filter flip, a new row) falls back to a full refresh.
        if not self._grid.update_row(row):
            self._rebuild_grid()

    def _rebuild_grid(self):
        self._grid.refresh(self._visible_rows())
        self._update_progress()

    def _update_progress(self):
        if self._processing:
            self._prog["maximum"] = self._total or 1
            self._prog["value"] = self._done_count
            self._prog_label.config(text=f"{self._done_count} / {self._total}")
            return

        total = len(self._rows_data)
        done = sum(1 for r in self._rows_data
                   if r.get("Status") in FINISHED_STATUSES)
        failed = sum(1 for r in self._rows_data if r.get("Status") in FAILED_STATUSES)
        filtered = 0
        if hasattr(self, "_filter_var"):
            filtered = sum(1 for r in self._rows_data if not self._row_matches_filter(r))
        self._prog["value"] = done
        self._prog["maximum"] = total or 1
        label = f"{done} / {total}"
        if failed:
            label += f" • {failed} failed"
        if filtered:
            label += f" • {filtered} filtered"
        if total == 0:
            label = "No imports — Fetch New"
        self._prog_label.config(text=label)
