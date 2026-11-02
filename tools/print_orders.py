"""PrintOrdersWindow — mass-print work orders from IW38.

Workflow:
  1. User has IW38 open → click "Export IW38 Grid"
  2. SAP exports grid as tabbed text → populates a Treeview
  3. Orders without "vrijgegeven" status are flagged "No Release"
  4. User clicks "▶ Start" → Python loops over released orders:
       a. Open IW32 with order
       b. Print work order as PDF via SAP GUI script
       c. Export attachment list from IHKD tab
       d. Download image files for that order
       e. Create filler PDF from downloaded images
  5. Pause/Resume via button
  6. "Generate PDF List" merges everything

SAP scripts are short operations chained via engine polling.
Processing stays in the main thread using after() scheduling.
"""

from __future__ import annotations

import os
import re
import tkinter as tk
from collections.abc import Callable
from datetime import datetime
from tkinter import ttk

from pypdf import PdfReader, PdfWriter

from core import config as cfg
from tools.grid_column_manager import GridColumnManager as GridManager
from ui import ui_styles
from ui.custom_widgets import BaseToolWindow, CustomButton
from ui.edit_modal import EditModal
from ui.ui_styles import styled_frame, styled_label


class PrintOrdersWindow(BaseToolWindow):
    """Batch-print work orders from IW38 with progress tracking."""

    EXPORT_FILENAME = "sap_iw38_export.txt"

    ORDER_PDF_SUFFIX = "_wo.pdf"
    IMAGES_PDF_SUFFIX = "_F.pdf"

    STATUS_PENDING = "Pending"
    STATUS_IN_PROGRESS = "In Progress"
    STATUS_DONE = "Done"
    STATUS_FAILED = "Failed"
    STATUS_NO_RELEASE = "No Release"

    def __init__(self, parent, engine):
        super().__init__(parent, engine, titlebar_color=ui_styles.TOOL_COLORS["print_orders"])
        self.title("Print Orders")
        self.geometry("960x720")
        self.minsize(640, 480)

        self._init_paths()

        self._processing = False
        self._paused = False
        self._stop_requested = False
        self._done_count = 0
        self._failed_count = 0
        self._total = 0
        self._processed_orders: list[str] = []
        self._merged_file: str | None = None
        self._merged_link: tk.Label | None = None

        self._build_export_bar()
        self._build_status_bar()
        self._build_treeview()
        self._build_progress_bar()
        self._build_controls()

        self._cache_scripts()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._show()

    # ── paths ───────────────────────────────────────────────────────────────
    def _init_paths(self):
        base = os.path.dirname(os.path.dirname(__file__))
        self._export_path = os.path.join(base, "tempexport")
        os.makedirs(self._export_path, exist_ok=True)
        self._export_file = os.path.join(self._export_path, self.EXPORT_FILENAME)
        escaped = self._export_path.replace("\\", "\\\\")
        self.engine.set_variable("po_export_path", escaped)
        self.engine.set_variable("po_export_file", self.EXPORT_FILENAME)

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
    def _build_export_bar(self):
        bar = styled_frame(self)
        bar.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                 pady=(ui_styles.SPACING["comfortable"], 0))

        self._export_btn = CustomButton(
            bar, "buttons", "po_export_iw38_btn",
            text="Export IW38 Grid",
            command=self._run_export,
            bg=ui_styles.COLORS["primary"], fg="#ffffff",
        )
        self._export_btn.pack(side="left")

        # One order already open in IW32: no IW38 export needed. The order
        # number is read from the open screen (see po_read_open_order).
        self._single_btn = CustomButton(
            bar, "buttons", "po_print_open_order_btn",
            text="Print Open Order",
            command=self._print_open_order,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
        )
        self._single_btn.pack(side="left", padx=(ui_styles.SPACING["tight"], 0))

        self._count_label = styled_label(
            bar, "Orders: 0", font=ui_styles.FONTS["body"], bg=ui_styles.COLORS["bg"])
        self._count_label.pack(side="left", padx=(ui_styles.SPACING["normal"], 0))

        self._release_label = styled_label(
            bar, "Released: 0", font=ui_styles.FONTS["body"], bg=ui_styles.COLORS["bg"],
            fg=ui_styles.COLORS["success"])
        self._release_label.pack(side="left")

    STATUS_TAGS = {
        STATUS_PENDING: "po_pending",
        STATUS_IN_PROGRESS: "po_in_progress",
        STATUS_DONE: "po_done",
        STATUS_FAILED: "po_failed",
        STATUS_NO_RELEASE: "po_no_release",
    }

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

        self.tree = self.CustomTreeview(
            tf, component_type="datagridview", button_id="po_orders_grid",
            style="PO_TV.Treeview")
        # manage_scrollbar=False: the bar below is owned here, packed
        # directly under the grid so the packer can't squeeze it out on
        # short windows (same failure as the PRH grid had).
        self.grid_manager = GridManager(
            self.tree, "po_orders_grid", self.config_data, manage_scrollbar=False
        )
        self.grid_manager.status_label = self.status_label
        self.grid_manager.post_apply_callback = self._add_status_column
        self.tree["show"] = "headings"
        self.tree.tag_configure("po_done", foreground=ui_styles.COLORS["success"])
        self.tree.tag_configure("po_failed", foreground=ui_styles.COLORS["error"])
        self.tree.tag_configure("po_no_release", foreground=ui_styles.COLORS["text_secondary"])
        self.tree.tag_configure("po_in_progress", foreground=ui_styles.COLORS["primary"])
        self.tree.tag_configure("po_pending", foreground=ui_styles.COLORS["text"])
        self._grid_h_scroll = ttk.Scrollbar(tf, orient="horizontal", command=self.tree.xview)
        # Bar packed FIRST at the bottom: it keeps its height while the
        # expanding tree absorbs any shortage (packing it last lets the
        # packer squeeze it to zero on short windows).
        self._grid_h_scroll.pack(side="bottom", fill="x")
        self.tree.pack(side="top", fill="both", expand=True)
        self.tree.configure(xscrollcommand=self._grid_h_scroll.set)

    def _build_progress_bar(self):
        pf = styled_frame(self)
        pf.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                pady=(ui_styles.SPACING["tight"], 0))
        self._prog = ttk.Progressbar(pf, orient="horizontal", mode="determinate")
        self._prog.pack(fill="x", expand=True, side="left")
        self._prog_label = styled_label(
            pf, "0 / 0", font=ui_styles.FONTS["body"], bg=ui_styles.COLORS["bg"])
        self._prog_label.pack(side="left", padx=(ui_styles.SPACING["tight"], 0))

    def _build_controls(self):
        cf = styled_frame(self)
        cf.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                pady=(ui_styles.SPACING["normal"], ui_styles.SPACING["comfortable"]))

        self._start_btn = CustomButton(
            cf, "buttons", "po_start_btn", text="\u25b6 Start",
            command=self._on_start_clicked,
            bg=ui_styles.COLORS["primary"], fg="#ffffff")
        self._start_btn.pack(side="left", padx=(0, ui_styles.SPACING["tight"]))

        self._pause_btn = CustomButton(
            cf, "buttons", "po_pause_btn", text="\u23f8 Pause",
            command=self._on_pause_clicked,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
            state="disabled")
        self._pause_btn.pack(side="left", padx=(0, ui_styles.SPACING["tight"]))

        self._gen_btn = CustomButton(
            cf, "buttons", "po_generate_pdf_btn", text="Generate PDF List",
            command=self._generate_merged_pdf,
            bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"])
        self._gen_btn.pack(side="left")

        self._merged_link = tk.Label(
            cf, text="", font=ui_styles.FONTS["small"],
            fg=ui_styles.COLORS["primary"], bg=ui_styles.COLORS["surface"],
            cursor="hand2")
        self._merged_link.pack(side="left", padx=(8, 0))
        self._merged_link.bind("<Button-1>", lambda e: self._open_merged())

        sf = styled_frame(self)
        sf.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                pady=(0, ui_styles.SPACING["tight"]))
        scripts_label = styled_label(sf, "Scripts:", font=ui_styles.FONTS["small"],
                                     bg=ui_styles.COLORS["bg"])
        scripts_label.pack(side="left", padx=(0, ui_styles.SPACING["tight"]))

        for _id, _label in [("po_read_open_order", "Read Open"),
                             ("po_open_order", "Open"), ("po_print_pdf", "Print"),
                             ("po_export_attachments", "Attach"),
                             ("po_export_one_attachment", "Export1"),
                             ("po_read_doknr", "DOKNR"),
                             ("po_download_image", "Download")]:
            btn = tk.Button(
                sf, text=_label,
                command=lambda bid=_id: self._edit_script(bid),
                font=ui_styles.FONTS["small"],
                bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
                relief="flat", padx=8, pady=1,
                cursor="hand2")
            btn.pack(side="left", padx=(0, ui_styles.SPACING["tight"]))

    def _build_status_bar(self, text: str = "Ready"):
        bar, self.status_label = super()._build_status_bar(text)
        bar.pack(fill="x", padx=2, pady=2)
        return bar, self.status_label

    # ── export ──────────────────────────────────────────────────────────────
    def _run_export(self):
        if self._processing:
            return self.report_status("Stop processing first", "warning")
        self._wipe_temp_files()
        self._set_export_ui(disabled=True)
        self.report_status("Exporting IW38 grid...", "info")
        self.run_script("po_export_iw38_btn", on_success=self._on_export_done)

    def _on_export_done(self):
        if self.engine.get_variable("export_done") != "1":
            self.report_status("Export returned no data", "error")
            return self._set_export_ui(disabled=False)
        if not os.path.exists(self._export_file):
            self.report_status("Export file not found", "error")
            return self._set_export_ui(disabled=False)

        self.grid_manager.populate_from_export(self._export_file)
        self._add_status_column()

        children = self.tree.get_children()
        total = len(children)
        released = sum(1 for c in children if self._row_status(c) == self.STATUS_PENDING)
        self._count_label.config(text=f"Orders: {total}")
        self._release_label.config(text=f"Released: {released}")
        self._set_export_ui(disabled=False)
        self._start_btn.config(state="normal")
        self.report_status(f"{total} orders loaded ({released} released)", "success")

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

        # Find the SAP status column by name in export headers, then locate it
        # in the treeview columns (values are in treeview column order now).
        export_headers = self.grid_manager.available_columns or []
        status_header = None
        for h in export_headers:
            if h.lower().replace(" ", "") in ("status", "sysstat", "userstatus", "st", "systeemstatus", "systemstatus", "objectstatus", "sttxt", "estat"):
                status_header = h
                break
        if not status_header:
            children = self.tree.get_children()
            if children:
                first_vals = list(self.tree.item(children[0], "values"))
                for i, h in enumerate(current_cols):
                    if i < len(first_vals):
                        raw = first_vals[i].lower()
                        if "vrijgegeven" in raw or "vrij" in raw or " rel " in f" {raw} " or any(kw in raw for kw in ("open", "released", "crtd", "confirmed")):
                            status_header = h
                            break
        status_idx = current_cols.index(status_header) if status_header and status_header in current_cols else -1

        progress_col_idx = len(current_cols) if "Progress" not in current_cols else current_cols.index("Progress")
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

    def _set_export_ui(self, disabled: bool):
        state = "disabled" if disabled else "normal"
        self._export_btn.config(state=state)
        if disabled:
            self._start_btn.config(state="disabled")
            self._single_btn.config(state="disabled")
            self._gen_btn.config(state="disabled")
        elif not self._processing:
            self._single_btn.config(state="normal")
            self._gen_btn.config(state="normal")
            # start button enabled only if there are orders
            if self.tree.get_children():
                self._start_btn.config(state="normal")

    # ── processing ──────────────────────────────────────────────────────────
    def _on_start_clicked(self):
        if self._processing:
            self._stop_requested = True
            self.report_status("Stopping after current step...", "warning")
            return

        items = [c for c in self.tree.get_children()
                 if self._row_status(c) in (self.STATUS_PENDING, self.STATUS_FAILED)]
        if not items:
            return self.report_status("No pending orders", "warning")

        self._begin_run([(item, self._order_num(item)) for item in items])

    def _print_open_order(self):
        """Print the one order already open in IW32, without an IW38 export.

        The order number is read off the open screen first; the regular
        per-order pipeline then prints, collects attachments and builds the
        filler PDF, exactly as it does for a batch row.
        """
        if self._processing:
            return self.report_status("Stop processing first", "warning")

        # Clear it first: a failed read must not fall through to a stale
        # order number left over from the previous batch.
        self.engine.set_variable("po_current_order", "")
        self.report_status("Reading order open in IW32...", "info")
        self.run_script("po_read_open_order", title="Read Open Order",
                        on_success=self._on_open_order_read)

    def _on_open_order_read(self):
        order = (self.engine.get_variable("po_current_order") or "").strip()
        if not order:
            return self.report_status(
                "No order open in IW32 (open one, or use Export IW38 Grid)",
                "warning",
            )
        self._begin_run([(None, order)])

    def _begin_run(self, orders: list[tuple[str | None, str]]):
        self._processing = True
        self._paused = False
        self._stop_requested = False
        self._pending_items = orders
        self._total = len(orders)
        self._done_count = 0
        self._failed_count = 0
        self._processed_orders = []

        self._start_btn.config(text="\u25a0 Stop")
        self._pause_btn.config(state="normal", text="\u23f8 Pause")
        self._export_btn.config(state="disabled")
        self._single_btn.config(state="disabled")
        self._gen_btn.config(state="disabled")
        self._prog["maximum"] = self._total
        self._prog["value"] = 0
        self._prog_label.config(text=f"0 / {self._total}")

        self._process_next()

    def _process_next(self):
        if self._stop_requested:
            return self._done()
        if self._paused:
            return self.after(200, self._process_next)
        if not self._pending_items:
            return self._done()

        item, order = self._pending_items.pop(0)
        if not order:
            self._set_status(item, self.STATUS_FAILED)
            self._done_count += 1
            self._failed_count += 1
            self._update_progress()
            return self.after(50, self._process_next)

        self._set_status(item, self.STATUS_IN_PROGRESS)
        self._update_progress()
        self.update_idletasks()

        self.engine.set_variable("po_current_order", order)
        self.engine.set_variable("po_pdf_filename", f"{order}_wo.pdf")
        self.engine.set_variable("po_attach_filename", f"{order}_att.txt")
        self.engine.set_variable("po_img_path", self._export_path)
        self.engine.set_variable("po_img_basename", order)
        self.engine.set_variable("po_print_status", "")
        self.report_status(f"Opening {order}...", "info")
        self._run_script_and_wait(
            self._scripts["open"],
            lambda: self._on_step(item, order, 0),
        )

    STEPS = ["open", "print", "attach", "spool"]

    MAX_ATTACHMENTS = 4

    def _on_step(self, item: str | None, order: str, step_idx: int):
        if self._stop_requested:
            return self._mark_done(item, order, False)

        if step_idx > 0 and self.engine._last_error:
            err = self.engine._last_error
            self.report_status(f"{order}: Step failed, skipping ({err[:80]})", "error")
            return self._finish_order(item, order)

        if step_idx >= len(self.STEPS):
            return self._finish_order(item, order)

        key = self.STEPS[step_idx]
        labels = {"open": "Opened", "print": "Printed to spool", "attach": "Attachments exported",
                  "spool": "PDF saved, spool deleted"}
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

    def _read_next_doknr(self):
        if (self._doknr_read_idx >= self._row_count
                or len(self._pending_attach_rows) >= self.MAX_ATTACHMENTS):
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
            self.report_status(f"{self._order}: No attachments found", "info")
            return self._on_step(self._item, self._order, self._attach_next_step)

        self._attach_export_idx = 0
        self.report_status(
            f"{self._order}: Exporting {count} unique attachment(s)...", "info"
        )
        self._export_next_attachment()

    def _export_next_attachment(self):
        if self._attach_export_idx >= len(self._pending_attach_rows):
            return self._on_step(self._item, self._order, self._attach_next_step)

        row_idx = self._pending_attach_rows[self._attach_export_idx]
        save_name = f"{self._order}_{self._attach_export_idx + 1}.bin"
        save_path = os.path.join(self._export_path, save_name)
        escaped = save_path.replace("\\", "\\\\")

        self.engine.set_variable("po_attach_row_idx", str(row_idx))
        self.engine.set_variable("po_attach_target", escaped)

        self.report_status(
            f"{self._order}: Attachment {self._attach_export_idx + 1}"
            f"/{len(self._pending_attach_rows)} (row {row_idx})",
            "info",
        )
        self._run_script_and_wait(
            self._scripts["attach_export_one"],
            lambda: self._on_attachment_exported(),
        )

    def _on_attachment_exported(self):
        self._attach_export_idx += 1
        self._export_next_attachment()

    def _finish_order(self, item: str | None, order: str):
        self._create_image_pdf(order)
        status = self.engine.get_variable("po_print_status", "")
        ok = status == "printed"
        self._mark_done(item, order, ok)
        if order not in self._processed_orders:
            self._processed_orders.append(order)

    def _mark_done(self, item: str | None, order: str, ok: bool):
        self._set_status(item, self.STATUS_DONE if ok else self.STATUS_FAILED)
        self._done_count += 1
        if not ok:
            self._failed_count += 1
        self._update_progress()
        self.report_status(
            f"Order {order}: {'Done' if ok else 'Failed'}",
            "success" if ok else "error",
        )
        self.after(50, self._process_next)

    def _done(self):
        self._processing = False
        self._paused = False
        self._stop_requested = False
        self._start_btn.config(text="\u25b6 Start")
        self._pause_btn.config(state="disabled", text="\u23f8 Pause")
        self._export_btn.config(state="normal")
        self._single_btn.config(state="normal")
        self._gen_btn.config(state="normal")

        done = self._done_count - self._failed_count
        failed = self._failed_count
        if failed:
            self.report_status(f"{done} done, {failed} failed — re-run Start to retry", "warning")
        else:
            self.report_status(f"All {done} orders done", "success")

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

    def _wait_for_idle(self, on_done: Callable, max_wait: float = 120.0):
        if self._stop_requested:
            return on_done()
        e = self.engine
        commands_done = not e.commands or e.pointer >= len(e.commands)
        stopped = not (e._run_immediate_mode or e._continue_mode)
        if not e.is_busy and (commands_done or stopped):
            return on_done()
        self.after(100, lambda: self._wait_for_idle(on_done, max_wait))

    # ── image PDF ───────────────────────────────────────────────────────────
    def _create_image_pdf(self, order: str):
        try:
            from PIL import Image
        except ImportError:
            return

        paths = []
        for i in range(4):
            p = os.path.join(self._export_path, f"{order}_{i + 1}.bin")
            if os.path.exists(p):
                paths.append(p)

        if not paths:
            return

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

        page_w, page_h = 595, 842
        n = len(imgs)
        page = Image.new("RGB", (page_w, page_h), "white")

        if n == 1:
            imgs[0].thumbnail((page_w - 40, page_h - 40))
            x = (page_w - imgs[0].width) // 2
            y = (page_h - imgs[0].height) // 2
            page.paste(imgs[0], (x, y))
        elif n == 2:
            ch = (page_h - 60) // 2
            for i, img in enumerate(imgs):
                img.thumbnail((page_w - 40, ch))
                x = (page_w - img.width) // 2
                y = i * (page_h // 2) + ((page_h // 2) - img.height) // 2
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

        out = os.path.join(self._export_path, f"{order}{self.IMAGES_PDF_SUFFIX}")
        page.save(out, "PDF")

    # ── helpers ─────────────────────────────────────────────────────────────
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
        return v[idx].strip() if v and idx < len(v) else ""

    def _row_status(self, item: str) -> str:
        v = self.tree.item(item, "values")
        return v[-1].strip() if v else self.STATUS_PENDING

    def _set_status(self, item: str | None, s: str):
        if item is None:
            return
        v = list(self.tree.item(item, "values"))
        if v:
            v[-1] = s
            self.tree.item(item, values=v)
        self._apply_row_visual(item, s)

    def _apply_row_visual(self, item: str | None, status: str):
        if item is None:
            return
        tag = self.STATUS_TAGS.get(status, "po_pending")
        self.tree.item(item, tags=(tag,))

    def _update_progress(self):
        self._prog["value"] = self._done_count
        self._prog_label.config(text=f"{self._done_count} / {self._total}")

    def _on_pause_clicked(self):
        if self._paused:
            self._paused = False
            self._pause_btn.config(text="\u23f8 Pause")
            self.report_status("Resumed", "info")
            self._process_next()
        else:
            self._paused = True
            self._pause_btn.config(text="\u25b6 Resume")
            self.report_status("Paused", "warning")

    def _on_close(self):
        keep = os.path.basename(self._merged_file) if self._merged_file else None
        for f in os.listdir(self._export_path):
            if keep and f == keep:
                continue
            try:
                os.remove(os.path.join(self._export_path, f))
            except Exception:
                pass
        self.destroy()

    def _wipe_temp_files(self):
        if os.path.exists(self._export_file):
            os.remove(self._export_file)
        for f in os.listdir(self._export_path):
            if f.lower().endswith((".pdf", ".bin", ".jpg", ".png", ".jpeg", ".gif")):
                try:
                    os.remove(os.path.join(self._export_path, f))
                except Exception:
                    pass

    def _open_merged(self):
        if self._merged_file and os.path.exists(self._merged_file):
            os.startfile(self._merged_file)

    # ── merged PDF ──────────────────────────────────────────────────────────
    def _generate_merged_pdf(self):
        pdf_dir = self._export_path
        all_pdfs = [f for f in os.listdir(pdf_dir) if f.lower().endswith(".pdf")]
        if not all_pdfs:
            return self.report_status("No PDFs to merge", "warning")

        pat = re.compile(r"(\d+)(_wo|_F|_img\d+)?\.pdf", re.IGNORECASE)
        groups: dict[str, dict[str, list[str]]] = {}
        for fn in all_pdfs:
            m = pat.match(fn)
            if not m:
                continue
            oid = m.group(1)
            suf = m.group(2) or "_wo"
            if oid not in groups:
                groups[oid] = {"wo": [], "pics": []}
            if suf == "_wo":
                groups[oid]["wo"].append(fn)
            else:
                groups[oid]["pics"].append(fn)

        for g in groups.values():
            g["pics"].sort()

        order_ids = [self._order_num(item) for item in self.tree.get_children()]
        if not order_ids:
            # Single-order print has no grid row; merge what this run produced.
            order_ids = list(self._processed_orders)

        writer = PdfWriter()
        for oid in order_ids:
            if oid not in groups:
                continue
            g = groups[oid]
            if not g["wo"]:
                continue
            try:
                reader = PdfReader(os.path.join(pdf_dir, g["wo"][0]))
            except Exception:
                continue

            for p in reader.pages:
                writer.add_page(p)

            for pf in g["pics"]:
                try:
                    pr = PdfReader(os.path.join(pdf_dir, pf))
                except Exception:
                    continue
                for p in pr.pages:
                    writer.add_page(p)

            if len(writer.pages) % 2 == 1:
                writer.add_blank_page()

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"merged_output_{ts}.pdf"
        out = os.path.join(pdf_dir, name)
        with open(out, "wb") as f:
            writer.write(f)

        self._merged_file = out
        self._merged_link.config(text=name)

        count = len([g for g in groups.values() if g["wo"]])
        self.report_status(f"Merged {count} order(s)", "success")



