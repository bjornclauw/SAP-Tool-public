"""PrintHistoryPane -- searchable run history for the Process Orders window.

Only final artifacts are shown: one combined PDF per single-order print, and
one merged PDF per batch. Runs are grouped under their start day; a filter box
matches every stored field. Double-click opens the existing PDF, right-click
adds Open Folder / Copy Path / Process again via SAP.

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
        str(run.get(k, "")) for k in
        ("id", "started", "started_day", "kind", "file", "merged",
         "order", "short_text", "status", "error")
    ]
    for entry in run.get("orders", []) or []:
        parts.extend(str(entry.get(k, "")) for k in
                     ("order", "short_text", "status", "error"))
    return " ".join(parts).lower()


def _order_blob(entry: dict) -> str:
    return " ".join(
        str(entry.get(k, "")) for k in
        ("order", "short_text", "status", "error")
    ).lower()


class PrintHistoryPane:
    """Searchable history tree. Caller owns packing ``self.frame``."""

    def __init__(self, parent: tk.Misc, store,
                 on_reprint: Callable[[str, str], None]):
        self._store = store
        self._on_reprint = on_reprint
        self._payload: dict[str, tuple[str, dict, dict | None]] = {}

        container = styled_frame(parent)
        self.frame = container

        top = styled_frame(container)
        top.pack(fill="x", pady=(0, ui_styles.SPACING["tight"]))

        self._header = styled_label(
            top, "History", font=ui_styles.FONTS["header"],
            bg=ui_styles.COLORS["bg"])
        self._header.pack(side="left")
        styled_label(
            top, "latest results", font=ui_styles.FONTS["small"],
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
            container, columns=("description", "when"), show="tree headings",
            style="PrintHist.Treeview", height=7)
        self.tree.heading("#0", text="Result")
        self.tree.heading("description", text="Description")
        self.tree.heading("when", text="When")
        self.tree.column("#0", width=230, minwidth=120, stretch=False)
        self.tree.column("description", width=380, minwidth=140, stretch=True)
        self.tree.column("when", width=140, minwidth=90, stretch=False)

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
        self.tree.tag_configure("result", font=(body[0], body[1], "bold"))
        self.tree.tag_configure("failed", foreground=ui_styles.COLORS["error"])

        self.refresh()

    # ---- view --------------------------------------------------------
    def _clear_search(self) -> None:
        if self._search_var.get():
            self._search_var.set("")
            self.refresh()
        self._search_entry.focus_set()

    @staticmethod
    def _stored_file(run: dict) -> str:
        name = run.get("file") or run.get("merged") or ""
        if not name:
            for entry in run.get("orders", []) or []:
                for key in ("workorder", "attachments"):
                    if entry.get(key):
                        name = entry[key]
                        break
                if name:
                    break
        return name

    def _insert_run(self, day_iid: str, run: dict, orders: list[dict],
                    query: str) -> None:
        kind = run.get("kind") or ("single" if run.get("order") else "batch")
        stored = self._stored_file(run)
        when = run.get("started") or ""
        run_matches = not query or query in _run_blob(run)

        if kind == "single":
            first = orders[0] if orders else {}
            order = str(run.get("order") or first.get("order", ""))
            short = str(run.get("short_text") or first.get("short_text", ""))
            status = str(run.get("status") or first.get("status", ""))
            if query and not run_matches:
                return
            label = f"{order} · {short}" if short else order
            details = " · ".join(p for p in (status, stored) if p)
            tags = ("result",) if status != "Failed" else ("result", "failed")
            iid = self.tree.insert(
                day_iid, "end", text=label,
                values=(details, when), tags=tags)
            self._payload[iid] = ("single", run, None)
            return

        done = run.get("done", 0)
        failed = run.get("failed", 0)
        visible_orders = [
            e for e in orders if run_matches or query in _order_blob(e)
        ]
        if query and not run_matches and not visible_orders:
            return
        summary = f"{done} done"
        if failed:
            summary += f", {failed} failed"
        iid = self.tree.insert(
            day_iid, "end",
            text=f"Batch · {len(orders) or done} order(s)",
            values=(f"{summary} · {stored}" if stored else summary, when),
            tags=("result",), open=bool(query))
        self._payload[iid] = ("batch", run, None)
        for entry in visible_orders:
            status = str(entry.get("status", ""))
            short = str(entry.get("short_text", ""))
            details = " · ".join(p for p in (short, status) if p)
            tags = ("order",) if status != "Failed" else ("order", "failed")
            child = self.tree.insert(
                iid, "end", text=str(entry.get("order", "")),
                values=(details, when), tags=tags)
            self._payload[child] = ("order", run, entry)

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._payload = {}

        query = (self._search_var.get() or "").strip().lower()
        runs = self._store.runs
        shown_runs = 0
        day_iid = None
        last_day = None
        for run in runs:
            run_matches = not query or query in _run_blob(run)
            orders = run.get("orders", []) or []
            if query and not run_matches and not any(
                    query in _order_blob(e) for e in orders):
                continue

            day = run.get("started_day") or run.get("started", "")[:10] or "—"
            if day != last_day:
                day_iid = self.tree.insert(
                    "", "end", text=day, values=("", ""), tags=("day",))
                last_day = day
            shown_runs += 1
            self._insert_run(day_iid or "", run, orders, query)

        total = len(runs)
        if query:
            self._header.config(text=f"History ({shown_runs} of {total} runs)")
        else:
            self._header.config(text=f"History ({total} runs)")
        self._clear_search_btn.config(
            state="normal" if query else "disabled")
        if not runs:
            self.tree.insert(
                "", "end", text="No history yet",
                values=("Completed results appear here", ""),
                tags=("day",))

    # ---- actions -----------------------------------------------------
    def _pdf_path(self, run: dict) -> str | None:
        name = self._stored_file(run)
        if not name:
            return None
        path = os.path.join(run.get("folder", ""), name)
        return path if os.path.exists(path) else None

    def _open_pdf(self, run: dict) -> None:
        path = self._pdf_path(run)
        if path:
            self._startfile(path)
        else:
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

    def _on_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        payload = self._payload.get(iid)
        if payload is None:
            return
        _kind, run, _entry = payload
        self._open_pdf(run)

    def _on_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        payload = self._payload.get(iid)
        if payload is None:
            return
        self.tree.selection_set(iid)
        kind, run, entry = payload
        menu = tk.Menu(self.tree, tearoff=0)
        menu.add_command(label="Open PDF", command=lambda: self._open_pdf(run))
        menu.add_command(label="Open Folder",
                         command=lambda: self._open_folder(run))
        path = self._pdf_path(run)
        if path:
            menu.add_command(label="Copy Path",
                             command=lambda p=path: self._copy(p))
        order = ""
        text = ""
        if kind == "single":
            first = (run.get("orders", []) or [{}])[0]
            order = str(run.get("order") or first.get("order", ""))
            text = str(run.get("short_text") or first.get("short_text", ""))
        elif kind == "order" and entry is not None:
            order = str(entry.get("order", ""))
            text = str(entry.get("short_text", ""))
        if order:
            menu.add_separator()
            menu.add_command(
                label="Process again via SAP",
                command=lambda o=order, t=text: self._on_reprint(o, t))
        menu.post(event.x_root, event.y_root)
