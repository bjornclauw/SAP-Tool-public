"""AtbHistoryPane -- searchable order/ATB/attachment history for the PRH window.

Lives in the right pane under the ATB form. One hierarchical Treeview:
days as parents, orders within each day, created ATBs and app-uploaded
attachments under their order. A filter box matches every stored field --
order, ATB and attachment data -- while double-click dispatches by node
type through the owner's hooks (which own all SAP work):

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
from datetime import datetime
from tkinter import ttk

from tools.atb_history import MAX_DETAIL
from ui import ui_styles
from ui.ui_styles import styled_frame, styled_label

_WS_RE = re.compile(r"[\r\n\t]+")

_DESCRIPTION_LIMIT = MAX_DETAIL

_HISTORY_COLUMNS = ("Type", "Reference", "Description", "When")

# A real table row, not a cell border: Word/Outlook honour cell backgrounds
# but routinely drop inline border-top styles.
_GROUP_DIVIDER_HTML = (
    '<tr><td colspan="4" bgcolor="#A6A6A6" '
    'style="height:6px;background-color:#A6A6A6;padding:0;'
    'font-size:1pt;line-height:1pt">&nbsp;</td></tr>'
)


def _oneline(value) -> str:
    """Flatten one display string to a single Treeview line.

    SAP texts arrive with \\r separators; a cell carrying them paints its
    continuation lines over neighbouring rows (fixed rowheight). The store
    keeps the raw text -- the search box matches against it -- only what the
    tree renders is flattened here.
    """
    return _WS_RE.sub(" ", str(value or "")).strip()


def _clip(value, limit: int = _DESCRIPTION_LIMIT) -> str:
    """Flattened text capped at SAP's 40-char description width."""
    return _oneline(value)[:limit]


def _order_day(order) -> str:
    """The order's latest activity day (recorded, ATB, or upload).

    Grouping by latest activity keeps a late PROFORMA attachment findable:
    the whole order moves to today's group, and its ATB travels with it.
    """
    dates = [order.get("created_on", "")]
    atbs = order.get("atbs", []) or []
    attachments = order.get("attachments", []) or []
    dates += [a.get("created_on", "") for a in atbs]
    dates += [a.get("updated_on", "") for a in atbs]
    dates += [a.get("uploaded_on", "") for a in attachments]
    dates += [a.get("updated_on", "") for a in attachments]
    best = ""
    best_key = None
    for raw in dates:
        text = _oneline(raw)
        if not text:
            continue
        key = _day_sort_key(text)
        if best_key is None or key > best_key:
            best, best_key = text, key
    return best or "Unknown"


def _day_sort_key(day: str) -> tuple[int, str]:
    """Newest day first, with unparseable dates sorted last."""
    try:
        return (datetime.strptime(day, "%d.%m.%Y").toordinal(), day)
    except (TypeError, ValueError):
        return (0, day)


def _relative_day(day: str) -> str:
    """Today/Yesterday for recent days, the date itself otherwise."""
    try:
        parsed = datetime.strptime(day, "%d.%m.%Y").date()
    except (TypeError, ValueError):
        return day
    return {0: "Today", 1: "Yesterday"}.get(
        (datetime.now().date() - parsed).days, day)


def _updated(record) -> bool:
    """True once a re-record/re-upload stamped ``updated_on``."""
    return bool(_oneline(record.get("updated_on", "")))


def _vendor_text(atb) -> str:
    return " ".join(
        p for p in (atb.get("vendor", ""), atb.get("vendor_name", "")) if p
    )


def _atb_details(atb, kind_prefix: str = "") -> str:
    """Description plus supplier; the kind prefix is only for the tree row."""
    text = _clip(atb.get("short_text", ""))
    details = f"{kind_prefix}: {text}" if kind_prefix else text
    return _join((details, _vendor_text(atb)))


def _attachment_details(att) -> str:
    """Description plus document type."""
    return _join((_clip(att.get("description", "")), att.get("doc_nr", "")))


def _atb_when(atb) -> str:
    """When cell for an ATB: Created/Updated + date only."""
    when = _oneline(atb.get("updated_on", "")) or _oneline(
        atb.get("created_on", ""))
    label = "Updated" if _updated(atb) else "Created"
    return f"{label} {when}".strip()


