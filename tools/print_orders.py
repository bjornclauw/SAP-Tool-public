"""PrintOrdersWindow — process work orders from IW38 or one open IW32 order.

Workflow:
  1. Open IW38, or open one order in IW32.
  2. "Load IW38 Orders" imports the open IW38 grid; "Process This Order"
     handles the order open in IW32 (or one typed into the Order field).
  3. Orders whose localised status tokens are not released are flagged
     "No Release"; unknown status values stay "Unknown".
  4. Process All / Process Selected / Retry Failed loop over released orders:
       a. Open IW32 with order
       b. Print work order as PDF via SAP GUI script
       c. Export attachment list from IHKD tab
       d. Download image files for that order
       e. Create filler PDF from downloaded images
  5. Each order becomes one combined PDF; single prints are kept per order and
     a batch produces one merged PDF. History lives in data/print_history.json.

SAP scripts are short operations chained via engine polling.
The batch loop and UI stay on the main thread; image/PDF composition runs on a
worker so a large attachment cannot freeze the window.
"""

from __future__ import annotations

import os
import queue
import re
import shutil
import threading
import tkinter as tk
from collections.abc import Callable
from datetime import datetime
from tkinter import messagebox, ttk

from pypdf import PdfReader, PdfWriter

from core import config as cfg
from core import i18n, sap_fields, user_settings
from tools.grid_column_manager import GridColumnManager as GridManager
from tools.print_history import PrintHistoryStore
from tools.print_history_view import PrintHistoryPane
from tools.print_order_assets import retain_cover_image
from tools.print_orders_packet import build_packet
from ui import ui_styles
from ui.custom_widgets import BaseToolWindow, CustomButton
from ui.edit_modal import EditModal
from ui.ui_styles import card_frame, styled_frame, styled_label


class _UserStatusPicker(tk.Toplevel):
    """Small dropdown of user-status checkboxes that stays open.

    ttk.Combobox is single-select and a menu of checkbuttons closes after each
    click, so the multi-select filter gets its own compact popup.
    """

    def __init__(self, parent, anchor, options: list[str], selected: set[str],
                 on_change: Callable[[set[str]], None],
                 on_close: Callable[[], None]):
        super().__init__(parent)
        self.withdraw()
        self.overrideredirect(True)
        self.transient(parent.winfo_toplevel())
        self.configure(bg=ui_styles.COLORS["border"])
        self._on_change = on_change
        self._on_close = on_close
        self._closed = False

        outer = tk.Frame(self, bg=ui_styles.COLORS["surface"])
        outer.pack(fill="both", expand=True, padx=1, pady=1)

        top = tk.Frame(outer, bg=ui_styles.COLORS["surface"])
        top.pack(fill="x", padx=8, pady=(6, 2))
        tk.Button(
            top, text=i18n.t("print_orders.all_statuses"), command=self._select_all,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text_secondary"],
            relief="flat", font=ui_styles.FONTS["small"], cursor="hand2",
            padx=4).pack(side="left")
        tk.Button(
            top, text=i18n.t("print_orders.done"), command=self._close,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["primary"],
            relief="flat", font=ui_styles.FONTS["small"], cursor="hand2",
            padx=4).pack(side="right")

        body = tk.Frame(outer, bg=ui_styles.COLORS["surface"])
        body.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self._canvas = tk.Canvas(
            body, bg=ui_styles.COLORS["surface"], highlightthickness=0,
            height=min(max(len(options), 1) * 24, 240))
        scroll = ttk.Scrollbar(body, orient="vertical",
                               command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scroll.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        if len(options) > 8:
            scroll.pack(side="right", fill="y")
        inner = tk.Frame(self._canvas, bg=ui_styles.COLORS["surface"])
        window = self._canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfigure(window, width=e.width))

        self._vars: dict[str, tk.BooleanVar] = {}
        for option in options:
            var = tk.BooleanVar(value=option in selected)
            tk.Checkbutton(
                inner, text=option, variable=var, anchor="w",
                bg=ui_styles.COLORS["surface"],
                activebackground=ui_styles.COLORS["surface"],
                selectcolor=ui_styles.COLORS["surface"],
                fg=ui_styles.COLORS["text"], font=ui_styles.FONTS["body"],
                relief="flat", bd=0, highlightthickness=0, padx=4, pady=2,
                cursor="hand2", command=self._emit,
            ).pack(fill="x")
            self._vars[option] = var
        if not options:
            tk.Label(
                inner, text=i18n.t("print_orders.no_user_status"),
                bg=ui_styles.COLORS["surface"],
                fg=ui_styles.COLORS["text_secondary"],
                font=ui_styles.FONTS["small"], anchor="w",
            ).pack(fill="x", padx=4, pady=4)

        self.bind("<Escape>", lambda e: self._close())
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<FocusOut>", lambda e: self.after(120, self._close_if_outside))

        self.update_idletasks()
        width = max(anchor.winfo_width(), 160)
        height = min(max(self.winfo_reqheight(), 80), 340)
        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() + anchor.winfo_height()
        if y + height > self.winfo_screenheight():
            y = anchor.winfo_rooty() - height
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.deiconify()
        self.lift()
        self.after(10, self.focus_force)

    def _on_wheel(self, event):
        self._canvas.yview_scroll(int(-event.delta / 120), "units")

    def _selected(self) -> set[str]:
        return {name for name, var in self._vars.items() if var.get()}

    def _emit(self):
        if not self._closed:
            self._on_change(self._selected())

    def _select_all(self):
        for var in self._vars.values():
            var.set(False)
        self._emit()

    def _close_if_outside(self):
        if not self.winfo_exists():
            return
        focus = self.focus_get()
        if focus is None or not str(focus).startswith(str(self)):
            self._close()

    def _close(self):
        if self._closed:
            return
        self._closed = True
        self._on_close()
        self.destroy()


