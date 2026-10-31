"""AtbHistoryPane -- searchable order/ATB/attachment history for the PRH window.

Lives in the right pane under the ATB form. One hierarchical Treeview:
orders as parents, created ATBs and app-uploaded attachments as children.
A filter box matches order nr, ATB nr, and order short text; double-click
dispatches by node type through the owner's hooks (which own all SAP work):

    on_open_order(order_nr)          -> IW32
    on_open_atb(atb_nr)              -> ME53N
    on_open_attachment(payload)      -> download + open locally

Pure view over AtbHistoryStore -- no engine access here.
"""

from __future__ import annotations

import html
import re
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ui import ui_styles
from ui.ui_styles import styled_frame, styled_label

_WS_RE = re.compile(r"[\r\n\t]+")

_HISTORY_COLUMNS = ("Type", "Reference", "Description", "Info")


def _oneline(value) -> str:
    """Flatten one display string to a single Treeview line.

    SAP texts arrive with \\r separators; a cell carrying them paints its
    continuation lines over neighbouring rows (fixed rowheight). The store
    keeps the raw text -- the search box matches against it -- only what the
    tree renders is flattened here.
    """
    return _WS_RE.sub(" ", str(value or "")).strip()


def selected_history_groups(tree, payload, store):
    """Selection as [group] in tree display order, one group per order.

    Each group is an order record plus one row dict per *selected* ATB /
    attachment beneath it; a selected child pulls in its order group for
    context. Selecting an order with no selected children yields one order
    summary row, so the selection is never silently empty. Placeholder rows
    (not in payload) are skipped.
    """
    selected = set(tree.selection())
    groups: list[dict] = []
    by_order: dict[str, dict] = {}

    def visit(iid):
        if iid in selected and iid in payload:
            entry = payload[iid]
            nr = entry["order"]
            group = by_order.get(nr)
            if group is None:
                group = {
                    "order": store.find_order(nr) or {"order": nr},
                    "order_selected": False,
                    "children": [],
                }
                by_order[nr] = group
                groups.append(group)
            if entry["type"] == "order":
                group["order_selected"] = True
            else:
                group["children"].append(entry)
        for child in tree.get_children(iid):
            visit(child)

    for iid in tree.get_children():
        visit(iid)
    return groups


def _atb_record(order, nr):
    for atb in order.get("atbs", []) or []:
        if atb.get("nr") == nr:
            return atb
    return {}


def _attachment_record(order, doc_nr, filename):
    for att in order.get("attachments", []) or []:
        if att.get("doc_nr") == doc_nr and att.get("filename") == filename:
            return att
    return {}


def _join(parts):
    return _oneline(" · ".join(p for p in parts if p))


def _group_header(order) -> tuple[str, str]:
    """(title, meta) for one order group, blanks skipped."""
    title = _oneline(order.get("order", ""))
    short = _oneline(order.get("short_text", ""))
    if short:
        title = f"{title} — {short}" if title else short
    meta = _join((order.get("date") or order.get("created_on", ""),
                 order.get("workplace", "")))
    return (f"Order {title}".strip(), meta)


def _detail_rows(order, children, order_selected) -> list[tuple[str, str, str, str]]:
    """Selected children as table rows, plus an order summary row when the
    order itself is selected but none of its children are."""
    rows = []
    for entry in children:
        kind = entry["type"]
        if kind == "atb":
            atb = _atb_record(order, entry.get("nr", ""))
            tag = (atb.get("kind", "") or "").strip()
            type_label = f"ATB ({tag})" if tag else "ATB"
            rows.append((
                type_label,
                _oneline(atb.get("nr", "")),
                _oneline(atb.get("short_text", "")),
                _join((atb.get("created_on", ""),
                       " ".join(p for p in (atb.get("vendor", ""),
                                            atb.get("vendor_name", "")) if p))),
            ))
        elif kind == "attachment":
            att = _attachment_record(order, entry.get("doc_nr", ""),
                                     entry.get("filename", ""))
            rows.append((
                "Attachment",
                _oneline(att.get("filename", "")),
                _oneline(att.get("description", "")),
                _join((att.get("doc_nr", ""), att.get("uploaded_on", ""))),
            ))
    if order_selected and not children:
        rows.append((
            "Order",
            _oneline(order.get("order", "")),
            _oneline(order.get("short_text", "")),
            _join((order.get("date") or order.get("created_on", ""),
                   order.get("workplace", ""))),
        ))
    return rows