def _attachment_when(att) -> str:
    """When cell for an attachment: Uploaded/Updated + date only."""
    when = _oneline(att.get("uploaded_on", ""))
    label = "Updated" if _updated(att) else "Uploaded"
    return f"{label} {when}".strip()


def _order_when(order) -> str:
    """When cell for an order: SAP start date (or recorded fallback)."""
    date = _oneline(order.get("date", ""))
    if date:
        return f"Start {date}"
    created = _oneline(order.get("created_on", ""))
    return f"Recorded {created}" if created else ""


def _blob(*values) -> str:
    return " ".join(_oneline(v) for v in values if v).lower()


def _order_blob(order) -> str:
    return _blob(order.get("order"), order.get("short_text"),
                 order.get("date"), order.get("workplace"),
                 order.get("created_on"))


def _atb_blob(atb) -> str:
    return _blob(atb.get("nr"), atb.get("kind"), atb.get("short_text"),
                 atb.get("vendor"), atb.get("vendor_name"),
                 atb.get("created_on"), atb.get("updated_on"))


def _attachment_blob(att) -> str:
    return _blob(att.get("doc_nr"), att.get("description"),
                 att.get("filename"), att.get("uploaded_on"),
                 att.get("updated_on"))


def selected_history_groups(tree, payload, store):
    """Selection as [group] in tree display order, one group per order.

    Each group is an order record plus one row dict per ATB / attachment
    beneath it; a selected child pulls in its order group for context.
    Selecting an order copies its entire subtree (all ATBs and uploads),
    which is how a work order is shared with accounting. Placeholder rows
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
    for group in groups:
        if group["order_selected"]:
            group["children"] = _all_child_entries(
                group["order"], group["order"]["order"])
    return groups


def _all_child_entries(order, nr):
    """Every ATB and attachment under an order, in tree display order."""
    entries = [
        {"type": "atb", "order": nr, "nr": atb.get("nr", "")}
        for atb in order.get("atbs", []) or []
    ]
    entries += [
        {
            "type": "attachment", "order": nr,
            "doc_nr": att.get("doc_nr", ""),
            "filename": att.get("filename", ""),
        }
        for att in order.get("attachments", []) or []
    ]
    return entries


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
    short = _clip(order.get("short_text", ""))
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
                _atb_details(atb),
                _atb_when(atb),
            ))
        elif kind == "attachment":
            att = _attachment_record(order, entry.get("doc_nr", ""),
                                     entry.get("filename", ""))
            rows.append((
                "Attachment",
                _oneline(att.get("filename", "")),
                _attachment_details(att),
                _attachment_when(att),
            ))
    if order_selected and not children:
        rows.append((
            "Order",
            _oneline(order.get("order", "")),
            _clip(order.get("short_text", "")),
            _order_when(order),
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
    for index, group in enumerate(groups):
        if index:
            parts.append(_GROUP_DIVIDER_HTML)
        title, meta = _group_header(group["order"])
        header = f"<b>{html.escape(title)}</b>"
        if meta:
            header += f'<br><span style="color:#595959">{html.escape(meta)}</span>'
        parts.append(
            f'<tr><td colspan="4" style="background-color:#F2F2F2">'
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
    rule = "-" * len(lines[0])
    for index, group in enumerate(groups):
        title, meta = _group_header(group["order"])
        lines.append("")
        if index:
            lines.append(rule)
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
        self._subtitle = styled_label(
            top, "by latest activity", font=ui_styles.FONTS["small"],
            fg=ui_styles.COLORS["text_secondary"], bg=ui_styles.COLORS["bg"],
        )
        self._subtitle.pack(side="left", padx=(6, 0))
        # Next to the header: copies the selected rows as a table for
        # Outlook (see _on_copy_clicked).
        self._copy_btn = ui_styles.styled_button(
            top, "Copy", width=8, pady=1, command=self._on_copy_clicked
        )
        self._copy_btn.pack(side="left", padx=(8, 0))

        self._search_var = tk.StringVar()
        self._clear_search_btn = ui_styles.styled_button(
            top, "Clear", width=6, pady=1, command=self._clear_search,
            state="disabled",
        )
        self._clear_search_btn.pack(side="right", padx=(4, 0))
        search = tk.Entry(
            top,
            textvariable=self._search_var,
            font=ui_styles.FONTS["body"],
            width=20,
        )
        self._search_entry = search
        search.pack(side="right")
        search.bind("<KeyRelease>", lambda e: self.refresh())
        search.bind("<Escape>", lambda e: self._clear_search())
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
        self.tree.heading("info", text="When")
        self.tree.column("#0", width=150, minwidth=80, stretch=False)
        self.tree.column("details", width=250, minwidth=100, stretch=True)
        self.tree.column("info", width=230, minwidth=120, stretch=False)
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
            "day", font=(body[0], body[1], "bold"),
            foreground=ui_styles.COLORS["primary"],
        )
        self.tree.tag_configure(
            "order", font=(body[0], body[1], "bold")
        )
        self.tree.tag_configure("att", foreground=ui_styles.COLORS["text_secondary"])

        self.refresh()

    # ---- view ------------------------------------------------------
    def _clear_search(self) -> None:
        """Drop the filter and return focus to the search box."""
        if self._search_var.get():
            self._search_var.set("")
            self.refresh()
        self._search_entry.focus_set()

    def refresh(self) -> None:
        """Rebuild the tree from the store, applying the search filter.

        Orders are grouped under their latest activity day (newest day
        first), so a late upload moves the order -- with its ATBs -- into
        today's group instead of hiding under the day it was first recorded.
        """
        self.tree.delete(*self.tree.get_children())
        self._payload = {}
        orders = self._store.orders

        q = (self._search_var.get() or "").strip().lower()
        days: dict[
            str, list[tuple[int, dict, bool, list[dict], list[dict]]]
        ] = {}
        for idx, o in enumerate(orders):
            atbs = o.get("atbs", []) or []
            attachments = o.get("attachments", []) or []
            order_hit = not q or q in _order_blob(o)
            atb_hits = [a for a in atbs if q and q in _atb_blob(a)]
            att_hits = [
                a for a in attachments if q and q in _attachment_blob(a)
            ]
            if not (order_hit or atb_hits or att_hits):
                continue
            days.setdefault(_order_day(o), []).append(
                (idx, o, order_hit, atb_hits, att_hits)
            )

        shown = 0
        for day in sorted(days, key=_day_sort_key, reverse=True):
            entries = days[day]
            did = f"day:{day}"
            label = _relative_day(day)
            self.tree.insert(
                "", "end", iid=did, text=_oneline(label),
                values=(_oneline(f"{len(entries)} order(s)"),
                        _oneline(day if label != day else "")),
                tags=("day",), open=True,
            )
            for idx, o, order_hit, atb_hits, att_hits in entries:
                shown += 1
                nr = o.get("order", "")
                short = o.get("short_text", "")
                atbs = o.get("atbs", []) or []
                attachments = o.get("attachments", []) or []
                oid = f"order:{idx}:{nr}"
                self.tree.insert(
                    did, "end", iid=oid, text=_oneline(nr),
                    values=(_clip(short), _order_when(o)),
                    tags=("order",), open=bool(q),
                )
                self._payload[oid] = {"type": "order", "order": nr}
                kids = atbs if order_hit else atb_hits
                for j, a in enumerate(kids):
                    aid = f"{oid}:atb:{j}"
                    tag = (a.get("kind", "") or "").upper()
                    self.tree.insert(
                        oid, "end", iid=aid, text=_oneline(f"ATB {a.get('nr', '')}"),
                        values=(_atb_details(a, tag), _atb_when(a)),
                    )
                    self._payload[aid] = {
                        "type": "atb", "order": nr, "nr": a.get("nr", ""),
                    }
                atts = attachments if order_hit else att_hits
                for k, att in enumerate(atts):
                    tid = f"{oid}:att:{k}"
                    self.tree.insert(
                        oid, "end", iid=tid,
                        text=_oneline(f"\U0001F4CE {att.get('filename', '')}"),
                        values=(_attachment_details(att),
                                _attachment_when(att)),
                        tags=("att",),
                    )
                    self._payload[tid] = {
                        "type": "attachment", "order": nr,
                        "doc_nr": att.get("doc_nr", ""),
                        "filename": att.get("filename", ""),
                    }
        if q:
            self._header.config(text=f"History ({shown} of {len(orders)})")
            self._subtitle.config(
                text="filtered", fg=ui_styles.COLORS["primary"]
            )
            self._clear_search_btn.config(state="normal")
        else:
            self._header.config(text=f"History ({len(orders)})")
            self._subtitle.config(
                text="by latest activity",
                fg=ui_styles.COLORS["text_secondary"],
            )
            self._clear_search_btn.config(state="disabled")

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
