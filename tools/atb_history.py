"""AtbHistoryStore -- local JSON history of orders with ATBs/uploads.

Per-machine log answering "which orders did I create ATBs for, and what did
I upload?" Shape:

    {"orders": [{
        "order": "4191447", "short_text": "URG| ...", "date": "28.09.2026",
        "workplace": "ME-01", "created_on": "28.09.2026",
        "atbs": [{"nr": "23063320", "kind": "Goods",
                  "short_text": "...", "created_on": "28.09.2026"}],
        "attachments": [{"doc_nr": "...", "description": "...",
                         "filename": "...", "uploaded_on": "28.09.2026"}],
    }]}

Pure data logic, no UI -- the Purchase Request Helper owns the view.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime

MAX_ORDERS = 100

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
                "short_text": short_text or "",
                "date": date or "",
                "workplace": workplace or "",
                "created_on": _today(),
                "atbs": [],
                "attachments": [],
            }
            self._data["orders"].insert(0, entry)
            del self._data["orders"][MAX_ORDERS:]
            self.save()
            return entry
        for key, val in (("short_text", short_text), ("date", date),
                         ("workplace", workplace)):
            if val and not entry.get(key):
                entry[key] = val
        self.save()
        return entry

    def add_atb(self, order_nr: str, nr: str, kind: str,
                short_text: str = "", vendor: str = "",
                vendor_name: str = "") -> dict | None:
        """Record a created ATB. Re-recording the same nr updates instead of
        duplicating. Returns the ATB entry, or None when nr is blank."""
        nr = (nr or "").strip()
        if not nr:
            return None
        entry = self.ensure_order(order_nr)
        for atb in entry["atbs"]:
            if atb.get("nr") == nr:
                atb["kind"] = kind or atb.get("kind", "")
                if short_text:
                    atb["short_text"] = short_text
                if vendor:
                    atb["vendor"] = vendor
                if vendor_name:
                    atb["vendor_name"] = vendor_name
                self.save()
                return atb
        atb = {"nr": nr, "kind": kind or "",
               "short_text": short_text or "", "vendor": vendor or "",
               "vendor_name": vendor_name or "", "created_on": _today()}
        entry["atbs"].insert(0, atb)
        self.save()
        return atb

    def add_attachment(self, order_nr: str, doc_nr: str,
                       description: str, filename: str) -> dict:
        """Record an uploaded attachment. Same doc_nr+filename twice updates
        instead of duplicating (re-uploads happen)."""
        entry = self.ensure_order(order_nr)
        for att in entry["attachments"]:
            if att.get("doc_nr") == doc_nr and att.get("filename") == filename:
                if description:
                    att["description"] = description
                att["uploaded_on"] = _today()
                self.save()
                return att
        att = {"doc_nr": doc_nr or "", "description": description or "",
               "filename": filename or "", "uploaded_on": _today()}
        entry["attachments"].insert(0, att)
        self.save()
        return att