def render_html_history(groups) -> str:
    """Groups as one HTML table -- pastes formatted straight into Outlook."""

    def cells(values, tag, style=""):
        return "".join(f"<{tag}{style}>{html.escape(v)}</{tag}>" for v in values)

    th = ' style="background-color:#D9E2F3"'
    parts = [
        '<table border="1" cellpadding="5" cellspacing="0" '
        'style="border-collapse:collapse;font-family:Calibri,Arial,sans-serif">',
        f"<tr>{cells(_HISTORY_COLUMNS, 'th', th)}</tr>",
    ]
    for group in groups:
        title, meta = _group_header(group["order"])
        header = f"<b>{html.escape(title)}</b>"
        if meta:
            header += f'<br><span style="color:#595959">{html.escape(meta)}</span>'
        parts.append(
            '<tr><td colspan="4" style="background-color:#F2F2F2">'
            f"{header}</td></tr>"
        )
        for row in _detail_rows(group["order"], group["children"],
                                group["order_selected"]):
            parts.append(f"<tr>{cells(row, 'td')}</tr>")
    parts.append("</table>")
    return "".join(parts)


def render_text_history(groups) -> str:
    """Groups as a plain-text summary (non-Outlook fallback)."""
    detail = [r for g in groups
              for r in _detail_rows(g["order"], g["children"], g["order_selected"])]
    widths = [len(h) for h in _HISTORY_COLUMNS]
    for row in detail:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(value))

    def fmt(row):
        return "  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip()

    lines = [fmt(_HISTORY_COLUMNS)]
    for group in groups:
        title, meta = _group_header(group["order"])
        lines.append("")
        lines.append(title if not meta else f"{title} · {meta}")
        for row in _detail_rows(group["order"], group["children"],
                                group["order_selected"]):
            lines.append(fmt(row))
    return "\n".join(lines).strip()


def _html_clipboard_payload(fragment: str) -> bytes:
    """Wrap an HTML fragment in the CF_HTML envelope (byte offsets)."""
    head = "<html><body>\r\n<!--StartFragment-->"
    tail = "<!--EndFragment-->\r\n</body>\r\n</html>"
    header = (
        "Version:0.9\r\nStartHTML:00000000\r\nEndHTML:00000000\r\n"
        "StartFragment:00000000\r\nEndFragment:00000000\r\n"
    )
    start_html = len(header.encode("utf-8"))
    start_frag = start_html + len(head.encode("utf-8"))
    end_frag = start_frag + len(fragment.encode("utf-8"))
    end_html = end_frag + len(tail.encode("utf-8"))
    header = (
        f"Version:0.9\r\nStartHTML:{start_html:08d}\r\nEndHTML:{end_html:08d}\r\n"
        f"StartFragment:{start_frag:08d}\r\nEndFragment:{end_frag:08d}\r\n"
    )
    return (header + head + fragment + tail).encode("utf-8")


def copy_table_to_clipboard(widget, html_text: str, plain_text: str) -> bool:
    """Put the table on the clipboard.

    On Windows (pywin32 present) this sets a real HTML table -- Outlook
    pastes it formatted -- plus plain text for everywhere else. Otherwise it
    falls back to the Tk clipboard with the plain-text table.
    """
    try:
        import win32clipboard
    except ImportError:
        win32clipboard = None
    if win32clipboard is not None:
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(
                    win32clipboard.CF_UNICODETEXT, plain_text
                )
                fmt = win32clipboard.RegisterClipboardFormat("HTML Format")
                win32clipboard.SetClipboardData(
                    fmt, _html_clipboard_payload(html_text)
                )
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception:
            pass  # fall through to the Tk clipboard
    try:
        widget.clipboard_clear()
        widget.clipboard_append(plain_text)
        return True
    except tk.TclError:
        return False


