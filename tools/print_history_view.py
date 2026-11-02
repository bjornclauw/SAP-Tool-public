"""PrintHistoryPane -- searchable run history for the Print Orders window.

Each run is a folder with the work-order PDFs, attachment PDFs and merged
PDF it produced. Runs are grouped under their start day; a filter box matches
every stored field. Double-click opens the existing PDF, right-click adds
Open Folder / Copy Path / Re-print via SAP.

Pure view over PrintHistoryStore -- no engine access here.
"""

from __future__ import annotations

import os
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ui import ui_styles
from ui.ui_styles import styled_frame, styled_label


def _run_blob(run: dict) -> str:
    parts = [
        str(run.get("id", "")),
        str(run.get("started", "")),
        str(run.get("started_day", "")),
        str(run.get("merged", "")),
    ]
    for entry in run.get("orders", []) or []:
        parts.extend(str(entry.get(k, "")) for k in
                     ("order", "short_text", "status", "workorder",
                      "attachments", "error"))
    return " ".join(parts).lower()


def _order_blob(entry: dict) -> str:
    return " ".join(
        str(entry.get(k, "")) for k in
        ("order", "short_text", "status", "workorder", "attachments", "error")
    ).lower()


class PrintHistoryPane:
    """Searchable history tree. Caller owns packing ``self.frame``."""

    def __init__(self, parent: tk.Misc, store,
                 on_reprint: Callable[[str, str], None]):
        self._store = store
        self._on_reprint = on_reprint
        self._payload: dict[str, tuple[str, dict, dict | None]] = {}
        self._empty_iid: str | None = None

        container = styled_frame(parent)
        self.frame = container

        top = styled_frame(container)
        top.pack(fill="x", pady=(0, ui_styles.SPACING["tight"]))

        self._header = styled_label(
            top, "History", font=ui_styles.FONTS["header"],
            bg=ui_styles.COLORS["bg"])
        self._header.pack(side="left")
        styled_label(
            top, "latest runs", font=ui_styles.FONTS["small"],
            fg=ui_styles.COLORS["text_secondary"],
            bg=ui_styles.COLORS["bg"]).pack(side="left", padx=(6, 0))

        self._clear_search_btn = ui_styles.styled_button(
            top, "Clear", width=6, pady=1, command=self._clear_search)
        self._clear_search_btn.pack(side="right", padx=(4, 0))
        self._search_var = tk.StringVar()
        self._search_entry = tk.Entry(
            top, textvariable=self._search_var, font=ui_styles.FONTS["body"],
            width=20)
        self._search_entry.pack(side="right")
        self._search_entry.bind("<KeyRelease>", lambda e: self.refresh())
        self._search_entry.bind("<Escape>", lambda e: self._clear_search())
        styled_label(top, "Search:", font=ui_styles.FONTS["body"],
                     bg=ui_styles.COLORS["bg"]).pack(side="right", padx=(0, 4))

        style = ttk.Style()
        style.configure(
            "PrintHist.Treeview", font=ui_styles.FONTS["body"],
            background=ui_styles.COLORS["surface"],
            fieldbackground=ui_styles.COLORS["surface"])

        self.tree = ttk.Treeview(
            container, columns=("details", "info"), show="tree headings",
            style="PrintHist.Treeview", height=7)
        self.tree.heading("#0", text="Run / Order")
        self.tree.heading("details", text="Details")
        self.tree.heading("info", text="When")
        self.tree.column("#0", width=170, minwidth=90, stretch=False)
        self.tree.column("details", width=340, minwidth=120, stretch=True)
        self.tree.column("info", width=150, minwidth=90, stretch=False)

        self._v_scroll = ttk.Scrollbar(
            container, orient="vertical", command=self.tree.yview)
        self._h_scroll = ttk.Scrollbar(
            container, orient="horizontal", command=self.tree.xview)
        self._v_scroll.pack(side="right", fill="y")
        self._h_scroll.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True)
        self.tree.configure(yscrollcommand=self._v_scroll.set,
                            xscrollcommand=self._h_scroll.set)

        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-3>", self._on_right_click)

        body = ui_styles.FONTS["body"]
        self.tree.tag_configure(
            "day", font=(body[0], body[1], "bold"),
            foreground=ui_styles.COLORS["primary"])
        self.tree.tag_configure("run", font=(body[0], body[1], "bold"))
        self.tree.tag_configure("failed", foreground=ui_styles.COLORS["error"])

        self.refresh()

    # ---- view --------------------------------------------------------
    def _clear_search(self) -> None:
        if self._search_var.get():
            self._search_var.set("")
            self.refresh()
        self._search_entry.focus_set()

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._payload = {}
        self._empty_iid = None

        query = (self._search_var.get() or "").strip().lower()
        runs = self._store.runs
        shown_runs = 0
        day_iid = None
        last_day = None
        for run in runs:
            run_matches = not query or query in _run_blob(run)
            orders = [
                e for e in (run.get("orders", []) or [])
                if run_matches or query in _order_blob(e)
            ]
            if query and not run_matches and not orders:
                continue

            day = run.get("started_day") or run.get("started", "")[:10] or "—"
            if day != last_day:
                day_iid = self.tree.insert(
                    "", "end", text=day, values=("", ""), tags=("day",))
                last_day = day
            shown_runs += 1

            done = run.get("done", 0)
            failed = run.get("failed", 0)
            summary = f"{done} done"
            if failed:
                summary += f", {failed} failed"
            time_part = (run.get("started", "") or "").split(" ")[-1]
            run_iid = self.tree.insert(
                day_iid or "", "end",
                text=f"{time_part} · {run.get('kind', 'run')}",
                values=(f"{len(run.get('orders', []) or [])} order(s), {summary}",
                        run.get("started") or ""),
                tags=("run",), open=True,
            )
            self._payload[run_iid] = ("run", run, None)

            for entry in orders:
                status = entry.get("status", "")
                details = " · ".join(
                    p for p in (
                        entry.get("short_text", ""),
                        status,
                        entry.get("workorder", ""),
                        entry.get("attachments", ""),
                        entry.get("error", ""),
                    ) if p)
                tags = ("order",) if status != "Failed" else ("order", "failed")
                child = self.tree.insert(
                    run_iid, "end",
                    text=str(entry.get("order", "")),
                    values=(details, run.get("started") or ""),
                    tags=tags)
                self._payload[child] = ("order", run, entry)

        total = len(runs)
        if query:
            self._header.config(text=f"History ({shown_runs} of {total} runs)")
        else:
            self._header.config(text=f"History ({total} runs)")
        self._clear_search_btn.config(
            state="normal" if query else "disabled")
        if not runs:
            self._empty_iid = self.tree.insert(
                "", "end", text="No print history yet",
                values=("Completed runs appear here", ""),
                tags=("day",))

    # ---- actions -----------------------------------------------------
    def _open_pdf(self, run: dict, entry: dict | None) -> None:
        if entry is not None:
            for kind in ("workorder", "attachments"):
                name = entry.get(kind) or ""
                path = os.path.join(run.get("folder", ""), name)
                if name and os.path.exists(path):
                    self._startfile(path)
                    return
        self._open_folder(run)

    def _open_folder(self, run: dict) -> None:
        folder = run.get("folder", "")
        if folder and os.path.isdir(folder):
            self._startfile(folder)

    def _startfile(self, path: str) -> None:
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError:
            pass

    def _copy(self, text: str) -> None:
        self.tree.clipboard_clear()
        self.tree.clipboard_append(text)

    def _selected_payload(self):
        sel = self.tree.selection()
        return self._payload.get(sel[0]) if sel else None

    def _on_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        payload = self._payload.get(iid)
        if payload is None:
            return
        kind, run, entry = payload
        self._open_pdf(run, entry)

    def _on_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        payload = self._payload.get(iid)
        if payload is None:
            return
        self.tree.selection_set(iid)
        kind, run, entry = payload
        menu = tk.Menu(self.tree, tearoff=0)
        menu.add_command(label="Open PDF",
                         command=lambda: self._open_pdf(run, entry))
        menu.add_command(label="Open Folder",
                         command=lambda: self._open_folder(run))
        if entry is not None:
            path = os.path.join(run.get("folder", ""),
                                entry.get("workorder") or "")
            menu.add_command(label="Copy Path",
                             command=lambda p=path: self._copy(p))
            menu.add_separator()
            order = str(entry.get("order", ""))
            text = entry.get("short_text", "")
            menu.add_command(
                label="Re-print via SAP",
                command=lambda o=order, t=text: self._on_reprint(o, t))
        menu.post(event.x_root, event.y_root)
