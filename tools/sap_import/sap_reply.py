"""Parsing SAP's status-bar reply after a save.

SAP reports the created document in the status bar, e.g.
``Notification 10001234 created`` or ``Order 10004567 created`` (localized:
``Meldung ... angelegt``, ``Auftrag ... erstellt``). The save scripts capture
``sbar.text`` and ``sbar.MessageType``; these helpers decide whether the save
succeeded and pull out the document number, so the row can store it for
"Open in SAP" later.
"""

from __future__ import annotations

import re

# Label followed by the number, tolerant of localised wording and separators:
# "Notification 10001234 created", "Auftrag Nr. 10004567 wurde angelegt".
_NUMBER_AFTER_LABEL = re.compile(
    r"(?:notification|order|meldung|auftrag|werkorder|instandhaltungsauftrag)"
    r"[^\d]{0,24}?(\d{6,})",
    re.IGNORECASE,
)
# Fallback when there is no recognisable label: the first long digit run.
_FALLBACK_NUMBER = re.compile(r"\b(\d{7,})\b")


def parse_created_number(text: str | None) -> str:
    """The document number in SAP's save message; "" when there isn't one."""
    match = _NUMBER_AFTER_LABEL.search(text or "")
    if match:
        return match.group(1)
    match = _FALLBACK_NUMBER.search(text or "")
    return match.group(1) if match else ""


def is_success(sbar_type: str | None, text: str | None) -> bool:
    """Did SAP accept the save? Reads the status-bar message type first.

    ``S`` = success, ``I``/``W`` = informational/warning (accepted), ``E``/``A``
    = rejected. With no usable type, a creation-number message counts as
    success and anything else does not.
    """
    kind = str(sbar_type or "").strip().upper()
    if kind in ("E", "A"):
        return False
    if kind in ("S", "I", "W"):
        return True
    return bool(parse_created_number(text))
