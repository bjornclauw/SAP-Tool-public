"""AtbHistoryStore -- local JSON history of orders with ATBs/uploads.

Per-machine log answering "which orders did I create ATBs for, and what did
I upload?" Shape:

    {"orders": [{
        "order": "4191447", "short_text": "URG| ...", "date": "28.09.2026",
        "workplace": "ME-01", "created_on": "28.09.2026",
        "atbs": [{"nr": "23063320", "kind": "Goods",
                  "short_text": "...", "created_on": "28.09.2026",
                  "updated_on": ""}],
        "attachments": [{"doc_nr": "...", "description": "...",
                         "filename": "...", "uploaded_on": "28.09.2026",
                         "updated_on": ""}],
    }]}

Pure data logic, no UI -- the Purchase Request Helper owns the view.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime

# SAP descriptions are short; capping at write time keeps the history file
# and every view/copy of it consistently trimmed.
MAX_DETAIL = 50

# Real SAP success message: "ATB-nummer 0023063323 gecreëerd".
_ATB_NR_RE = re.compile(r"ATB-nummer\s+(\d+)", re.IGNORECASE)
# Fallback for other languages, e.g. "Purchase requisition 10012345 created".
_FALLBACK_NR_RE = re.compile(r"(\d{8,})")


def parse_atb_number(sbar_text: str) -> str:
    """Extract the ATB number from SAP's creation message. "" when absent."""
    m = _ATB_NR_RE.search(sbar_text or "")
    if m:
        return m.group(1)
    m = _FALLBACK_NR_RE.search(sbar_text or "")
    return m.group(1) if m else ""


def _today() -> str:
    return datetime.now().strftime("%d.%m.%Y")


def _cap_detail(value) -> str:
    """Trim a description to MAX_DETAIL; the store keeps only the short form."""
    return (value or "")[:MAX_DETAIL]


def default_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "data", "atb_history.json")


class AtbHistoryStore:
    """Loads/saves the history JSON. Every mutator persists immediately;
    I/O failures never raise -- history must not break the tool window."""

    def __init__(self, path: str | None = None):
        self.path = path or default_path()
        self._data: dict = {"orders": []}
        self.load()

    # ---- persistence -------------------------------------------------
    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("orders"), list):
                self._data = data
        except (OSError, ValueError):
            self._data = {"orders": []}

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    @property
    def orders(self) -> list[dict]:
        return self._data["orders"]

    def find_order(self, order_nr: str) -> dict | None:
        for o in self._data["orders"]:
            if o.get("order") == order_nr:
                return o
        return None

    # ---- mutators ----------------------------------------------------
    def ensure_order(self, order_nr: str, short_text: str = "",
                     date: str = "", workplace: str = "") -> dict:
        """Return the order entry, creating it (newest-first) when missing.
        Provided meta fills only blank fields, so a later lookup with fuller
        data upgrades the shell without overwriting known values."""
        order_nr = (order_nr or "").strip()
        entry = self.find_order(order_nr)
        if entry is None:
            entry = {
                "order": order_nr,
                "short_text": _cap_detail(short_text),
                "date": date or "",
                "workplace": workplace or "",
                "created_on": _today(),
                "atbs": [],
                "attachments": [],
            }
            self._data["orders"].insert(0, entry)
            self.save()
            return entry
        for key, val in (("short_text", short_text), ("date", date),
                         ("workplace", workplace)):
            if val and not entry.get(key):
                entry[key] = _cap_detail(val) if key == "short_text" else val
        capped = _cap_detail(entry.get("short_text", ""))
        if capped != entry.get("short_text", ""):
            entry["short_text"] = capped
        self.save()
        return entry

    def add_atb(self, order_nr: str, nr: str, kind: str,
                short_text: str = "", vendor: str = "",
                vendor_name: str = "") -> dict | None:
        """Record a created ATB. Re-recording the same nr updates instead of
        duplicating, and stamps ``updated_on`` only when a field changed.
        Returns the ATB entry, or None when nr is blank."""
        nr = (nr or "").strip()
        if not nr:
            return None
        short_text = _cap_detail(short_text)
        entry = self.ensure_order(order_nr)
        for atb in entry["atbs"]:
            if atb.get("nr") == nr:
                changed = False
                for key, value in (("kind", kind), ("short_text", short_text),
                                   ("vendor", vendor),
                                   ("vendor_name", vendor_name)):
                    if value and value != atb.get(key):
                        atb[key] = value
                        changed = True
                if changed:
                    atb["updated_on"] = _today()
                self.save()
                return atb
        atb = {"nr": nr, "kind": kind or "",
               "short_text": short_text or "", "vendor": vendor or "",
               "vendor_name": vendor_name or "", "created_on": _today(),
               "updated_on": ""}
        entry["atbs"].insert(0, atb)
        self.save()
        return atb

    def seed_atbs(self, order_nr: str, atbs: list[dict]) -> int:
        """Fill in ATB records history does not have yet.

        Used when an order entry was lost (old store file, or a rotated-out
        order) but the overview grid still knows its ATBs. Existing numbers
        are never overwritten. Returns how many were added.
        """
        entry = self.find_order(order_nr)
        if entry is None:
            return 0
        existing = {a.get("nr") for a in entry.setdefault("atbs", [])}
        added = 0
        for atb in atbs:
            nr = (atb.get("nr") or "").strip()
            if not nr or nr in existing:
                continue
            entry["atbs"].append({
                "nr": nr,
                "kind": atb.get("kind", "") or "",
                "short_text": _cap_detail(atb.get("short_text", "")),
                "vendor": atb.get("vendor", "") or "",
                "vendor_name": atb.get("vendor_name", "") or "",
                "created_on": atb.get("created_on", "") or "",
                "updated_on": "",
            })
            existing.add(nr)
            added += 1
        if added:
            self.save()
        return added

    def add_attachment(self, order_nr: str, doc_nr: str,
                       description: str, filename: str) -> dict:
        """Record an uploaded attachment. Same doc_nr+filename twice updates
        instead of duplicating (re-uploads happen) and stamps ``updated_on``;
        ``uploaded_on`` stays the latest upload date."""
        description = _cap_detail(description)
        entry = self.ensure_order(order_nr)
        for att in entry["attachments"]:
            if att.get("doc_nr") == doc_nr and att.get("filename") == filename:
                if description:
                    att["description"] = description
                att["uploaded_on"] = _today()
                att["updated_on"] = _today()
                self.save()
                return att
        att = {"doc_nr": doc_nr or "", "description": description or "",
               "filename": filename or "", "uploaded_on": _today(),
               "updated_on": ""}
        entry["attachments"].insert(0, att)
        self.save()
        return att