class PrintOrdersWindow(BaseToolWindow):
    """Batch-print work orders from IW38 with progress tracking."""

    EXPORT_FILENAME = "sap_iw38_export.txt"

    STATUS_PENDING = "Pending"
    STATUS_IN_PROGRESS = "In Progress"
    STATUS_DONE = "Done"
    STATUS_FAILED = "Failed"
    STATUS_NO_RELEASE = "No Release"
    STATUS_UNKNOWN = "Unknown"

    STATUS_TAGS = {
        STATUS_PENDING: "po_pending",
        STATUS_IN_PROGRESS: "po_in_progress",
        STATUS_DONE: "po_done",
        STATUS_FAILED: "po_failed",
        STATUS_NO_RELEASE: "po_no_release",
        STATUS_UNKNOWN: "po_unknown",
    }

    STATUS_LABEL_KEYS = {
        STATUS_PENDING: "status.pending",
        STATUS_IN_PROGRESS: "status.in_progress",
        STATUS_DONE: "status.done",
        STATUS_FAILED: "status.failed",
        STATUS_NO_RELEASE: "status.no_release",
        STATUS_UNKNOWN: "status.unknown",
    }

    STEPS = ["open", "print", "attach", "spool"]
    MAX_ATTACHMENTS = 4
    RUN_LIMIT = 30

    def __init__(self, parent, engine):
        super().__init__(parent, engine, titlebar_color=ui_styles.TOOL_COLORS["print_orders"])
        self.title(i18n.t("print_orders.title"))
        self._fields = sap_fields.get_registry()
        self._column_map: sap_fields.ColumnMap | None = None
        self._order_missing = False
        self._load_warning = ""
        self.geometry("1060x760")
        self.minsize(720, 480)

        self._init_paths()
        self._load_po_settings()

        self._processing = False
        self._merging = False
        self._paused = False
        self._stop_requested = False
        self._loading = False
        self._done_count = 0
        self._failed_count = 0
        self._total = 0
        self._all_items: list[str] = []
        self._row_errors: dict[str, str] = {}
        self._run_orders: list[str] = []
        self._run_started = ""
        self._run_started_day = ""
        self._run_kind = "batch"
        self._active_stem = ""
        self._active_text = ""
        self._order_pdfs: dict[str, str] = {}
        self._order_results: dict[str, dict] = {}
        self._batch_dir = ""
        self._batch_file = ""
        self._batch_run_id = ""
        self._overwrite_single: dict | None = None
        self._merged_file: str | None = None
        self._merged_signature: tuple = ()
        self._merged_link: tk.Label | None = None
        self._history_pane: PrintHistoryPane | None = None
        self._history_visible = False
        self._mode = "list"

        self._build_actions()
        self._list_area = styled_frame(self)
        self._list_area.pack(fill="both", expand=True)
        self._build_filter_bar()
        self._build_status_bar()
        self._build_single_info()
        self._build_treeview()
        self._build_progress_bar()
        self._cache_scripts()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._show()
        self._refresh_action_state()
        self._detect_after_id = self.after(300, self._detect_active_screen)

    def destroy(self):
        trace = getattr(self, "_power_user_trace", None)
        power = getattr(self, "_power_user_var", None)
        if trace and power is not None:
            try:
                power.trace_remove("write", trace)
            except tk.TclError:
                pass
            self._power_user_trace = None
        after_id = getattr(self, "_detect_after_id", None)
        if isinstance(after_id, str):
            try:
                self.after_cancel(after_id)
            except tk.TclError:
                pass
            self._detect_after_id = None
        if getattr(self, "_user_status_popup", None) is not None:
            self._close_user_status_picker()
        if getattr(self, "_work_dir", ""):
            self._remove_work_dir()
        super().destroy()

    # ── paths / settings ────────────────────────────────────────────────────
    def _init_paths(self):
        base = os.path.dirname(os.path.dirname(__file__))
        self._temp_path = os.path.join(base, "tempexport")
        os.makedirs(self._temp_path, exist_ok=True)
        self._import_file = os.path.join(self._temp_path, self.EXPORT_FILENAME)

        self._output_root = os.path.join(base, "data", "print_output")
        os.makedirs(self._output_root, exist_ok=True)
        self._work_root = os.path.join(self._output_root, ".work")
        self._session_stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self._work_dir = os.path.join(self._work_root, self._session_stamp)
        os.makedirs(self._work_dir, exist_ok=True)

        escaped = self._work_dir.replace("\\", "\\\\")
        self.engine.set_variable("po_import_path", self._temp_path.replace("\\", "\\\\"))
        self.engine.set_variable("po_import_file", self.EXPORT_FILENAME)
        self.engine.set_variable("po_export_path", escaped)

        self._history = PrintHistoryStore(output_root=self._output_root)

    def _load_po_settings(self):
        data = user_settings.load_user_settings()
        self._stop_on_error_var = tk.BooleanVar(
            value=bool(data.get("po_stop_on_error", False)))
        try:
            max_attach = int(data.get("po_max_attachments", self.MAX_ATTACHMENTS))
        except (TypeError, ValueError):
            max_attach = self.MAX_ATTACHMENTS
        self._max_attach_var = tk.IntVar(value=max(1, min(6, max_attach)))
        layout = str(data.get("po_pdf_layout", "auto"))
        self._pdf_layout_var = tk.StringVar(
            value=layout if layout in ("auto", "1", "2", "4") else "auto")
        self._group_by_workcenter = bool(data.get("po_group_by_workcenter", False))
        self._group_by_workcenter_var = tk.BooleanVar(value=self._group_by_workcenter)
        overrides = data.get("po_team_colors", {})
        self._team_color_overrides = dict(overrides) if isinstance(overrides, dict) else {}

    def _save_po_settings(self):
        user_settings.update_user_settings({
            "po_stop_on_error": bool(self._stop_on_error_var.get()),
            "po_max_attachments": str(self._max_attach_var.get()),
            "po_pdf_layout": self._pdf_layout_var.get(),
            "po_group_by_workcenter": bool(self._group_by_workcenter),
            "po_team_colors": dict(self._team_color_overrides),
        })

    def _toggle_group_by_workcenter(self):
        self._group_by_workcenter = bool(self._group_by_workcenter_var.get())
        self._save_po_settings()
        self._refresh_action_state()

    def _edit_team_colors(self):
        teams = []
        for item in self._all_items:
            team = self._workcenter_for(item)
            if team and team not in teams:
                teams.append(team)
        if not teams:
            self.report_status(
                i18n.t("print_orders.no_team_colors", "No workcenters available to configure"),
                "warning")
            return
        from ui.team_color_dialog import TeamColorDialog

        TeamColorDialog(
            self, teams, self._team_color_overrides,
            on_save=self._apply_team_colors)

    def _apply_team_colors(self, overrides: dict):
        self._team_color_overrides = dict(overrides)
        self._save_po_settings()
        self._refresh_action_state()

    def _cache_scripts(self):
        """Read SAP scripts from config once, use many times."""
        self._scripts = {
            "open": self._script("po_open_order"),
            "print": self._script("po_print_pdf"),
            "attach": self._script("po_export_attachments"),
            "read_doknr": self._script("po_read_doknr"),
            "attach_export_one": self._script("po_export_one_attachment"),
            "spool": self._script("po_download_image"),
        }

    @staticmethod
    def _script(button_id: str) -> str:
        return cfg.get_button_cfg(cfg.load_config(), button_id).get("script", "")

    def _edit_script(self, button_id: str):
        config = cfg.load_config()
        data = cfg.get_button_cfg(config, button_id)

        def on_save(updated: dict):
            config["customcomponents"]["buttons"][button_id] = updated
            cfg.save_config(config)
            self._cache_scripts()

        EditModal(self, "buttons", data, on_save, title=button_id)

    # ── UI ──────────────────────────────────────────────────────────────────
    def _add_menu(self, parent: tk.Misc, text: str):
        mb = tk.Menubutton(
            parent, text=text, bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text"], activebackground=ui_styles.COLORS["primary"],
            activeforeground="#ffffff", relief="flat", bd=0,
            font=ui_styles.FONTS["body"], cursor="hand2", padx=8, pady=2,
        )
        menu = tk.Menu(mb, tearoff=0)
        mb.config(menu=menu)
        return mb, menu

    def _find_power_user_var(self) -> tk.BooleanVar | None:
        """The toolbar's Power User toggle, when this window has one."""
        power = getattr(self.master, "power_user", None)
        return power if isinstance(power, tk.BooleanVar) else None

    def _sync_scripts_menu(self):
        """Show the script editor menu only while Power User is on."""
        if not hasattr(self, "_scripts_mb"):
            return
        show = bool(self._power_user_var.get()) if self._power_user_var else False
        if show == getattr(self, "_scripts_visible", False):
            return
        if show:
            self._scripts_mb.pack(
                side="right", padx=(0, ui_styles.SPACING["tight"]))
        else:
            self._scripts_mb.pack_forget()
        self._scripts_visible = show

    def _build_actions(self):
        bar = styled_frame(self)
        bar.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                 pady=(ui_styles.SPACING["comfortable"], 0))
        self._actions_bar = bar
        self.order_var = tk.StringVar()

        self._load_btn = CustomButton(
            bar, "buttons", "po_export_iw38_btn",
            text=i18n.t("print_orders.load_orders"), command=self._run_export,
            bg=ui_styles.COLORS["primary"], fg="#ffffff",
        )
        self._load_btn.pack(side="left")

        self._single_btn = CustomButton(
            bar, "buttons", "po_print_open_order_btn",
            text=i18n.t("print_orders.process_this"), command=self._print_open_order,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"])
        # Shown only in single-order (IW32) mode; see _refresh_action_state.
        self._print_sel_btn = CustomButton(
            bar, "buttons", "po_print_selected_btn",
            text=i18n.t("print_orders.process_selected"), command=self._on_print_selected_clicked,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"])
        # Shown only while rows are selected; see _refresh_action_state.
        self._print_all_btn = CustomButton(
            bar, "buttons", "po_start_btn",
            text=i18n.t("print_orders.process_all"), command=self._on_print_all_clicked,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"])
        self._print_all_btn.pack(side="left", padx=(ui_styles.SPACING["tight"], 0))

        self._stop_btn = CustomButton(
            bar, "buttons", "po_stop_btn", text=i18n.t("print_orders.stop"),
            command=self._on_stop_clicked,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
            state="disabled")
        self._pause_btn = CustomButton(
            bar, "buttons", "po_pause_btn", text=i18n.t("print_orders.pause"),
            command=self._on_pause_clicked,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
            state="disabled")
        # Anchor for _set_left: re-packed left buttons land before this.
        self._actions_end = tk.Frame(bar, width=0, height=0)
        self._actions_end.pack(side="left")

        options_mb, options_menu = self._add_menu(
            bar, i18n.t("print_orders.options"))
        self._options_mb = options_mb
        options_mb.pack(side="right")
        self._scripts_mb, scripts_menu = self._add_menu(
            bar, i18n.t("print_orders.scripts"))
        self._scripts_visible = False
        self._power_user_var = self._find_power_user_var()
        self._power_user_trace = None
        if self._power_user_var is not None:
            self._power_user_trace = self._power_user_var.trace_add(
                "write", lambda *_: self._sync_scripts_menu())
        self._sync_scripts_menu()
        self._history_btn = CustomButton(
            bar, "buttons", "po_history_btn",
            text=f"{i18n.t('print_orders.history')} (0)",
            command=self._toggle_history,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"])
        self._history_btn.pack(side="right",
                               padx=(0, ui_styles.SPACING["tight"]))

        for button_id, key in [
            ("po_export_iw38_btn", "print_orders.load_orders"),
            ("po_read_tcode", "print_orders.detect_screen"),
            ("po_read_open_order", "print_orders.read_open_order"),
            ("po_open_order", "print_orders.open_iw32"),
            ("po_print_pdf", "print_orders.print_spool"),
            ("po_export_attachments", "print_orders.read_attachments"),
            ("po_read_doknr", "print_orders.read_doknr"),
            ("po_export_one_attachment", "print_orders.export_one_attachment"),
            ("po_download_image", "print_orders.sp02_pdf_cleanup"),
        ]:
            scripts_menu.add_command(
                label=i18n.t(key),
                command=lambda bid=button_id: self._edit_script(bid))

        options_menu.add_checkbutton(
            label=i18n.t("print_orders.stop_on_error"),
            variable=self._stop_on_error_var,
            command=self._save_po_settings)
        options_menu.add_checkbutton(
            label=i18n.t("print_orders.team_summary"),
            variable=self._group_by_workcenter_var,
            command=self._toggle_group_by_workcenter)
        options_menu.add_command(
            label=i18n.t("print_orders.team_colors"),
            command=self._edit_team_colors)
        options_menu.add_separator()
        max_menu = tk.Menu(options_menu, tearoff=0)
        for value in range(1, 7):
            max_menu.add_radiobutton(
                label=str(value), variable=self._max_attach_var, value=value,
                command=self._save_po_settings)
        options_menu.add_cascade(
            label=i18n.t("print_orders.max_attachments"), menu=max_menu)
        layout_menu = tk.Menu(options_menu, tearoff=0)
        for value, key in (("auto", "print_orders.auto"),
                           ("1", "print_orders.page_1"),
                           ("2", "print_orders.page_2"),
                           ("4", "print_orders.page_4")):
            layout_menu.add_radiobutton(
                label=i18n.t(key), variable=self._pdf_layout_var, value=value,
                command=self._save_po_settings)
        options_menu.add_cascade(
            label=i18n.t("print_orders.pdf_layout"), menu=layout_menu)
        options_menu.add_separator()
        options_menu.add_command(
            label=i18n.t("print_orders.open_output_folder"),
            command=self._open_output_folder)
        options_menu.add_command(
            label=i18n.t("print_orders.clear_list"), command=self._clear_list)

    def _build_filter_bar(self):
        bar = styled_frame(self._list_area)
        bar.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                 pady=(ui_styles.SPACING["tight"], 0))
        self._filter_bar = bar

        styled_label(bar, i18n.t("print_orders.filter"), font=ui_styles.FONTS["body"],
                     bg=ui_styles.COLORS["bg"]).pack(side="left")
        self._filter_var = tk.StringVar()
        self._filter_var.trace_add("write", lambda *_: self._apply_filter())
        self._filter_entry = tk.Entry(
            bar, textvariable=self._filter_var, width=20,
            font=ui_styles.FONTS["body"])
        self._filter_entry.pack(side="left", padx=(4, 8))

        styled_label(bar, i18n.t("print_orders.user_status"), font=ui_styles.FONTS["body"],
                     bg=ui_styles.COLORS["bg"]).pack(side="left")
        self._user_status_options: list[str] = []
        self._user_status_selected: set[str] = set()
        self._user_status_col_idx = -1
        self._user_status_popup: _UserStatusPicker | None = None
        self._user_status_btn = CustomButton(
            bar, "buttons", "po_user_status_btn",
            text=i18n.t("print_orders.all_statuses"),
            command=self._open_user_status_picker,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"])
        self._user_status_btn.pack(side="left", padx=(4, 4))

        self._clear_filter_btn = tk.Button(
            bar, text="\u2715", command=self._clear_filter,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text_secondary"],
            relief="flat", font=ui_styles.FONTS["small"], padx=6,
            cursor="hand2")
        self._clear_filter_btn.pack(side="left")

        self._counter_labels: dict[str, tk.Label] = {}
        for status, color in (
            (self.STATUS_PENDING, ui_styles.COLORS["text"]),
            (self.STATUS_NO_RELEASE, ui_styles.COLORS["text_secondary"]),
            (self.STATUS_UNKNOWN, ui_styles.COLORS["text_secondary"]),
            (self.STATUS_DONE, ui_styles.COLORS["success"]),
            (self.STATUS_FAILED, ui_styles.COLORS["error"]),
        ):
            label = styled_label(
                bar, f"{i18n.t(self.STATUS_LABEL_KEYS[status])} 0",
                font=ui_styles.FONTS["small"],
                bg=ui_styles.COLORS["bg"], fg=color)
            label.pack(side="left", padx=(ui_styles.SPACING["normal"], 0))
            self._counter_labels[status] = label

        tools = styled_frame(bar)
        tools.pack(side="right", padx=(0, ui_styles.SPACING["tight"]))
        self._retry_btn = CustomButton(
            tools, "buttons", "po_retry_failed_btn",
            text=i18n.t("print_orders.retry_failed"),
            command=self._on_retry_failed_clicked,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"])
        self._retry_btn.pack(side="left")
        self._merge_btn = CustomButton(
            tools, "buttons", "po_generate_pdf_btn",
            text=i18n.t("print_orders.merge_pdfs"),
            command=self._merge_done_orders,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"])
        self._merge_btn.pack(side="left", padx=(ui_styles.SPACING["tight"], 0))
        self._merged_link = tk.Label(
            tools, text="", font=ui_styles.FONTS["small"],
            fg=ui_styles.COLORS["primary"], bg=ui_styles.COLORS["bg"],
            cursor="hand2")
        self._merged_link.pack(side="left",
                               padx=(ui_styles.SPACING["tight"], 0))
        self._merged_link.bind("<Button-1>", lambda e: self._open_merged())

        self._refresh_history_btn()

    def _build_single_info(self):
        """The IW32 single-order card (shown instead of the list)."""
        frame = styled_frame(self)
        self._single_info_frame = frame
        card = card_frame(frame)
        card.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                  pady=(ui_styles.SPACING["normal"], 0))

        title = styled_frame(card, bg=ui_styles.COLORS["surface"])
        title.pack(fill="x", padx=12, pady=(10, 0))
        styled_label(
            title, "IW32", font=ui_styles.FONTS["small"],
            fg=ui_styles.COLORS["text_secondary"],
            bg=ui_styles.COLORS["surface"]).pack(side="left", padx=(0, 9))
        self._iw_order_var = self.bind_var("po_iw_order", tk.StringVar())
        self._iw_text_var = self.bind_var("po_iw_text", tk.StringVar())
        self._iw_status_var = self.bind_var("po_iw_status", tk.StringVar())
        self._iw_start_var = self.bind_var("po_iw_start", tk.StringVar())
        self._iw_workplace_var = self.bind_var("po_iw_workplace", tk.StringVar())

        tk.Label(
            title, textvariable=self._iw_order_var,
            font=ui_styles.FONTS["title"], bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text"], anchor="w",
        ).pack(side="left")
        tk.Label(
            title, textvariable=self._iw_text_var,
            font=ui_styles.FONTS["body"], bg=ui_styles.COLORS["surface"],
            fg=ui_styles.COLORS["text_secondary"], anchor="w",
        ).pack(side="left", padx=(10, 0))

        grid = styled_frame(card, bg=ui_styles.COLORS["surface"])
        grid.pack(fill="x", padx=12, pady=(10, 0))
        fields = [
            ("print_orders.iw32_status", self._iw_status_var),
            ("print_orders.iw32_start", self._iw_start_var),
            ("print_orders.iw32_workplace", self._iw_workplace_var),
        ]
        for column, (key, var) in enumerate(fields):
            cell = styled_frame(grid, bg=ui_styles.COLORS["surface"])
            cell.grid(row=0, column=column, sticky="w", padx=(0, 26))
            styled_label(
                cell, i18n.t(key), font=ui_styles.FONTS["small"],
                fg=ui_styles.COLORS["text_secondary"],
                bg=ui_styles.COLORS["surface"]).pack(anchor="w")
            tk.Label(
                cell, textvariable=var, font=ui_styles.FONTS["header"],
                bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
                anchor="w",
            ).pack(anchor="w")

        styled_label(
            card, i18n.t("print_orders.iw32_hint"),
            font=ui_styles.FONTS["small"],
            fg=ui_styles.COLORS["text_secondary"],
            bg=ui_styles.COLORS["surface"], justify="left",
        ).pack(anchor="w", padx=12, pady=(12, 10))

    def _build_treeview(self):
        style = ttk.Style()
        style.configure("PO_TV.Treeview", font=ui_styles.FONTS["body"],
                        background=ui_styles.COLORS["surface"],
                        fieldbackground=ui_styles.COLORS["surface"])
        style.configure("PO_TV.Treeview.Heading", font=ui_styles.FONTS["body"])
        style.map("PO_TV.Treeview",
                  background=[("selected", ui_styles.COLORS["primary"])])
        for tag_name in self.STATUS_TAGS.values():
            style.configure(tag_name, foreground=ui_styles.COLORS["text"])

        tf = styled_frame(self._list_area)
        tf.pack(fill="both", expand=True, padx=ui_styles.SPACING["comfortable"],
                pady=(ui_styles.SPACING["tight"], 0))
        self._grid_frame = tf

        self.tree = self.CustomTreeview(
            tf, component_type="datagridview", button_id="po_orders_grid",
            style="PO_TV.Treeview", selectmode="extended")
        # The right-click menu is built here so it can also offer column
        # configuration, row actions and Clear List in one place.
        self.grid_manager = GridManager(
            self.tree, "po_orders_grid", self.config_data,
            manage_scrollbar=False, bind_right_click=False)
        self.grid_manager.status_label = self.status_label
        self.grid_manager.post_apply_callback = self._add_status_column
        self.tree["show"] = "headings"
        self.tree.tag_configure("po_done", foreground=ui_styles.COLORS["success"])
        self.tree.tag_configure("po_failed", foreground=ui_styles.COLORS["error"])
        self.tree.tag_configure("po_no_release", foreground=ui_styles.COLORS["text_secondary"])
        self.tree.tag_configure("po_in_progress", foreground=ui_styles.COLORS["primary"])
        self.tree.tag_configure("po_pending", foreground=ui_styles.COLORS["text"])

        self._grid_v_scroll = ttk.Scrollbar(
            tf, orient="vertical", command=self.tree.yview)
        self._grid_h_scroll = ttk.Scrollbar(
            tf, orient="horizontal", command=self.tree.xview)
        self._grid_v_scroll.pack(side="right", fill="y")
        self._grid_h_scroll.pack(side="bottom", fill="x")
        self.tree.pack(side="top", fill="both", expand=True)
        self.tree.configure(yscrollcommand=self._grid_v_scroll.set,
                            xscrollcommand=self._grid_h_scroll.set)

        self._empty_label = tk.Label(
            tf, text="", font=ui_styles.FONTS["body"],
            fg=ui_styles.COLORS["text_secondary"], bg=ui_styles.COLORS["surface"],
            justify="center")
        self._empty_label.place(relx=0.5, rely=0.45, anchor="center")

        self.tree.bind("<Double-1>", self._on_row_double_click)
        self.tree.bind("<Button-3>", self._on_row_right_click)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._refresh_action_state())

    def _build_progress_bar(self):
        pf = styled_frame(self)
        # Packed only while a run is active; see _refresh_action_state.
        self._progress_frame = pf
        self._prog = ttk.Progressbar(pf, orient="horizontal", mode="determinate")
        self._prog.pack(fill="x", expand=True, side="left")
        self._prog_label = styled_label(
            pf, "0 / 0", font=ui_styles.FONTS["body"], bg=ui_styles.COLORS["bg"])
        self._prog_label.pack(side="left", padx=(ui_styles.SPACING["tight"], 0))
        styled_label(
            pf, i18n.t("print_orders.release_rule"),
            font=ui_styles.FONTS["small"],
            fg=ui_styles.COLORS["text_secondary"],
            bg=ui_styles.COLORS["bg"]).pack(side="right", padx=(0, 4))

    def _build_status_bar(self, text: str = "Ready"):
        bar, self.status_label = super()._build_status_bar(text)
        bar.pack(side="bottom", fill="x", padx=2, pady=2)
        return bar, self.status_label

    # ── history pane ────────────────────────────────────────────────────────
    def _refresh_history_btn(self):
        if not hasattr(self, "_history_btn"):
            return
        count = len(self._history.runs)
        self._history_btn.config(
            text=f"{i18n.t('print_orders.history')} ({count})")

    def _toggle_history(self):
        if self._history_pane is None:
            self._history_pane = PrintHistoryPane(
                self, self._history, on_reprint=self._print_order_from_history)
        if self._history_visible:
            self._history_pane.frame.pack_forget()
            self._history_visible = False
        else:
            self._history_pane.refresh()
            self._history_pane.frame.pack(
                side="bottom", fill="both",
                padx=ui_styles.SPACING["comfortable"],
                pady=(ui_styles.SPACING["tight"], 0))
            self._history_visible = True

    # ── export / load ───────────────────────────────────────────────────────
    def _run_export(self, auto: bool = False):
        if self._processing:
            return self.report_status("Stop processing first", "warning")
        if self._loading:
            return
        self._loading = True
        self.engine.set_variable("export_done", "0")
        self._wipe_temp_files()
        self._refresh_action_state()
        self._update_empty_state()
        self.report_status(
            "IW38 detected - loading orders..." if auto
            else "Loading IW38 orders...", "info")
        self.run_script(
            "po_export_iw38_btn", title="Load IW38 Orders",
            on_success=lambda: self._on_export_done(auto))

    def _on_export_done(self, auto: bool = False):
        self._loading = False
        if self.engine.get_variable("export_done") != "1":
            self.report_status("IW38 returned no data", "error")
            self._refresh_action_state()
            self._update_empty_state()
            return
        if not os.path.exists(self._import_file):
            self.report_status("IW38 export file not found", "error")
            self._refresh_action_state()
            self._update_empty_state()
            return

        self.grid_manager.populate_from_export(self._import_file)
        self._add_status_column()

        self._all_items = list(self.tree.get_children())
        self._row_errors.clear()
        self._reset_session_outputs()

        self._rebuild_user_status_options()
        self._apply_filter()
        self._update_counters()
        self._refresh_action_state()
        self._update_empty_state()

        total = len(self._all_items)
        released = sum(
            1 for c in self._all_items if self._row_status(c) == self.STATUS_PENDING)
        if total:
            if not self._load_warning:
                self.report_status(
                    f"{total} orders loaded ({released} released)", "success")
        else:
            self.report_status(
                "IW38 returned no orders" if auto else "No orders loaded",
                "warning")

    def _add_status_column(self):
        current_cols = list(self.tree["columns"])
        if "Progress" not in current_cols:
            # Reassigning -columns resets every existing column to ttk defaults
            # (width 200, stretch=1), which discards the autosized widths and
            # lets the grid squash itself to the window instead of scrolling.
            saved = {
                c: {
                    "text": self.tree.heading(c).get("text", c),
                    "width": self.tree.column(c, "width"),
                    "minwidth": self.tree.column(c, "minwidth"),
                    "stretch": self.tree.column(c, "stretch"),
                    "anchor": self.tree.column(c, "anchor"),
                }
                for c in current_cols
            }
            self.tree["columns"] = current_cols + ["Progress"]
            for c, opts in saved.items():
                self.tree.heading(c, text=opts["text"])
                self.tree.column(c, width=opts["width"], minwidth=opts["minwidth"],
                                 stretch=opts["stretch"], anchor=opts["anchor"])
            self.tree.heading("Progress", text="Progress")
            self.tree.column("Progress", width=100, minwidth=0, stretch=False,
                             anchor="center")

        export_headers = self.grid_manager.available_columns or []
        self._column_map = self._fields.resolve(export_headers)
        self._order_missing = "order" not in self._column_map
        status_fields = [
            field for field in ("system_status", "user_status", "status")
            if field in self._column_map
        ]
        stored_rows = self.grid_manager._stored_rows

        progress_col_idx = (
            len(current_cols) if "Progress" not in current_cols
            else current_cols.index("Progress"))
        unknown_count = 0
        for item in self.tree.get_children():
            vals = list(self.tree.item(item, "values"))
            row_index = self.tree.index(item)
            if row_index < len(stored_rows):
                raw = " ".join(
                    self._column_map.value(field, stored_rows[row_index])
                    for field in status_fields
                )
            else:
                raw = ""
            if not status_fields:
                progress = self.STATUS_UNKNOWN
            else:
                kind = self._fields.classify_status(raw)
                if kind == "released":
                    progress = self.STATUS_PENDING
                elif kind == "not_released":
                    progress = self.STATUS_NO_RELEASE
                else:
                    progress = self.STATUS_UNKNOWN
                    unknown_count += 1
            if len(vals) > progress_col_idx:
                vals[progress_col_idx] = progress
            else:
                vals.append(progress)
            self.tree.item(item, values=vals)
            self._apply_row_visual(item, progress)

        self._load_warning = ""
        if self._order_missing:
            expected = ", ".join(self._fields.aliases("order")[:6])
            available = ", ".join(export_headers) or "(none)"
            self._load_warning = i18n.t(
                "print_orders.order_column_missing",
                expected=expected, available=available)
            self.report_status(self._load_warning, "error")
        elif not status_fields:
            self._load_warning = i18n.t("print_orders.status_unknown_warning")
            self.report_status(self._load_warning, "warning")
        elif unknown_count:
            self._load_warning = i18n.t(
                "print_orders.status_unrecognised", count=unknown_count)
            self.report_status(self._load_warning, "warning")

        # Column config re-applies rebuild the rows (new item ids), so keep the
        # filter/action state pointing at the live items.
        self._all_items = list(self.tree.get_children())
        if hasattr(self, "_user_status_btn"):
            self._rebuild_user_status_options()

    # ── filter / counters / action state ────────────────────────────────────
    def _clear_filter(self):
        self._filter_var.set("")
        self._clear_user_status_filter(apply=False)
        self._apply_filter()
        self._filter_entry.focus_set()

    def _selected_user_statuses(self) -> set[str]:
        return set(self._user_status_selected)

    def _set_user_status_selected(self, selected, apply: bool = True):
        self._user_status_selected = set(selected) & set(self._user_status_options)
        self._update_user_status_button()
        if apply:
            self._apply_filter()

    def _update_user_status_button(self):
        if not hasattr(self, "_user_status_btn"):
            return
        selected = self._user_status_selected
        if not selected:
            text = i18n.t("print_orders.all_statuses")
        elif len(selected) == 1:
            text = next(iter(selected))
        else:
            text = f"{len(selected)} {i18n.t('print_orders.done').lower()}"
        self._user_status_btn.config(text=text)

    def _open_user_status_picker(self):
        if self._user_status_popup is not None:
            self._close_user_status_picker()
            return
        if not self._user_status_options:
            self.report_status("Load orders first to filter on user status",
                               "info")
            return
        self._user_status_popup = _UserStatusPicker(
            self, self._user_status_btn, self._user_status_options,
            self._user_status_selected,
            on_change=self._on_user_status_picked,
            on_close=self._on_user_status_picker_closed)

    def _on_user_status_picked(self, selected):
        self._set_user_status_selected(selected)

    def _on_user_status_picker_closed(self):
        self._user_status_popup = None

    def _close_user_status_picker(self):
        popup = self._user_status_popup
        self._user_status_popup = None
        if popup is not None:
            popup._close()

    def _clear_user_status_filter(self, apply: bool = True):
        self._user_status_selected = set()
        self._update_user_status_button()
        if apply and hasattr(self, "_filter_var"):
            self._apply_filter()

    def _user_status_column_index(self) -> int:
        cols = list(self.tree["columns"])
        if self._column_map is None:
            return -1
        # Prefer the dedicated user-status column; fall back to a combined
        # status column before the system status.
        for field in ("user_status", "status", "system_status"):
            header = self._column_map.header(field)
            if header in cols:
                return cols.index(header)
        return -1

    def _row_user_status(self, item: str) -> set[str]:
        if self._user_status_col_idx < 0:
            return set()
        values = self.tree.item(item, "values")
        if self._user_status_col_idx >= len(values):
            return set()
        raw = str(values[self._user_status_col_idx]).strip()
        return {token for token in re.split(r"[\s,;/]+", raw) if token}

    def _rebuild_user_status_options(self):
        """Refresh the user-status picker from the loaded rows, keeping any
        selection that still exists."""
        if not hasattr(self, "_user_status_btn"):
            return
        self._close_user_status_picker()
        items = self._all_items or list(self.tree.get_children())
        self._user_status_col_idx = self._user_status_column_index()
        tokens: set[str] = set()
        for item in items:
            tokens.update(self._row_user_status(item))
        options = sorted(tokens, key=str.casefold)
        self._user_status_options = options
        self._user_status_selected = self._user_status_selected & set(options)
        if hasattr(self, "_user_status_btn") and self._user_status_btn is not None:
            self._user_status_btn.config(
                state="normal" if self._user_status_col_idx >= 0 else "disabled")
        self._update_user_status_button()

    def _apply_filter(self):
        if not hasattr(self, "_filter_var"):
            return
        query = self._filter_var.get().strip().lower()
        selected_statuses = self._selected_user_statuses()
        if not query and not selected_statuses:
            # Common case: nothing hidden. Avoid detach/reattach churn on every
            # status change while a batch runs.
            if len(self.tree.get_children()) == len(self._all_items):
                self._refresh_action_state()
                return
        matches: list[str] = []
        for item in self._all_items:
            values = [str(v) for v in self.tree.item(item, "values")]
            text_ok = not query or any(query in v.lower() for v in values)
            status_ok = (not selected_statuses
                         or bool(self._row_user_status(item) & selected_statuses))
            if text_ok and status_ok:
                matches.append(item)

        for item in self._all_items:
            if self.tree.exists(item):
                self.tree.detach(item)
        for item in matches:
            self.tree.reattach(item, "", "end")
        self._refresh_action_state()

    def _update_counters(self):
        if not hasattr(self, "_counter_labels"):
            return
        counts = {
            self.STATUS_PENDING: 0,
            self.STATUS_NO_RELEASE: 0,
            self.STATUS_UNKNOWN: 0,
            self.STATUS_DONE: 0,
            self.STATUS_FAILED: 0,
        }
        for item in self._all_items:
            status = self._row_status(item)
            if status in counts:
                counts[status] += 1
        for status, label in self._counter_labels.items():
            label.config(
                text=f"{i18n.t(self.STATUS_LABEL_KEYS[status])} {counts[status]}")

    def _update_empty_state(self):
        if not hasattr(self, "_empty_label"):
            return
        if self._mode == "single":
            self._empty_label.place_forget()
            return
        if self._all_items:
            if self.tree.get_children():
                self._empty_label.place_forget()
            else:
                self._empty_label.config(
                    text=i18n.t("print_orders.no_rows_match"))
                self._empty_label.place(relx=0.5, rely=0.45, anchor="center")
            return
        if self._loading:
            text = i18n.t("print_orders.loading")
        else:
            text = i18n.t("print_orders.empty_open_iw38")
        self._empty_label.config(text=text)
        self._empty_label.place(relx=0.5, rely=0.45, anchor="center")

    def _set_primary(self, button, primary: bool):
        if primary:
            button.config(bg=ui_styles.COLORS["primary"], fg="#ffffff")
        else:
            button.config(bg=ui_styles.COLORS["surface"],
                          fg=ui_styles.COLORS["text"])

    def _set_left(self, button, visible: bool, padx=0):
        """Show/hide a left-aligned action without losing its position."""
        try:
            managed = bool(button.winfo_manager())
            if visible and not managed:
                button.pack(side="left", padx=padx, before=self._actions_end)
            elif not visible and managed:
                button.pack_forget()
        except tk.TclError:
            pass

    def _set_result_tool(self, button, visible: bool, before):
        """Show/hide a result action inside the right-hand tools group."""
        try:
            managed = bool(button.winfo_manager())
            if visible and not managed:
                button.pack(side="left",
                            padx=(ui_styles.SPACING["tight"], 0), before=before)
            elif not visible and managed:
                button.pack_forget()
        except tk.TclError:
            pass

    def _merge_signature(self, sources) -> tuple:
        colors = tuple(sorted(getattr(self, "_team_color_overrides", {}).items()))
        return (
            tuple((str(order), str(path)) for order, path in sources),
            bool(getattr(self, "_group_by_workcenter", False)),
            colors,
        )

    def _visible_pending(self) -> list[str]:
        if self._order_missing:
            return []
        return [
            c for c in self.tree.get_children()
            if self._row_status(c) in (
                self.STATUS_PENDING, self.STATUS_FAILED, self.STATUS_UNKNOWN)
        ]

    def _refresh_action_state(self):
        if not hasattr(self, "_load_btn"):
            return
        processing = self._processing
        single = self._mode == "single"
        has_rows = bool(self._all_items)
        visible_pending = self._visible_pending() if has_rows else []
        selected_items = self.tree.selection()
        selected_pending = [
            c for c in selected_items
            if self._row_status(c) in (
                self.STATUS_PENDING, self.STATUS_FAILED, self.STATUS_UNKNOWN)
        ] if not self._order_missing else []
        any_failed = any(
            self._row_status(c) == self.STATUS_FAILED for c in self._all_items)
        merge_sources = self._merge_sources()
        merge_pending = (
            bool(merge_sources)
            and self._merge_signature(merge_sources) != self._merged_signature
        )

        self._set_left(self._load_btn, not single)
        self._set_left(self._single_btn, single,
                       padx=(ui_styles.SPACING["tight"], 0))
        self._set_left(self._print_all_btn, not single,
                       padx=(ui_styles.SPACING["tight"], 0))
        self._set_left(
            self._print_sel_btn,
            not single and bool(selected_items) and not processing,
            padx=(ui_styles.SPACING["tight"], 0))
        self._set_left(self._stop_btn, processing,
                       padx=(ui_styles.SPACING["tight"], 0))
        self._set_left(self._pause_btn, processing,
                       padx=(ui_styles.SPACING["tight"], 0))
        self._set_result_tool(
            self._merge_btn, merge_pending and not processing,
            before=self._merged_link)
        retry_before = (self._merge_btn if self._merge_btn.winfo_manager()
                        else self._merged_link)
        self._set_result_tool(
            self._retry_btn, any_failed and not processing,
            before=retry_before)

        self._load_btn.config(
            state="disabled" if processing or self._loading else "normal")
        self._single_btn.config(state="disabled" if processing else "normal")
        self._print_sel_btn.config(
            state="normal" if selected_pending and not processing else "disabled")
        self._print_all_btn.config(
            state="normal" if visible_pending and not processing else "disabled")
        self._stop_btn.config(state="normal" if processing else "disabled")
        self._pause_btn.config(state="normal" if processing else "disabled")
        self._retry_btn.config(
            state="normal" if any_failed and not processing else "disabled")
        self._merge_btn.config(
            state="normal" if merge_sources and not processing else "disabled")

        if processing:
            if not self._progress_frame.winfo_manager():
                self._progress_frame.pack(
                    fill="x", padx=ui_styles.SPACING["comfortable"],
                    pady=(ui_styles.SPACING["tight"], 0))
        elif self._progress_frame.winfo_manager():
            self._progress_frame.pack_forget()

        self._set_primary(
            self._load_btn, not single and not processing and not has_rows)
        self._set_primary(
            self._print_all_btn,
            not single and not processing and bool(visible_pending))
        self._set_primary(self._single_btn, single and not processing)

        self._update_counters()
        self._update_empty_state()

    # ── screen detection / single order ─────────────────────────────────────
    def _set_mode(self, mode: str):
        """Switch between the IW38 list and the IW32 single-order card."""
        self._mode = "single" if mode == "single" else "list"
        if self._mode == "single":
            self._list_area.pack_forget()
            if not self._single_info_frame.winfo_manager():
                self._single_info_frame.pack(
                    fill="x", padx=ui_styles.SPACING["comfortable"],
                    pady=(ui_styles.SPACING["tight"], 0))
        else:
            self._single_info_frame.pack_forget()
            if not self._list_area.winfo_manager():
                self._list_area.pack(fill="both", expand=True)
        self._refresh_action_state()

    def _detect_active_screen(self):
        if not self.winfo_exists():
            return
        if self._processing or self._loading or self._all_items:
            return
        if self._toolbar.debug_mode.get():
            return
        self.engine.set_variable("po_tcode", "")
        self.run_script("po_read_tcode", title="Detect SAP Screen",
                        on_success=self._on_tcode_detected)

    def _on_tcode_detected(self):
        if self._processing or self._loading:
            return
        tcode = (self.engine.get_variable("po_tcode") or "").strip().upper()
        if tcode == "IW38":
            self._set_mode("list")
            self._run_export(auto=True)
        elif tcode == "IW32":
            self._set_mode("single")
            self._read_iw32_info()
        else:
            # No usable SAP screen: the list is the default working mode.
            self._set_mode("list")
            if tcode:
                self.report_status(
                    f"SAP is on {tcode}. Open IW38 to load orders, or open an "
                    "order in IW32.", "info")
            else:
                err = getattr(self.engine, "_last_error", "") or ""
                if err:
                    self.report_status(f"Could not read SAP screen: {err[:120]}",
                                       "warning")
                else:
                    self.report_status(
                        "Open IW38 to load orders, or open an order in IW32.",
                        "info")

    def _read_iw32_info(self, on_success=None):
        for name in ("po_iw_order", "po_iw_text", "po_iw_status",
                     "po_iw_start", "po_iw_workplace"):
            self.engine.set_variable(name, "")
        self.run_script(
            "po_read_iw32_info", title="Read IW32 Order",
            on_success=on_success or self._on_iw32_info)

    def _on_iw32_info(self):
        order = (self.engine.get_variable("po_iw_order") or "").strip()
        if order:
            self.order_var.set(order)
            self.report_status(f"IW32 order {order} loaded", "info")
        else:
            self.report_status("No order open in IW32.", "warning")

    def _print_open_order(self):
        """Print the order currently open in IW32 (read fresh on click)."""
        if self._processing:
            return self.report_status("Stop processing first", "warning")
        self.report_status("Reading order open in IW32...", "info")
        self._read_iw32_info(on_success=self._on_open_order_read)

    def _on_open_order_read(self):
        order = (self.engine.get_variable("po_iw_order")
                 or self.engine.get_variable("po_current_order") or "").strip()
        if not order:
            return self.report_status(
                "No order open in IW32. Open an order in SAP first.", "warning")
        self.order_var.set(order)
        short_text = self.engine.get_variable("po_iw_text") or ""
        self._print_single(order, short_text)

    def _print_single(self, order: str, short_text: str = ""):
        if self._processing:
            return self.report_status("Stop processing first", "warning")
        order = (order or "").strip()
        if not order:
            return self.report_status("No order number to print", "warning")
        existing = self._history.find_single_order(order)
        if existing is not None:
            started = existing.get("started") or "an earlier run"
            overwrite = messagebox.askyesno(
                "Order already printed",
                f"Order {order} was already printed on {started}.\n\n"
                "Overwrite that PDF and history entry?",
                parent=self)
            if not overwrite:
                return self.report_status(
                    f"Order {order} was not printed again", "info")
        self._overwrite_single = existing
        if not short_text:
            short_text = self._text_for_order(order)
        item = self._item_for_order(order)
        if item is None:
            item = self._add_order_row(order, short_text)
        self._begin_run([(item, order, short_text)], kind="single")

    def _print_order_from_history(self, order: str, short_text: str):
        if self._processing:
            return self.report_status("Stop processing first", "warning")
        self.order_var.set(order)
        self._print_single(order, short_text)

    # ── printing ────────────────────────────────────────────────────────────
    def _on_print_all_clicked(self):
        items = self._visible_pending()
        if not items:
            return self.report_status("No pending orders in the current view",
                                      "warning")
        unknown = sum(
            1 for item in items
            if self._row_status(item) == self.STATUS_UNKNOWN)
        if unknown and not messagebox.askyesno(
            i18n.t("print_orders.title"),
            i18n.t("print_orders.process_unknown_confirm", count=unknown),
            parent=self,
        ):
            return
        self._overwrite_single = None
        self._begin_run(
            [(item, self._order_num(item), self._short_text_for(item))
             for item in items], kind="batch")

    def _on_print_selected_clicked(self):
        items = [
            c for c in self.tree.selection()
            if self._row_status(c) in (
                self.STATUS_PENDING, self.STATUS_FAILED, self.STATUS_UNKNOWN)
        ]
        if not items:
            return self.report_status("Select one or more pending orders first",
                                      "warning")
        self._overwrite_single = None
        self._begin_run(
            [(item, self._order_num(item), self._short_text_for(item))
             for item in items], kind="batch")

    def _on_retry_failed_clicked(self):
        items = [c for c in self._all_items
                 if self._row_status(c) == self.STATUS_FAILED]
        if not items:
            return self.report_status("No failed orders to retry", "info")
        self._overwrite_single = None
        self._begin_run(
            [(item, self._order_num(item), self._short_text_for(item))
             for item in items], kind="batch")

    def _on_stop_clicked(self):
        if not self._processing:
            return
        self._stop_requested = True
        self.report_status("Stopping after the current step...", "warning")

    def _begin_run(self, orders: list[tuple[str | None, str, str]],
                   kind: str = "batch"):
        self._processing = True
        self._paused = False
        self._stop_requested = False
        self._pending_items = list(orders)
        self._total = len(orders)
        self._done_count = 0
        self._failed_count = 0
        self._run_orders = [order for _, order, _ in orders]
        self._run_kind = kind
        self._merged_file = None
        if self._merged_link is not None:
            self._merged_link.config(text="")

        self._ensure_work_dir()
        now = datetime.now()
        self._run_started = now.strftime("%d.%m.%Y %H:%M")
        self._run_started_day = now.strftime("%d.%m.%Y")
        self.engine.set_variable(
            "po_export_path", self._work_dir.replace("\\", "\\\\"))
        self.engine.set_variable("po_img_path", self._temp_path)

        self._prog["maximum"] = self._total
        self._prog["value"] = 0
        self._prog_label.config(text=f"0 / {self._total}")
        self._pause_btn.config(text=i18n.t("print_orders.pause"))
        self._refresh_action_state()
        self.report_status(f"Processing {self._total} order(s)...", "info")
        self._process_next()

    def _ensure_work_dir(self):
        os.makedirs(self._work_dir, exist_ok=True)

    def _clean_work_dir(self):
        if not os.path.isdir(self._work_dir):
            return
        for name in os.listdir(self._work_dir):
            path = os.path.join(self._work_dir, name)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _reset_session_outputs(self):
        """Start a fresh batch context after a new list or a clear."""
        self._order_pdfs = {}
        self._order_results = {}
        self._batch_dir = ""
        self._batch_file = ""
        self._batch_run_id = ""
        self._merged_file = None
        self._overwrite_single = None
        if self._merged_link is not None:
            self._merged_link.config(text="")
        self._clean_work_dir()

    def _new_single_dir(self, order: str) -> str:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        safe = re.sub(r"[^\w.-]", "_", order)
        base = os.path.join(self._output_root, f"{stamp}_order_{safe}")
        path = base
        counter = 2
        while os.path.exists(path):
            path = f"{base}_{counter}"
            counter += 1
        os.makedirs(path, exist_ok=True)
        return path

    def _batch_paths(self) -> tuple[str, str]:
        if not self._batch_dir:
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            base = os.path.join(self._output_root, f"{stamp}_batch")
            path = base
            counter = 2
            while os.path.exists(path):
                path = f"{base}_{counter}"
                counter += 1
            os.makedirs(path, exist_ok=True)
            self._batch_dir = path
            self._batch_file = os.path.join(
                path, f"Processed_{stamp}.pdf")
        return self._batch_dir, self._batch_file

    def _process_next(self):
        if self._stop_requested:
            return self._done()
        if self._paused:
            return self.after(200, self._process_next)
        if not self._pending_items:
            return self._done()

        item, order, short_text = self._pending_items.pop(0)
        if not order:
            self._done_count += 1
            self._failed_count += 1
            self._update_progress()
            return self.after(50, self._process_next)

        self._active_stem = self._pdf_stem(order, short_text)
        self._active_text = short_text
        self._order_results[order] = {
            "short_text": short_text,
            "workcenter": self._workcenter_for(item),
            "revision": self._revision_for(item),
            "cover_image": None,
        }
        self._set_status(item, self.STATUS_IN_PROGRESS)
        self._update_progress()
        self.update_idletasks()

        self.engine.set_variable("po_current_order", order)
        self.engine.set_variable(
            "po_pdf_filename", f"{self._active_stem}_workorder.pdf")
        self.engine.set_variable(
            "po_attach_filename", f"{self._active_stem}_attachments.txt")
        self.engine.set_variable("po_img_path", self._temp_path)
        self.engine.set_variable("po_img_basename", order)
        self.engine.set_variable("po_print_status", "")
        self._row_errors.pop(order, None)
        self.report_status(f"Opening {order}...", "info")
        self._run_script_and_wait(
            self._scripts["open"],
            lambda: self._on_step(item, order, 0),
        )

    def _on_step(self, item: str | None, order: str, step_idx: int):
        if self._stop_requested:
            return self._mark_done(item, order, False)

        if step_idx > 0 and self.engine._last_error:
            err = self.engine._last_error
            self._row_errors[order] = err
            self.report_status(f"{order}: step failed - {err[:80]}", "error")
            return self._finish_order(item, order)

        if step_idx >= len(self.STEPS):
            return self._finish_order(item, order)

        key = self.STEPS[step_idx]
        labels = {
            "open": "Opened in IW32",
            "print": "Printed to spool",
            "attach": "Attachments read",
            "spool": "Spool saved as PDF",
        }
        self.report_status(f"{order}: {labels.get(key, key)}", "info")
        self.update_idletasks()

        next_step = step_idx + 1
        if key == "attach":
            self._attach_export_idx = 0
            self._run_script_and_wait(
                self._scripts[key],
                lambda: self._on_attach_tab_done(item, order, next_step),
            )
        else:
            self._run_script_and_wait(
                self._scripts[key],
                lambda: self._on_step(item, order, next_step),
            )

    def _on_attach_tab_done(self, item: str | None, order: str, next_step: int):
        self._row_count = int(self.engine.get_variable("po_row_count", "0"))
        self._doknr_read_idx = 0
        self._pending_attach_rows = []
        self._seen_doknrs = set()
        self._item = item
        self._order = order
        self._attach_next_step = next_step
        self._read_next_doknr()

    def _max_attachments(self) -> int:
        try:
            return max(1, min(6, int(self._max_attach_var.get())))
        except (tk.TclError, ValueError):
            return self.MAX_ATTACHMENTS

    def _read_next_doknr(self):
        if (self._doknr_read_idx >= self._row_count
                or len(self._pending_attach_rows) >= self._max_attachments()):
            return self._start_attach_export()

        self.engine.set_variable("po_doknr_read_idx", str(self._doknr_read_idx))
        self._run_script_and_wait(
            self._scripts["read_doknr"],
            lambda: self._on_doknr_read(),
        )

    def _on_doknr_read(self):
        doknr = self.engine.get_variable("po_doknr_read_val", "")
        if doknr and doknr not in self._seen_doknrs:
            self._seen_doknrs.add(doknr)
            self._pending_attach_rows.append(self._doknr_read_idx)
        self._doknr_read_idx += 1
        self._read_next_doknr()

    def _start_attach_export(self):
        count = len(self._pending_attach_rows)
        if count == 0:
            self.report_status(f"{self._order}: no attachments found", "info")
            return self._on_step(self._item, self._order, self._attach_next_step)

        self._attach_export_idx = 0
        self.report_status(
            f"{self._order}: exporting {count} unique attachment(s)...", "info")
        self._export_next_attachment()

    def _export_next_attachment(self):
        if self._attach_export_idx >= len(self._pending_attach_rows):
            return self._on_step(self._item, self._order, self._attach_next_step)

        row_idx = self._pending_attach_rows[self._attach_export_idx]
        save_name = f"{self._order}_{self._attach_export_idx + 1}.bin"
        save_path = os.path.join(self._temp_path, save_name)
        escaped = save_path.replace("\\", "\\\\")

        self.engine.set_variable("po_attach_row_idx", str(row_idx))
        self.engine.set_variable("po_attach_target", escaped)

        self.report_status(
            f"{self._order}: attachment {self._attach_export_idx + 1}"
            f"/{len(self._pending_attach_rows)} (row {row_idx})", "info")
        self._run_script_and_wait(
            self._scripts["attach_export_one"],
            lambda: self._on_attachment_exported(),
        )

    def _on_attachment_exported(self):
        self._attach_export_idx += 1
        self._export_next_attachment()

    def _finish_order(self, item: str | None, order: str):
        """Build the order's PDF off the UI thread, then save and advance."""
        stem = self._active_stem
        workorder_name = f"{stem}_workorder.pdf"
        layout = self._pdf_layout_var.get()
        max_attach = self._max_attachments()
        results: queue.Queue = queue.Queue()
        self.report_status(f"Order {order}: building PDF", "info")

        def worker():
            try:
                attachments = self._create_image_pdf(
                    order, stem, layout, max_attach)
                combined = self._combine_order_pdfs(
                    stem, workorder_name, attachments)
            except Exception:
                combined = ""
            results.put(combined)

        threading.Thread(target=worker, daemon=True).start()
        self._collect_order_pdf(item, order, results)

    def _collect_order_pdf(self, item: str | None, order: str,
                           results: queue.Queue):
        """Main thread: take the built PDF, then save it and mark the row."""
        if not self.winfo_exists():
            return
        try:
            combined = results.get_nowait()
        except queue.Empty:
            self.after(50, lambda: self._collect_order_pdf(item, order, results))
            return

        status = self.engine.get_variable("po_print_status", "")
        ok = status == "printed"
        result = {
            **dict(self._order_results.get(order, {})),
            "order": order,
            "short_text": self._active_text,
            "status": self.STATUS_DONE if ok else self.STATUS_FAILED,
            "error": self._row_errors.get(order, ""),
        }
        self._order_results[order] = result
        if combined:
            self._order_pdfs[order] = combined
        if ok and combined and self._run_kind == "single":
            if not self._save_single_output(order, combined):
                ok = False
        self._mark_done(item, order, ok)

    def _combine_order_pdfs(self, stem: str, workorder_name: str,
                            attachments_name: str) -> str:
        """Combine the work-order and photo parts into one PDF and drop the
        parts. Returns the combined path, or "" when nothing was produced."""
        workorder_path = os.path.join(self._work_dir, workorder_name)
        attachments_path = (os.path.join(self._work_dir, attachments_name)
                            if attachments_name else "")
        has_workorder = os.path.exists(workorder_path)
        has_attachments = bool(attachments_path and os.path.exists(attachments_path))
        if not has_workorder and not has_attachments:
            return ""

        out = os.path.join(self._work_dir, f"{stem}.pdf")
        try:
            if has_workorder and not has_attachments:
                os.replace(workorder_path, out)
            elif has_attachments and not has_workorder:
                os.replace(attachments_path, out)
            else:
                writer = PdfWriter()
                for path in (workorder_path, attachments_path):
                    reader = PdfReader(path)
                    for page in reader.pages:
                        writer.add_page(page)
                with open(out, "wb") as f:
                    writer.write(f)
                for path in (workorder_path, attachments_path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        except Exception:
            return ""
        return out if os.path.exists(out) else ""

    def _save_single_output(self, order: str, combined: str) -> bool:
        """Store a single-order print as one permanent PDF and update history."""
        existing = self._overwrite_single
        if existing is None:
            existing = self._history.find_single_order(order)
        result = self._order_results.get(order, {})
        try:
            if existing is not None:
                folder = existing.get("folder") or self._new_single_dir(order)
                os.makedirs(folder, exist_ok=True)
                file_name = os.path.basename(combined)
                dest = os.path.join(folder, file_name)
                old_file = existing.get("file") or ""
                old_path = os.path.join(folder, old_file) if old_file else ""
                if old_path and os.path.abspath(old_path) != os.path.abspath(dest):
                    try:
                        os.remove(old_path)
                    except OSError:
                        pass
                # Drop legacy part files from an older version.
                for name in os.listdir(folder):
                    if name.endswith(("_workorder.pdf", "_attachments.pdf")):
                        try:
                            os.remove(os.path.join(folder, name))
                        except OSError:
                            pass
                if os.path.abspath(dest) != os.path.abspath(combined):
                    shutil.copy2(combined, dest)
                entry = {
                    "id": existing.get("id", ""),
                    "folder": folder,
                    "started": self._run_started,
                    "started_day": self._run_started_day,
                    "kind": "single",
                    "order": order,
                    "short_text": self._active_text,
                    "workcenter": result.get("workcenter", ""),
                    "revision": result.get("revision", ""),
                    "cover_image": result.get("cover_image"),
                    "status": self.STATUS_DONE,
                    "file": file_name,
                    "error": "",
                }
                self._history.upsert_single(order, entry)
            else:
                folder = self._new_single_dir(order)
                file_name = os.path.basename(combined)
                dest = os.path.join(folder, file_name)
                shutil.copy2(combined, dest)
                entry = {
                    "id": os.path.basename(folder),
                    "folder": folder,
                    "started": self._run_started,
                    "started_day": self._run_started_day,
                    "kind": "single",
                    "order": order,
                    "short_text": self._active_text,
                    "workcenter": result.get("workcenter", ""),
                    "revision": result.get("revision", ""),
                    "cover_image": result.get("cover_image"),
                    "status": self.STATUS_DONE,
                    "file": file_name,
                    "error": "",
                }
                self._history.add_run(entry)
        except OSError as exc:
            self.report_status(
                f"Order {order} printed but could not be saved: {exc}", "error")
            return False
        self._history.prune(self.RUN_LIMIT)
        self._refresh_history_btn()
        if self._history_pane is not None:
            self._history_pane.refresh()
        return True

    def _mark_done(self, item: str | None, order: str, ok: bool):
        self._set_status(item, self.STATUS_DONE if ok else self.STATUS_FAILED)
        self._done_count += 1
        if not ok:
            self._failed_count += 1
            if self._stop_on_error_var.get():
                self._stop_requested = True
                self.report_status(
                    f"Order {order} failed - stopping (Stop on first error)",
                    "error")
        self._update_progress()
        self._refresh_action_state()
        if ok:
            self.report_status(f"Order {order}: done", "success")
        elif not self._stop_requested:
            self.report_status(f"Order {order}: failed", "error")
        self.after(50, self._process_next)

    def _done(self):
        stopped = self._stop_requested
        remaining = len(self._pending_items)
        self._processing = False
        self._paused = False
        self._stop_requested = False
        self._overwrite_single = None

        done = self._done_count - self._failed_count
        failed = self._failed_count
        # Auto-stitch only a clean run; any failure leaves the PDFs alone.
        if failed == 0 and not stopped and done:
            self._merge_done_orders(auto=True)
        self._refresh_action_state()
        self._refresh_history_btn()
        if self._history_pane is not None:
            self._history_pane.refresh()

        if failed:
            self.report_status(
                f"{done} done, {failed} failed - Retry Failed retries them",
                "warning")
        elif stopped and remaining:
            self.report_status(
                f"Stopped after {done} order(s); {remaining} not processed",
                "warning")
        else:
            self.report_status(f"All {done} order(s) done", "success")

    # ── engine helper ───────────────────────────────────────────────────────
    def _run_script_and_wait(self, script: str, on_done: Callable):
        if not script:
            return on_done()

        toolbar = self._toolbar
        if toolbar and toolbar.debug_mode.get():
            toolbar.open_debugger(script, "Start Step", on_success=on_done)
            return

        def sub_script_cb(btn_id: str):
            sub = self._script(btn_id)
            if sub:
                self.engine.run_immediate(sub)

        self.engine.run_script_callbacks.append(sub_script_cb)
        try:
            self.engine.run_immediate(script)
        finally:
            if sub_script_cb in self.engine.run_script_callbacks:
                self.engine.run_script_callbacks.remove(sub_script_cb)

        self._wait_for_idle(on_done)

    def _wait_for_idle(self, on_done: Callable):
        if self._stop_requested:
            return on_done()
        e = self.engine
        commands_done = not e.commands or e.pointer >= len(e.commands)
        stopped = not (e._run_immediate_mode or e._continue_mode)
        if not e.is_busy and (commands_done or stopped):
            return on_done()
        self.after(100, lambda: self._wait_for_idle(on_done))

    # ── PDFs ────────────────────────────────────────────────────────────────
    @staticmethod
    def _pdf_stem(order: str, short_text: str = "") -> str:
        slug = re.sub(r"[^\w\s-]", "", short_text or "", flags=re.UNICODE)
        slug = re.sub(r"[-\s]+", "_", slug).strip("_")
        slug = slug[:40].strip("_")
        return f"WO_{order}_{slug}" if slug else f"WO_{order}"

    def _create_image_pdf(self, order: str, stem: str,
                          layout: str | None = None,
                          max_attach: int | None = None) -> str:
        try:
            from PIL import Image
        except ImportError:
            return ""

        if layout is None:
            layout = self._pdf_layout_var.get()
        if max_attach is None:
            max_attach = self._max_attachments()

        paths = []
        for i in range(max_attach):
            p = os.path.join(self._temp_path, f"{order}_{i + 1}.bin")
            if os.path.exists(p):
                paths.append(p)
        if not paths:
            return ""

        imgs = []
        for p in paths:
            try:
                img = Image.open(p).convert("RGB")
                img.load()
            except Exception:
                continue
            if img.width > img.height:
                img = img.rotate(-90, expand=True)
            imgs.append(img)
        if not imgs:
            return ""
        try:
            cover = retain_cover_image(
                order, paths, os.path.join(self._output_root, ".covers"))
        except OSError:
            cover = None
        if cover:
            self._order_results.setdefault(order, {})["cover_image"] = cover

        per = int(layout) if layout in ("1", "2", "4") else (
            1 if len(imgs) == 1 else 2 if len(imgs) == 2 else 4)
        pages = [self._compose_page(imgs[i:i + per], per)
                 for i in range(0, len(imgs), per)]

        name = f"{stem}_attachments.pdf"
        out = os.path.join(self._work_dir, name)
        try:
            if len(pages) == 1:
                pages[0].save(out, "PDF")
            else:
                pages[0].save(out, "PDF", save_all=True,
                              append_images=pages[1:])
        except Exception:
            return ""

        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass
        return name

    @staticmethod
    def _compose_page(imgs, per: int):
        from PIL import Image

        page_w, page_h = 595, 842
        page = Image.new("RGB", (page_w, page_h), "white")
        n = len(imgs)
        if per == 1 or n == 1:
            imgs[0].thumbnail((page_w - 40, page_h - 40))
            page.paste(imgs[0], ((page_w - imgs[0].width) // 2,
                                 (page_h - imgs[0].height) // 2))
        elif per == 2:
            half = page_h // 2
            for i, img in enumerate(imgs):
                img.thumbnail((page_w - 40, half - 40))
                x = (page_w - img.width) // 2
                y = i * half + (half - img.height) // 2
                page.paste(img, (x, y))
        else:
            cw, ch = page_w // 2, page_h // 2
            for i, img in enumerate(imgs):
                if i >= 4:
                    break
                img.thumbnail((cw - 20, ch - 20))
                x = (i % 2) * cw + (cw - img.width) // 2
                y = (i // 2) * ch + (ch - img.height) // 2
                page.paste(img, (x, y))
        return page

    def _done_visible_orders(self) -> list[str]:
        orders: list[str] = []
        seen: set[str] = set()
        for item in self.tree.get_children():
            if self._row_status(item) != self.STATUS_DONE:
                continue
            order = self._order_num(item)
            if order and order not in seen:
                seen.add(order)
                orders.append(order)
        return orders

    def _source_pdf(self, order: str) -> str | None:
        path = self._order_pdfs.get(order)
        if path and os.path.exists(path):
            return path
        return self._history.single_pdf_path(order)

    def _merge_sources(self) -> list[tuple[str, str]]:
        sources: list[tuple[str, str]] = []
        for order in self._done_visible_orders():
            path = self._source_pdf(order)
            if path:
                sources.append((order, path))
        return sources

    def _merge_done_orders(self, auto: bool = False):
        """Merge every DONE row visible in the table, not just this run.

        A single-order print keeps its own file; a batch (or a manual merge
        after more DONE rows appeared) produces one batch PDF.
        """
        sources = self._merge_sources()
        if not sources:
            if not auto:
                self.report_status(
                    "No DONE orders with a stored PDF to merge", "warning")
            return
        if auto and self._run_kind == "single" and len(sources) < 2:
            return
        if auto and self._run_kind == "batch" and len(sources) < 1:
            return

        records: list[dict] = []
        merged: list[tuple[str, str]] = []
        for order, path in sources:
            try:
                PdfReader(path)
            except Exception:
                continue
            item = self._item_for_order(order)
            result = self._order_results.get(order, {})
            records.append({
                "order": order,
                "title": result.get("short_text") or self._short_text_for(item),
                "workcenter": result.get("workcenter") or self._workcenter_for(item),
                "revision": result.get("revision") or self._revision_for(item),
                "cover_image": result.get("cover_image"),
                "pdf_path": path,
            })
            merged.append((order, path))
        if not records:
            if not auto:
                self.report_status("No readable PDFs to merge", "warning")
            return

        if getattr(self, "_merging", False):
            return
        self._merging = True
        batch_dir, batch_file = self._batch_paths()
        os.makedirs(batch_dir, exist_ok=True)
        revision = next((record["revision"] for record in records
                         if record["revision"]), "")
        date = self._run_started_day or datetime.now().strftime("%d.%m.%Y")
        colors = dict(getattr(self, "_team_color_overrides", {}))
        grouped = bool(getattr(self, "_group_by_workcenter", False))
        temp_file = batch_file + ".building"
        results: queue.Queue = queue.Queue()

        def worker():
            try:
                info = build_packet(
                    records, temp_file, revision=revision, date=date,
                    color_overrides=colors, grouped=grouped,
                )
                results.put((temp_file, info, None))
            except Exception as exc:
                results.put(("", None, exc))

        threading.Thread(target=worker, daemon=True).start()
        self._collect_merge_result(results, batch_file, batch_dir, merged,
                                   records, grouped, auto)

    def _collect_merge_result(self, results: queue.Queue, batch_file: str,
                              batch_dir: str, merged: list[tuple[str, str]],
                              records: list[dict], grouped: bool, auto: bool):
        if not self.winfo_exists():
            return
        try:
            temp_file, packet_info, error = results.get_nowait()
        except queue.Empty:
            self.after(50, lambda: self._collect_merge_result(
                results, batch_file, batch_dir, merged, records, grouped, auto))
            return

        self._merging = False
        if error or not temp_file:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass
            message = str(error)
            if "ReportLab and Pillow" in message:
                message = i18n.t(
                    "print_orders.summary_dependencies_missing",
                    "Team summaries need ReportLab and Pillow. "
                    "Open Settings → Update to install them.",
                )
            self.report_status(f"Could not build merged PDF: {message}", "error")
            self._refresh_action_state()
            return
        try:
            os.replace(temp_file, batch_file)
        except OSError as exc:
            self.report_status(f"Could not write merged PDF: {exc}", "error")
            self._refresh_action_state()
            return

        self._merged_file = batch_file
        self._merged_signature = self._merge_signature(merged)
        name = os.path.basename(batch_file)
        if self._merged_link is not None:
            self._merged_link.config(text=name)

        payload = [{
            "order": record["order"],
            "short_text": record["title"],
            "workcenter": record["workcenter"],
            "revision": record["revision"],
            "cover_image": record["cover_image"],
            "status": self.STATUS_DONE,
        } for record in records]
        now = datetime.now()
        run = {
            "id": os.path.basename(batch_dir),
            "folder": batch_dir,
            "started": now.strftime("%d.%m.%Y %H:%M"),
            "started_day": now.strftime("%d.%m.%Y"),
            "kind": "batch",
            "file": name,
            "done": len(merged),
            "failed": 0,
            "orders": payload,
            "grouped": grouped,
            "team_order": packet_info["team_order"],
        }
        self._batch_run_id = run["id"]
        self._history.upsert_batch(run["id"], run)
        self._history.prune(self.RUN_LIMIT)
        self._refresh_history_btn()
        if self._history_pane is not None:
            self._history_pane.refresh()
        if not auto:
            self.report_status(
                f"Merged {len(merged)} DONE order(s) into {name}", "success")
        self._refresh_action_state()

    def _open_merged(self):
        if self._merged_file and os.path.exists(self._merged_file):
            self._open_path(self._merged_file)

    def _open_path(self, path: str):
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            self.report_status(f"Could not open file: {exc}", "error")

    def _open_output_folder(self):
        if os.path.isdir(self._output_root):
            self._open_path(self._output_root)

    # ── row helpers ─────────────────────────────────────────────────────────
    def _stored_row(self, item: str) -> list[str] | None:
        """The export row behind a tree item, even when a column is hidden."""
        rows = getattr(self.grid_manager, "_stored_rows", [])
        try:
            index = self.tree.index(item)
        except tk.TclError:
            return None
        if 0 <= index < len(rows):
            return rows[index]
        return None

    def _workcenter_for(self, item: str | None) -> str:
        value = ""
        if item is not None and self._column_map is not None:
            row = self._stored_row(item)
            if row is not None:
                value = self._column_map.value("work_center", row)
            else:
                values = list(self.tree.item(item, "values"))
                columns = list(self.tree["columns"])
                header = self._column_map.header("work_center")
                if header in columns:
                    index = columns.index(header)
                    if index < len(values):
                        value = str(values[index]).strip()
        if not value and (item is None or getattr(self, "_run_kind", "") == "single"):
            value = str(self.engine.get_variable("po_iw_workplace", "")).strip()
        return value

    def _revision_for(self, item: str | None) -> str:
        if item is None or self._column_map is None:
            return ""
        row = self._stored_row(item)
        if row is not None:
            return self._column_map.value("revision", row)
        values = list(self.tree.item(item, "values"))
        columns = list(self.tree["columns"])
        header = self._column_map.header("revision")
        if header in columns:
            index = columns.index(header)
            if index < len(values):
                return str(values[index]).strip()
        return ""

    def _short_text_for(self, item: str | None) -> str:
        if item is None or self._column_map is None:
            return ""
        row = self._stored_row(item)
        if row is not None:
            return self._column_map.value("short_text", row)
        values = list(self.tree.item(item, "values"))
        # Values follow the treeview's display order, not the export order.
        columns = list(self.tree["columns"])
        header = self._column_map.header("short_text")
        if header in columns:
            index = columns.index(header)
            if index < len(values):
                return str(values[index]).strip()
        return ""

    def _item_for_order(self, order: str) -> str | None:
        for item in self._all_items:
            if self._order_num(item) == order:
                return item
        return None

    def _add_order_row(self, order: str, short_text: str) -> str | None:
        """Give a typed/opened order a DONE-able row when a list is loaded."""
        cols = list(self.tree["columns"])
        if not cols or self._order_missing or self._column_map is None:
            return None
        values = [""] * len(cols)
        order_idx = self._order_col_idx()
        if order_idx < 0:
            return None
        values[order_idx] = order
        text_header = self._column_map.header("short_text")
        if text_header in cols:
            values[cols.index(text_header)] = short_text
        if "Progress" in cols:
            values[cols.index("Progress")] = self.STATUS_PENDING
        item = self.tree.insert("", "end", values=values)
        self._all_items.append(item)
        self._apply_row_visual(item, self.STATUS_PENDING)
        self._apply_filter()
        self._update_counters()
        return item

    def _text_for_order(self, order: str) -> str:
        for item in self._all_items:
            if self._order_num(item) == order:
                return self._short_text_for(item)
        return ""

    def _order_col_idx(self) -> int:
        """The order-number column's position in this treeview, or -1.

        Resolved from the stable ``order`` field, never from a header list,
        so a localised or reordered export still works.
        """
        cols = list(self.tree["columns"])
        if self._column_map is None:
            return -1
        header = self._column_map.header("order")
        if header in cols:
            return cols.index(header)
        return -1

    def _order_num(self, item: str) -> str:
        row = self._stored_row(item)
        if row is not None and self._column_map is not None:
            return self._column_map.value("order", row)
        v = self.tree.item(item, "values")
        idx = self._order_col_idx()
        return str(v[idx]).strip() if v and 0 <= idx < len(v) else ""

    def _row_status(self, item: str) -> str:
        v = self.tree.item(item, "values")
        return str(v[-1]).strip() if v else self.STATUS_PENDING

    def _set_status(self, item: str | None, s: str):
        if item is None:
            return
        v = list(self.tree.item(item, "values"))
        if v:
            v[-1] = s
            self.tree.item(item, values=v)
        self._apply_row_visual(item, s)
        # Only a live filter needs rows to move; the unfiltered batch path
        # calls _refresh_action_state once per finished order instead.
        if hasattr(self, "_filter_var") and (
                self._filter_var.get().strip()
                or self._selected_user_statuses()):
            self._apply_filter()

    def _apply_row_visual(self, item: str | None, status: str):
        if item is None:
            return
        tag = self.STATUS_TAGS.get(status, "po_pending")
        self.tree.item(item, tags=(tag,))

    def _update_progress(self):
        self._prog["value"] = self._done_count
        self._prog_label.config(text=f"{self._done_count} / {self._total}")

    def _on_pause_clicked(self):
        if not self._processing:
            return
        if self._paused:
            self._paused = False
            self._pause_btn.config(text=i18n.t("print_orders.pause"))
            self.report_status("Resumed", "info")
            self._process_next()
        else:
            self._paused = True
            self._pause_btn.config(text=i18n.t("print_orders.resume"))
            self.report_status("Paused", "warning")

    # ── row actions ─────────────────────────────────────────────────────────
    def _pdf_for_order(self, order: str) -> str | None:
        path = self._order_pdfs.get(order)
        if path and os.path.exists(path):
            return path
        return self._history.pdf_for_order(order)

    def _open_order_in_iw32(self, order: str):
        if self._processing:
            return self.report_status("Stop processing first", "warning")
        self.order_var.set(order)
        self.engine.set_variable("po_current_order", order)
        self.run_script("po_open_order", title=f"Open Order {order}")

    def _on_row_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        order = self._order_num(iid)
        if not order:
            return
        if self._row_status(iid) == self.STATUS_DONE:
            path = self._pdf_for_order(order)
            if path:
                return self._open_path(path)
        self._open_order_in_iw32(order)

    def _on_row_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        order = self._order_num(iid) if iid else ""
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        menu = tk.Menu(self.tree, tearoff=0)
        if order:
            menu.add_command(
                label=i18n.t("print_orders.open_iw32"),
                command=lambda: self._open_order_in_iw32(order))
            menu.add_command(
                label=i18n.t("print_orders.process_this"),
                command=lambda o=order: self._print_single(o))
            menu.add_command(
                label=i18n.t("print_orders.copy_order"),
                command=lambda o=order: self._copy_text(o))
            pdf = self._pdf_for_order(order)
            if pdf:
                menu.add_command(
                    label=i18n.t("print_orders.open_pdf"),
                    command=lambda p=pdf: self._open_path(p))
                menu.add_command(
                    label=i18n.t("print_orders.open_folder"),
                    command=lambda p=pdf: self._open_path(os.path.dirname(p)))
            error = self._row_errors.get(order)
            if error:
                menu.add_command(
                    label=i18n.t("print_orders.show_error"),
                    command=lambda e=error: messagebox.showerror(
                        f"Order {order}", e, parent=self))
            menu.add_separator()
        menu.add_command(
            label=i18n.t("print_orders.configure_columns"),
            command=self.grid_manager.configure)
        menu.add_command(
            label=i18n.t("print_orders.clear_list"), command=self._clear_list)
        menu.post(event.x_root, event.y_root)

    def _copy_text(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(text)

    def _clear_list(self):
        if self._processing:
            return self.report_status("Stop processing first", "warning")
        items = list(self._all_items)
        if items:
            self.tree.delete(*items)
        self.grid_manager.clear()
        self._all_items = []
        self._row_errors.clear()
        self._merged_signature = ()
        self._reset_session_outputs()
        self._filter_var.set("")
        self._rebuild_user_status_options()
        self._apply_filter()
        self._refresh_action_state()
        self._update_empty_state()
        if items:
            self.report_status("List cleared", "info")

    # ── temp / close ────────────────────────────────────────────────────────
    def _remove_work_dir(self):
        shutil.rmtree(self._work_dir, ignore_errors=True)
        try:
            os.rmdir(self._work_root)
        except OSError:
            pass

    def _on_close(self):
        if self._processing:
            keep_going = messagebox.askyesno(
                i18n.t("print_orders.close_running_title"),
                i18n.t("print_orders.close_running_body"),
                parent=self)
            if not keep_going:
                return
            self._stop_requested = True
        self._wipe_temp_files()
        self._remove_work_dir()
        self.destroy()

    def _wipe_temp_files(self):
        if os.path.exists(self._import_file):
            try:
                os.remove(self._import_file)
            except OSError:
                pass
        for name in os.listdir(self._temp_path):
            if name.lower().endswith(
                    (".pdf", ".bin", ".jpg", ".png", ".jpeg", ".gif")):
                try:
                    os.remove(os.path.join(self._temp_path, name))
                except OSError:
                    pass