class AtbHistoryPane:
    """Searchable history tree. Pack itself into the given parent."""

    def __init__(
        self,
        parent: tk.Misc,
        store,
        on_open_order: Callable[[str], None],
        on_open_atb: Callable[[str], None],
        on_open_attachment: Callable[[dict], None],
    ):
        self._store = store
        self._on_open_order = on_open_order
        self._on_open_atb = on_open_atb
        self._on_open_attachment = on_open_attachment
        self._payload: dict[str, dict] = {}

        container = styled_frame(parent)
        container.pack(fill="both", expand=True, padx=0, pady=0)
        self.frame = container

        # --- header + search ---
        top = styled_frame(container)
        top.pack(fill="x", pady=(0, ui_styles.SPACING["tight"]))

        self._header = styled_label(
            top, "History", font=ui_styles.FONTS["header"], bg=ui_styles.COLORS["bg"]
        )
        self._header.pack(side="left")
        # Next to the header: copies the selected rows as a table for
        # Outlook (see _on_copy_clicked).
        self._copy_btn = ui_styles.styled_button(
            top, "Copy", width=8, command=self._on_copy_clicked
        )
        self._copy_btn.pack(side="left", padx=(8, 0))

        self._search_var = tk.StringVar()
        search = tk.Entry(
            top,
            textvariable=self._search_var,
            font=ui_styles.FONTS["body"],
            width=20,
        )
        search.pack(side="right")
        search.bind("<KeyRelease>", lambda e: self.refresh())
        styled_label(
            top, "Search:", font=ui_styles.FONTS["body"], bg=ui_styles.COLORS["bg"]
        ).pack(side="right", padx=(0, 4))

        # --- tree ---
        style = ttk.Style()
        style.configure(
            "HistTreeview.Treeview",
            font=ui_styles.FONTS["body"],
            background=ui_styles.COLORS["surface"],
            fieldbackground=ui_styles.COLORS["surface"],
        )
        self.tree = ttk.Treeview(
            container,
            columns=("details", "info"),
            show="tree headings",
            style="HistTreeview.Treeview",
        )
        self.tree.heading("#0", text="Entry")
        self.tree.heading("details", text="Details")
        self.tree.heading("info", text="Info")
        self.tree.column("#0", width=150, minwidth=80, stretch=False)
        self.tree.column("details", width=250, minwidth=100, stretch=True)
        self.tree.column("info", width=190, minwidth=80, stretch=False)
        # Scrollbars: long histories need vertical scrolling, and the fixed
        # Entry/Info columns overflow on narrow right panes. Packed around
        # the tree (v-bar right first) so neither is ever squeezed out.
        self._v_scroll = ttk.Scrollbar(
            container, orient="vertical", command=self.tree.yview
        )
        self._h_scroll = ttk.Scrollbar(
            container, orient="horizontal", command=self.tree.xview
        )
        self._v_scroll.pack(side="right", fill="y")
        self._h_scroll.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True)
        self.tree.configure(yscrollcommand=self._v_scroll.set,
                            xscrollcommand=self._h_scroll.set)
        self.tree.bind("<Double-1>", self._on_double_click)

        body = ui_styles.FONTS["body"]
        self.tree.tag_configure(
            "order", font=(body[0], body[1], "bold")
        )
        self.tree.tag_configure("att", foreground=ui_styles.COLORS["text_secondary"])

        self.refresh()

    # ---- view ------------------------------------------------------
    def refresh(self) -> None:
        """Rebuild the tree from the store, applying the search filter."""
        self.tree.delete(*self.tree.get_children())
        self._payload = {}
        orders = self._store.orders
        self._header.config(text=f"History ({len(orders)})")

        q = (self._search_var.get() or "").strip().lower()
        shown = 0
        for idx, o in enumerate(orders):
            nr = o.get("order", "")
            short = o.get("short_text", "")
            atbs = o.get("atbs", []) or []
            atb_hits = [a for a in atbs if q and q in a.get("nr", "").lower()]
            order_hit = not q or q in nr.lower() or q in short.lower()
            if not (order_hit or atb_hits):
                continue
            shown += 1
            info = " · ".join(
                p for p in (o.get("date") or o.get("created_on", ""), o.get("workplace", ""))
                if p
            )
            oid = f"order:{idx}:{nr}"
            self.tree.insert(
                "", "end", iid=oid, text=_oneline(nr),
                values=(_oneline(short), _oneline(info)), tags=("order",), open=bool(q),
            )
            self._payload[oid] = {"type": "order", "order": nr}
            kids = atbs if order_hit else atb_hits
            for j, a in enumerate(kids):
                aid = f"{oid}:atb:{j}"
                tag = (a.get("kind", "") or "").upper()
                details = f"{tag}: {a.get('short_text', '')}" if tag else a.get(
                    "short_text", "")
                vendor = " ".join(
                    p for p in (a.get("vendor", ""), a.get("vendor_name", "")) if p
                )
                ainfo = " · ".join(
                    p for p in (a.get("created_on", ""), vendor) if p
                )
                self.tree.insert(
                    oid, "end", iid=aid, text=_oneline(f"ATB {a.get('nr', '')}"),
                    values=(_oneline(details), _oneline(ainfo)),
                )
                self._payload[aid] = {
                    "type": "atb", "order": nr, "nr": a.get("nr", ""),
                }
            if order_hit:
                for k, att in enumerate(o.get("attachments", []) or []):
                    tid = f"{oid}:att:{k}"
                    ainfo = " · ".join(
                        p for p in (att.get("doc_nr", ""), att.get("uploaded_on", ""))
                        if p
                    )
                    self.tree.insert(
                        oid, "end", iid=tid,
                        text=_oneline(f"\U0001F4CE {att.get('filename', '')}"),
                        values=(_oneline(att.get("description", "")), _oneline(ainfo)),
                        tags=("att",),
                    )
                    self._payload[tid] = {
                        "type": "attachment", "order": nr,
                        "doc_nr": att.get("doc_nr", ""),
                        "filename": att.get("filename", ""),
                    }
        if not orders:
            self.tree.insert(
                "", "end", iid="hist-empty",
                text="No history yet",
                values=(_oneline("Created ATBs and uploads will appear here."), ""),
            )
        elif not shown:
            self.tree.insert(
                "", "end", iid="hist-empty",
                text="No matches",
                values=(_oneline(f'Nothing matches "{self._search_var.get().strip()}".'), ""),
            )

    # ---- events ----------------------------------------------------
    def _on_copy_clicked(self) -> bool:
        """Copy the selected history rows as a table for Outlook.

        Returns True when the clipboard now holds the table; flashes a hint
        on the button when nothing (copyable) is selected.
        """
        groups = selected_history_groups(self.tree, self._payload, self._store)
        if not groups:
            self._flash_copy_button("Select rows")
            return False
        ok = copy_table_to_clipboard(
            self.frame, render_html_history(groups), render_text_history(groups)
        )
        self._flash_copy_button("Copied" if ok else "Copy failed")
        return ok

    def _flash_copy_button(self, text: str) -> None:
        self._copy_btn.config(text=text)
        self._copy_btn.after(1200, lambda: self._copy_btn.config(text="Copy"))

    def _on_double_click(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if not item:
            return
        payload = self._payload.get(item)
        if not payload:
            return
        kind = payload["type"]
        if kind == "order":
            self._on_open_order(payload["order"])
        elif kind == "atb":
            self._on_open_atb(payload["nr"])
        elif kind == "attachment":
            self._on_open_attachment(payload)
