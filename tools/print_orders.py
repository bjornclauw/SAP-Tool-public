"""PrintOrdersWindow — mass-print work orders from IW38.

Workflow:
  1. Open IW38, or open one order in IW32.
  2. "Load IW38 Orders" imports the open IW38 grid; "Print This Order"
     handles the order open in IW32 (or one typed into the Order field).
  3. Orders without "vrijgegeven" status are flagged "No Release".
  4. Print All / Print Selected / Retry Failed loop over released orders:
       a. Open IW32 with order
       b. Print work order as PDF via SAP GUI script
       c. Export attachment list from IHKD tab
       d. Download image files for that order
       e. Create filler PDF from downloaded images
  5. Completed PDFs are kept in data/print_output/<run>/ and recorded in
     data/print_history.json; the collapsible History pane shows past runs.

SAP scripts are short operations chained via engine polling.
Processing stays in the main thread using after() scheduling.
"""

from __future__ import annotations

import os
import re
import tkinter as tk
from collections.abc import Callable
from datetime import datetime
from tkinter import messagebox, ttk

from pypdf import PdfReader, PdfWriter

from core import config as cfg
from core import user_settings
from tools.grid_column_manager import GridColumnManager as GridManager
from tools.print_history import PrintHistoryStore
from tools.print_history_view import PrintHistoryPane
from ui import ui_styles
from ui.custom_widgets import BaseToolWindow, CustomButton
from ui.edit_modal import EditModal
from ui.ui_styles import styled_frame, styled_label


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
            top, text="All statuses", command=self._select_all,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text_secondary"],
            relief="flat", font=ui_styles.FONTS["small"], cursor="hand2",
            padx=4).pack(side="left")
        tk.Button(
            top, text="Done", command=self._close,
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
                inner, text="(no user status values)",
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

    STATUS_TAGS = {
        STATUS_PENDING: "po_pending",
        STATUS_IN_PROGRESS: "po_in_progress",
        STATUS_DONE: "po_done",
        STATUS_FAILED: "po_failed",
        STATUS_NO_RELEASE: "po_no_release",
    }

    STEPS = ["open", "print", "attach", "spool"]
    MAX_ATTACHMENTS = 4
    RUN_LIMIT = 30

    SHORT_TEXT_HEADERS = {
        "omschrijving", "description", "korte tekst", "kurtext", "text",
        "short text", "bezeichnung",
    }

    def __init__(self, parent, engine):
        super().__init__(parent, engine, titlebar_color=ui_styles.TOOL_COLORS["print_orders"])
        self.title("Print Orders")
        self.geometry("1060x760")
        self.minsize(720, 480)

        self._init_paths()
        self._load_po_settings()

        self._processing = False
        self._paused = False
        self._stop_requested = False
        self._loading = False
        self._done_count = 0
        self._failed_count = 0
        self._total = 0
        self._all_items: list[str] = []
        self._row_errors: dict[str, str] = {}
        self._processed_orders: list[str] = []
        self._run_files: dict[str, dict] = {}
        self._run_orders: list[str] = []
        self._run_dir = ""
        self._run_id = ""
        self._run_started = ""
        self._run_started_day = ""
        self._run_kind = "batch"
        self._run_recorded = False
        self._active_stem = ""
        self._active_text = ""
        self._merged_file: str | None = None
        self._merged_link: tk.Label | None = None
        self._history_pane: PrintHistoryPane | None = None
        self._history_visible = False

        self._build_actions()
        self._build_filter_bar()
        self._build_status_bar()
        self._build_treeview()
        self._build_progress_bar()
        self._cache_scripts()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._show()
        self._refresh_action_state()
        self._detect_after_id = self.after(300, self._detect_active_screen)

    def destroy(self):
        after_id = getattr(self, "_detect_after_id", None)
        if isinstance(after_id, str):
            try:
                self.after_cancel(after_id)
            except tk.TclError:
                pass
            self._detect_after_id = None
        if getattr(self, "_user_status_popup", None) is not None:
            self._close_user_status_picker()
        super().destroy()

    # ── paths / settings ────────────────────────────────────────────────────
    def _init_paths(self):
        base = os.path.dirname(os.path.dirname(__file__))
        self._temp_path = os.path.join(base, "tempexport")
        os.makedirs(self._temp_path, exist_ok=True)
        self._import_file = os.path.join(self._temp_path, self.EXPORT_FILENAME)

        self._output_root = os.path.join(base, "data", "print_output")
        os.makedirs(self._output_root, exist_ok=True)

        escaped = self._temp_path.replace("\\", "\\\\")
        self.engine.set_variable("po_import_path", escaped)
        self.engine.set_variable("po_import_file", self.EXPORT_FILENAME)
        self.engine.set_variable("po_export_path", escaped)

        self._history = PrintHistoryStore(output_root=self._output_root)

    def _load_po_settings(self):
        data = user_settings.load_user_settings()
        self._auto_merge_var = tk.BooleanVar(
            value=bool(data.get("po_auto_merge", True)))
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

    def _save_po_settings(self):
        user_settings.update_user_settings({
            "po_auto_merge": bool(self._auto_merge_var.get()),
            "po_stop_on_error": bool(self._stop_on_error_var.get()),
            "po_max_attachments": str(self._max_attach_var.get()),
            "po_pdf_layout": self._pdf_layout_var.get(),
        })

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

    def _build_actions(self):
        bar = styled_frame(self)
        bar.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                 pady=(ui_styles.SPACING["comfortable"], 0))

        self._load_btn = CustomButton(
            bar, "buttons", "po_export_iw38_btn",
            text="Load IW38 Orders", command=self._run_export,
            bg=ui_styles.COLORS["primary"], fg="#ffffff",
        )
        self._load_btn.pack(side="left")

        styled_label(
            bar, "Order:", font=ui_styles.FONTS["body"],
            bg=ui_styles.COLORS["bg"]).pack(
                side="left", padx=(ui_styles.SPACING["normal"], 4))
        self.order_var = self.bind_entry("po_order_entry")
        self._order_entry = self.CustomEntry(
            bar, "textinput", "po_order_entry",
            textvariable=self.order_var, width=12)
        self._order_entry.pack(side="left")
        self._order_entry.bind("<Return>", lambda e: self._print_open_order())

        self._single_btn = CustomButton(
            bar, "buttons", "po_print_open_order_btn",
            text="Print This Order", command=self._print_open_order,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"])
        self._single_btn.pack(side="left", padx=(ui_styles.SPACING["tight"], 0))

        self._print_sel_btn = CustomButton(
            bar, "buttons", "po_print_selected_btn",
            text="Print Selected", command=self._on_print_selected_clicked,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"])
        self._print_sel_btn.pack(side="left", padx=(ui_styles.SPACING["tight"], 0))

        self._print_all_btn = CustomButton(
            bar, "buttons", "po_start_btn",
            text="Print All", command=self._on_print_all_clicked,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"])
        self._print_all_btn.pack(side="left", padx=(ui_styles.SPACING["tight"], 0))

        self._stop_btn = CustomButton(
            bar, "buttons", "po_stop_btn", text="Stop",
            command=self._on_stop_clicked,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
            state="disabled")
        self._stop_btn.pack(side="left", padx=(ui_styles.SPACING["tight"], 0))

        self._pause_btn = CustomButton(
            bar, "buttons", "po_pause_btn", text="\u23f8 Pause",
            command=self._on_pause_clicked,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
            state="disabled")
        self._pause_btn.pack(side="left", padx=(ui_styles.SPACING["tight"], 0))

        options_mb, options_menu = self._add_menu(bar, "Options \u25be")
        options_mb.pack(side="right")
        scripts_mb, scripts_menu = self._add_menu(bar, "Scripts \u25be")
        scripts_mb.pack(side="right", padx=(0, ui_styles.SPACING["tight"]))

        for button_id, label in [
            ("po_export_iw38_btn", "Load IW38 Orders"),
            ("po_read_tcode", "Detect SAP Screen"),
            ("po_read_open_order", "Read Open Order"),
            ("po_open_order", "Open in IW32"),
            ("po_print_pdf", "Print to Spool"),
            ("po_export_attachments", "Read Attachments"),
            ("po_read_doknr", "Read DOKNR"),
            ("po_export_one_attachment", "Export One Attachment"),
            ("po_download_image", "SP02 PDF & Cleanup"),
        ]:
            scripts_menu.add_command(
                label=label, command=lambda bid=button_id: self._edit_script(bid))

        options_menu.add_checkbutton(
            label="Auto-merge after run", variable=self._auto_merge_var,
            command=self._save_po_settings)
        options_menu.add_checkbutton(
            label="Stop on first error", variable=self._stop_on_error_var,
            command=self._save_po_settings)
        options_menu.add_separator()
        max_menu = tk.Menu(options_menu, tearoff=0)
        for value in range(1, 7):
            max_menu.add_radiobutton(
                label=str(value), variable=self._max_attach_var, value=value,
                command=self._save_po_settings)
        options_menu.add_cascade(label="Max attachments", menu=max_menu)
        layout_menu = tk.Menu(options_menu, tearoff=0)
        for value, label in (("auto", "Auto"), ("1", "1 per page"),
                             ("2", "2 per page"), ("4", "4 per page")):
            layout_menu.add_radiobutton(
                label=label, variable=self._pdf_layout_var, value=value,
                command=self._save_po_settings)
        options_menu.add_cascade(label="PDF layout", menu=layout_menu)
        options_menu.add_separator()
        options_menu.add_command(label="Open output folder",
                                 command=self._open_output_folder)
        options_menu.add_command(label="Clear list", command=self._clear_list)

    def _build_filter_bar(self):
        bar = styled_frame(self)
        bar.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                 pady=(ui_styles.SPACING["tight"], 0))

        styled_label(bar, "Filter:", font=ui_styles.FONTS["body"],
                     bg=ui_styles.COLORS["bg"]).pack(side="left")
        self._filter_var = tk.StringVar()
        self._filter_var.trace_add("write", lambda *_: self._apply_filter())
        self._filter_entry = tk.Entry(
            bar, textvariable=self._filter_var, width=20,
            font=ui_styles.FONTS["body"])
        self._filter_entry.pack(side="left", padx=(4, 8))

        styled_label(bar, "User status:", font=ui_styles.FONTS["body"],
                     bg=ui_styles.COLORS["bg"]).pack(side="left")
        self._user_status_options: list[str] = []
        self._user_status_selected: set[str] = set()
        self._user_status_col_idx = -1
        self._user_status_popup: _UserStatusPicker | None = None
        self._user_status_btn = CustomButton(
            bar, "buttons", "po_user_status_btn", text="All",
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
            (self.STATUS_DONE, ui_styles.COLORS["success"]),
            (self.STATUS_FAILED, ui_styles.COLORS["error"]),
        ):
            label = styled_label(
                bar, f"{status} 0", font=ui_styles.FONTS["small"],
                bg=ui_styles.COLORS["bg"], fg=color)
            label.pack(side="left", padx=(ui_styles.SPACING["normal"], 0))
            self._counter_labels[status] = label

        self._history_btn = CustomButton(
            bar, "buttons", "po_history_btn", text="History (0)",
            command=self._toggle_history,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"])
        self._history_btn.pack(side="right")

        self._merge_btn = CustomButton(
            bar, "buttons", "po_generate_pdf_btn", text="Merge PDFs",
            command=self._generate_merged_pdf,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"])
        self._merge_btn.pack(side="right", padx=(0, ui_styles.SPACING["tight"]))

        self._retry_btn = CustomButton(
            bar, "buttons", "po_retry_failed_btn", text="Retry Failed",
            command=self._on_retry_failed_clicked,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"])
        self._retry_btn.pack(side="right", padx=(0, ui_styles.SPACING["tight"]))

        self._merged_link = tk.Label(
            bar, text="", font=ui_styles.FONTS["small"],
            fg=ui_styles.COLORS["primary"], bg=ui_styles.COLORS["bg"],
            cursor="hand2")
        self._merged_link.pack(side="right", padx=(0, ui_styles.SPACING["tight"]))
        self._merged_link.bind("<Button-1>", lambda e: self._open_merged())

        self._refresh_history_btn()

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

        tf = styled_frame(self)
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
        pf.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                pady=(ui_styles.SPACING["tight"], 0))
        self._prog = ttk.Progressbar(pf, orient="horizontal", mode="determinate")
        self._prog.pack(fill="x", expand=True, side="left")
        self._prog_label = styled_label(
            pf, "0 / 0", font=ui_styles.FONTS["body"], bg=ui_styles.COLORS["bg"])
        self._prog_label.pack(side="left", padx=(ui_styles.SPACING["tight"], 0))
        styled_label(
            pf, "No Release = SAP status not 'vrijgegeven' (skipped)",
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
        self._history_btn.config(text=f"History ({count})")

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
        self._processed_orders = []
        self._run_files = {}
        self._run_orders = []
        self._merged_file = None
        if self._merged_link is not None:
            self._merged_link.config(text="")

        self._rebuild_user_status_options()
        self._apply_filter()
        self._update_counters()
        self._refresh_action_state()
        self._update_empty_state()

        total = len(self._all_items)
        released = sum(
            1 for c in self._all_items if self._row_status(c) == self.STATUS_PENDING)
        if total:
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
        status_header = None
        for h in export_headers:
            if h.lower().replace(" ", "") in (
                    "status", "sysstat", "userstatus", "st", "systeemstatus",
                    "systemstatus", "objectstatus", "sttxt", "estat"):
                status_header = h
                break
        if not status_header:
            children = self.tree.get_children()
            if children:
                first_vals = list(self.tree.item(children[0], "values"))
                for i, h in enumerate(current_cols):
                    if i < len(first_vals):
                        raw = first_vals[i].lower()
                        if ("vrijgegeven" in raw or "vrij" in raw
                                or " rel " in f" {raw} "
                                or any(kw in raw for kw in (
                                    "open", "released", "crtd", "confirmed"))):
                            status_header = h
                            break
        status_idx = (
            current_cols.index(status_header)
            if status_header and status_header in current_cols else -1)

        progress_col_idx = (
            len(current_cols) if "Progress" not in current_cols
            else current_cols.index("Progress"))
        for item in self.tree.get_children():
            vals = list(self.tree.item(item, "values"))
            raw = vals[status_idx].lower() if 0 <= status_idx < len(vals) else ""
            released = "vrijgegeven" in raw or "vrij" in raw or " rel " in f" {raw} "
            progress = self.STATUS_PENDING if released else self.STATUS_NO_RELEASE
            if len(vals) > progress_col_idx:
                vals[progress_col_idx] = progress
            else:
                vals.append(progress)
            self.tree.item(item, values=vals)
            self._apply_row_visual(item, progress)

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
            text = "All"
        elif len(selected) == 1:
            text = next(iter(selected))
        else:
            text = f"{len(selected)} selected"
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
        cols = [(" ".join(str(c).split()).casefold()) for c in self.tree["columns"]]
        for name in (
            "gebr.stat.", "gebr.stat", "gebrstat", "gebruikersstatus",
            "userstatus", "user status", "ustatus",
        ):
            if name in cols:
                return cols.index(name)
        # No dedicated user-status column: prefer the combined Status column
        # over the system-status column, since it carries user-status tokens.
        for name in ("status", "systeemstatus", "systemstatus", "sttxt",
                     "objectstatus"):
            if name in cols:
                return cols.index(name)
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
            self.STATUS_DONE: 0,
            self.STATUS_FAILED: 0,
        }
        for item in self._all_items:
            status = self._row_status(item)
            if status in counts:
                counts[status] += 1
        for status, label in self._counter_labels.items():
            label.config(text=f"{status} {counts[status]}")

    def _update_empty_state(self):
        if not hasattr(self, "_empty_label"):
            return
        if self._all_items:
            if self.tree.get_children():
                self._empty_label.place_forget()
            else:
                self._empty_label.config(text="No rows match the current filter.")
                self._empty_label.place(relx=0.5, rely=0.45, anchor="center")
            return
        if self._loading:
            text = "Loading IW38 orders..."
        else:
            text = ("Open IW38 and click Load IW38 Orders, or open an order in\n"
                    "IW32 (or type its number above) and click Print This Order.")
        self._empty_label.config(text=text)
        self._empty_label.place(relx=0.5, rely=0.45, anchor="center")

    def _set_primary(self, button, primary: bool):
        if primary:
            button.config(bg=ui_styles.COLORS["primary"], fg="#ffffff")
        else:
            button.config(bg=ui_styles.COLORS["surface"],
                          fg=ui_styles.COLORS["text"])

    def _visible_pending(self) -> list[str]:
        return [c for c in self.tree.get_children()
                if self._row_status(c) in (self.STATUS_PENDING, self.STATUS_FAILED)]

    def _refresh_action_state(self):
        if not hasattr(self, "_load_btn"):
            return
        processing = self._processing
        has_rows = bool(self._all_items)
        visible_pending = self._visible_pending() if has_rows else []
        selected_pending = [
            c for c in self.tree.selection()
            if self._row_status(c) in (self.STATUS_PENDING, self.STATUS_FAILED)
        ]
        any_failed = any(
            self._row_status(c) == self.STATUS_FAILED for c in self._all_items)
        has_files = any(
            e.get("workorder") or e.get("attachments")
            for e in self._run_files.values())

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
            state="normal" if has_files and not processing else "disabled")

        if not processing and not has_rows:
            self._set_primary(self._load_btn, True)
            self._set_primary(self._print_all_btn, False)
            self._set_primary(self._single_btn, False)
        elif not processing and visible_pending:
            self._set_primary(self._load_btn, False)
            self._set_primary(self._print_all_btn, True)
            self._set_primary(self._single_btn, False)
        elif not processing:
            self._set_primary(self._load_btn, False)
            self._set_primary(self._print_all_btn, False)
            self._set_primary(self._single_btn, True)

        self._update_counters()
        self._update_empty_state()

    # ── screen detection / single order ─────────────────────────────────────
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
            self._run_export(auto=True)
        elif tcode == "IW32":
            self._read_open_order_into_field()
        elif tcode:
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
                    "Open IW38 to load orders, or open an order in IW32.", "info")

    def _read_open_order_into_field(self):
        self.engine.set_variable("po_current_order", "")
        self.run_script("po_read_open_order", title="Read Open Order",
                        on_success=self._on_detected_order_read)

    def _on_detected_order_read(self):
        order = (self.engine.get_variable("po_current_order") or "").strip()
        if order:
            self.order_var.set(order)
            self.report_status(
                f"IW32 order {order} detected - click Print This Order", "info")
        else:
            self.report_status(
                "No order open in IW32; type an order number above.", "warning")

    def _print_open_order(self):
        """Print one order: the Order field when filled, else IW32's screen."""
        if self._processing:
            return self.report_status("Stop processing first", "warning")

        order = (self.order_var.get() or "").strip()
        if order:
            return self._print_single(order)

        # Clear first: a failed read must not fall through to a stale number.
        self.engine.set_variable("po_current_order", "")
        self.report_status("Reading order open in IW32...", "info")
        self.run_script("po_read_open_order", title="Read Open Order",
                        on_success=self._on_open_order_read)

    def _on_open_order_read(self):
        order = (self.engine.get_variable("po_current_order") or "").strip()
        if not order:
            return self.report_status(
                "No order open in IW32. Type an order number above or open "
                "one in SAP.", "warning")
        self.order_var.set(order)
        self._print_single(order)

    def _print_single(self, order: str, short_text: str = ""):
        if self._processing:
            return self.report_status("Stop processing first", "warning")
        order = (order or "").strip()
        if not order:
            return self.report_status("No order number to print", "warning")
        if not short_text:
            short_text = self._text_for_order(order)
        self._begin_run([(None, order, short_text)], kind="single")

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
        self._begin_run(
            [(item, self._order_num(item), self._short_text_for(item))
             for item in items], kind="batch")

    def _on_print_selected_clicked(self):
        items = [
            c for c in self.tree.selection()
            if self._row_status(c) in (self.STATUS_PENDING, self.STATUS_FAILED)
        ]
        if not items:
            return self.report_status("Select one or more pending orders first",
                                      "warning")
        self._begin_run(
            [(item, self._order_num(item), self._short_text_for(item))
             for item in items], kind="batch")

    def _on_retry_failed_clicked(self):
        items = [c for c in self._all_items
                 if self._row_status(c) == self.STATUS_FAILED]
        if not items:
            return self.report_status("No failed orders to retry", "info")
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
        self._processed_orders = []
        self._run_files = {}
        self._run_orders = [order for _, order, _ in orders]
        self._run_kind = kind
        self._run_recorded = False
        self._merged_file = None
        if self._merged_link is not None:
            self._merged_link.config(text="")

        now = datetime.now()
        self._run_started = now.strftime("%d.%m.%Y %H:%M")
        self._run_started_day = now.strftime("%d.%m.%Y")
        self._run_dir = self._create_run_dir(self._run_orders)
        self._run_id = os.path.basename(self._run_dir)
        self.engine.set_variable(
            "po_export_path", self._run_dir.replace("\\", "\\\\"))
        self.engine.set_variable("po_img_path", self._temp_path)

        self._prog["maximum"] = self._total
        self._prog["value"] = 0
        self._prog_label.config(text=f"0 / {self._total}")
        self._pause_btn.config(text="\u23f8 Pause")
        self._refresh_action_state()
        self.report_status(f"Printing {self._total} order(s)...", "info")
        self._process_next()

    def _create_run_dir(self, orders: list[str]) -> str:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        if len(orders) == 1:
            label = f"1order_{orders[0]}"
        else:
            label = f"{len(orders)}orders_{orders[0]}-{orders[-1]}"
        safe = re.sub(r"[^\w.-]", "_", label)
        base = os.path.join(self._output_root, f"{stamp}_{safe}")
        path = base
        counter = 2
        while os.path.exists(path):
            path = f"{base}_{counter}"
            counter += 1
        os.makedirs(path, exist_ok=True)
        return path

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
        attachments = self._create_image_pdf(order, self._active_stem)
        status = self.engine.get_variable("po_print_status", "")
        ok = status == "printed"

        workorder_name = f"{self._active_stem}_workorder.pdf"
        workorder_path = os.path.join(self._run_dir, workorder_name)
        self._run_files[order] = {
            "order": order,
            "short_text": self._active_text,
            "status": self.STATUS_DONE if ok else self.STATUS_FAILED,
            "workorder": workorder_name if os.path.exists(workorder_path) else "",
            "attachments": attachments,
            "error": self._row_errors.get(order, ""),
        }
        self._mark_done(item, order, ok)
        if order not in self._processed_orders:
            self._processed_orders.append(order)

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

        if self._auto_merge_var.get():
            self._generate_merged_pdf(auto=True)
        self._record_run_history()
        self._refresh_action_state()
        self._refresh_history_btn()
        if self._history_pane is not None:
            self._history_pane.refresh()

        done = self._done_count - self._failed_count
        failed = self._failed_count
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

    def _create_image_pdf(self, order: str, stem: str) -> str:
        try:
            from PIL import Image
        except ImportError:
            return ""

        paths = []
        for i in range(self._max_attachments()):
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

        layout = self._pdf_layout_var.get()
        per = int(layout) if layout in ("1", "2", "4") else (
            1 if len(imgs) == 1 else 2 if len(imgs) == 2 else 4)
        pages = [self._compose_page(imgs[i:i + per], per)
                 for i in range(0, len(imgs), per)]

        name = f"{stem}_attachments.pdf"
        out = os.path.join(self._run_dir, name)
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

    def _generate_merged_pdf(self, auto: bool = False):
        order_ids = list(self._processed_orders) or list(self._run_orders)
        if not order_ids:
            if not auto:
                self.report_status("Nothing to merge yet", "warning")
            return

        writer = PdfWriter()
        merged_orders = 0
        for order in order_ids:
            entry = self._run_files.get(order)
            if not entry:
                continue
            files = [entry.get("workorder"), entry.get("attachments")]
            added = False
            for name in files:
                if not name:
                    continue
                path = os.path.join(self._run_dir, name)
                if not os.path.exists(path):
                    continue
                try:
                    reader = PdfReader(path)
                except Exception:
                    continue
                for page in reader.pages:
                    writer.add_page(page)
                added = True
            if added:
                merged_orders += 1
                if len(writer.pages) % 2 == 1:
                    writer.add_blank_page()

        if not writer.pages:
            if not auto:
                self.report_status("No completed PDFs to merge", "warning")
            return

        first = order_ids[0]
        last = order_ids[-1]
        name = (f"merged_{first}.pdf" if first == last
                else f"merged_{first}-{last}.pdf")
        out = os.path.join(self._run_dir, name)
        try:
            with open(out, "wb") as f:
                writer.write(f)
        except OSError as exc:
            self.report_status(f"Could not write merged PDF: {exc}", "error")
            return

        self._merged_file = out
        if self._merged_link is not None:
            self._merged_link.config(text=name)
        self._history.update_run(self._run_id, merged=name)
        if self._history_pane is not None:
            self._history_pane.refresh()
        if not auto:
            self.report_status(
                f"Merged {merged_orders} order(s) into {name}", "success")

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

    # ── history ─────────────────────────────────────────────────────────────
    def _record_run_history(self):
        if not self._run_dir or self._run_recorded:
            return
        orders = []
        for order in self._run_orders:
            entry = self._run_files.get(order)
            if entry is not None:
                orders.append(entry)
            elif order in self._processed_orders:
                orders.append({
                    "order": order, "short_text": "", "status": self.STATUS_FAILED,
                    "workorder": "", "attachments": "",
                    "error": self._row_errors.get(order, ""),
                })
        if not orders:
            return
        merged = os.path.basename(self._merged_file) if self._merged_file else ""
        run = {
            "id": self._run_id,
            "folder": self._run_dir,
            "started": self._run_started,
            "started_day": self._run_started_day,
            "kind": self._run_kind,
            "done": self._done_count - self._failed_count,
            "failed": self._failed_count,
            "merged": merged,
            "orders": orders,
        }
        self._history.add_run(run)
        self._history.prune(self.RUN_LIMIT)
        self._run_recorded = True
        self._refresh_history_btn()

    # ── row helpers ─────────────────────────────────────────────────────────
    def _short_text_for(self, item: str | None) -> str:
        if item is None:
            return ""
        values = list(self.tree.item(item, "values"))
        # Values follow the treeview's display order, not the export order.
        columns = list(self.tree["columns"])
        for i, header in enumerate(columns):
            norm = " ".join(str(header).split()).casefold()
            if norm in self.SHORT_TEXT_HEADERS and i < len(values):
                return str(values[i]).strip()
        return ""

    def _text_for_order(self, order: str) -> str:
        for item in self._all_items:
            if self._order_num(item) == order:
                return self._short_text_for(item)
        return ""

    def _order_col_idx(self) -> int:
        """Find the order-number column index in the treeview."""
        cols = list(self.tree["columns"])
        for i, c in enumerate(cols):
            if c.lower() in ("order", "aufnr", "work order", "wo"):
                return i
        return 0

    def _order_num(self, item: str) -> str:
        v = self.tree.item(item, "values")
        idx = self._order_col_idx()
        return str(v[idx]).strip() if v and idx < len(v) else ""

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
            self._pause_btn.config(text="\u23f8 Pause")
            self.report_status("Resumed", "info")
            self._process_next()
        else:
            self._paused = True
            self._pause_btn.config(text="\u25b6 Resume")
            self.report_status("Paused", "warning")

    # ── row actions ─────────────────────────────────────────────────────────
    def _pdf_for_order(self, order: str) -> str | None:
        entry = self._run_files.get(order)
        if entry:
            for kind in ("workorder", "attachments"):
                name = entry.get(kind) or ""
                path = os.path.join(self._run_dir, name)
                if name and os.path.exists(path):
                    return path
        return self._history.pdf_path(order)

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
            menu.add_command(label="Open in IW32",
                             command=lambda: self._open_order_in_iw32(order))
            menu.add_command(
                label="Print This Order",
                command=lambda o=order: self._print_single(o))
            menu.add_command(
                label="Copy Order Number",
                command=lambda o=order: self._copy_text(o))
            pdf = self._pdf_for_order(order)
            if pdf:
                menu.add_command(label="Open PDF",
                                 command=lambda p=pdf: self._open_path(p))
                menu.add_command(
                    label="Open Containing Folder",
                    command=lambda p=pdf: self._open_path(os.path.dirname(p)))
            error = self._row_errors.get(order)
            if error:
                menu.add_command(
                    label="Show Error",
                    command=lambda e=error: messagebox.showerror(
                        f"Order {order}", e, parent=self))
            menu.add_separator()
        menu.add_command(label="Configure Columns...",
                         command=self.grid_manager.configure)
        menu.add_command(label="Clear List", command=self._clear_list)
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
        self._filter_var.set("")
        self._rebuild_user_status_options()
        self._apply_filter()
        self._refresh_action_state()
        self._update_empty_state()
        if items:
            self.report_status("List cleared", "info")

    # ── temp / close ────────────────────────────────────────────────────────
    def _on_close(self):
        if self._processing:
            keep_going = messagebox.askyesno(
                "Print Orders",
                "Printing is still running.\n\nStop and close the window?",
                parent=self)
            if not keep_going:
                return
            self._stop_requested = True
        self._wipe_temp_files()
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
