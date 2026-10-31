"""Constants and column config for SAP Import."""

from __future__ import annotations

import os
from dataclasses import dataclass

PIC_URL_BASE = "https://apps.bcsys.be/prod-budi-app-assets/"
API_BASE = "https://api.bcsys.be/webhook/SAP"


@dataclass(frozen=True)
class Status:
    """One row status: its display label, colour and how the loop treats it.

    ``kind`` is "create" (send to SAP), "cancel" (flag + mark cancelled, never
    send) or "none". ``iterated`` means Send / auto upload picks the row up.
    """

    code: str
    label: str
    colour: str
    kind: str = "none"
    iterated: bool = False


# All row statuses fit 0-10. 66 (FORCEREN) is retired; old queues map it to 4.
STATUSES: dict[str, Status] = {
    "0": Status("0", "Pending", "#888888", "create", True),
    "1": Status("1", "Processing", "#ffa500"),
    "2": Status("2", "OK, ERR reply", "#ffa500"),
    "3": Status("3", "Error", "#e74c3c"),
    "4": Status("4", "Te annuleren", "#ffc000", "cancel", True),
    "5": Status("5", "OK, ERR pic", "#ffa500"),
    "6": Status("6", "Completed", "#27ae60"),
    "7": Status("7", "Already Added", "#3498db"),
    "8": Status("8", "NO COMM", "#e74c3c"),
    "9": Status("9", "Force Upload", "#e67e22", "create", True),
    "10": Status("10", "Geannuleerd", "#b8860b"),
}

STATUS_LABELS = {code: status.label for code, status in STATUSES.items()}
STATUS_COLORS = {code: status.colour for code, status in STATUSES.items()}

ITERATED_STATUSES = frozenset(c for c, s in STATUSES.items() if s.iterated)
CREATE_STATUSES = frozenset(c for c, s in STATUSES.items() if s.kind == "create")
CANCEL_STATUSES = frozenset(c for c, s in STATUSES.items() if s.kind == "cancel")
PENDING = "0"
PROCESSING = "1"
OK_ERR_REPLY = "2"
FAILED = "3"
CANCEL_REQUESTED = "4"
OK_ERR_PIC = "5"
COMPLETED = "6"
ALREADY_FLAGGED = "7"
NO_COMM = "8"
FORCE_UPLOAD = "9"
CANCELLED = "10"

# Retired status 66 (FORCEREN) maps to the cancel request when loading an
# old queue.
LEGACY_FORCE = "66"

# Accounting buckets shared by the Import window and the toolbar counter.
# Status 7 means SAP already has the row, so it is finished, not pending.
FINISHED_STATUSES = frozenset({"6", CANCELLED, "7"})
PENDING_STATUSES = frozenset({"0", "9"})
FAILED_STATUSES = frozenset({"3"})


def auto_upload_candidate(row: dict) -> bool:
    """A row the auto upload offers to Send, ignoring its current status:
    a cancellation request, approved (60RE) work, or a register auto-assign."""
    return (str(row.get("Status", "")) in CANCEL_STATUSES
            or row.get("USTATUS") == "60RE"
            or bool(row.get("_auto_assigned")))


def auto_upload_eligible(row: dict) -> bool:
    """Whether the next auto-upload tick would actually send this row.

    A candidate that is still in an iterated status (not completed, already
    added or cancelled); drives the grid's Auto indicator.
    """
    return (str(row.get("Status", "0")) in ITERATED_STATUSES
            and auto_upload_candidate(row))

STATUS_COLUMN_MAPPING = {
    "_id": "_id", "tableId": "tableId", "PICS": "PICS",
    "WO_TYPE": "WO_TYPE", "FLOC": "FLOC", "PG": "PG",
    "WP": "WP", "PRIOR": "PRIOR", "STEXT": "STEXT",
    "LTEXT": "LTEXT", "USER": "USER", "WO": "WO",
    "REV": "REV", "WO_KEY": "WO_KEY", "USTATUS": "USTATUS",
    "PICS_REP": "PICS_REP", "PREV_ORDER_ID": "PREV_ORDER_ID",
}

_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PICS_FOLDER = os.path.join(_APP_DIR, "tempexport", "pics")

# Friendly labels for the edit modal, using SAP (IW22/IW32) terminology. The
# modal groups fields as Location data / Order data / Responsibility / Texts /
# Administration, mirroring how SAP groups them on the order and notification
# screens.
FIELD_LABELS = {
    "FLOC": "Functional location",
    "WP": "Work center",
    "PG": "Planner group",
    "WO_TYPE": "Order type",
    "WO_KEY": "Activity type",
    "PRIOR": "Priority",
    "USTATUS": "User status",
    "STEXT": "Short text",
    "LTEXT": "Long text",
    "USER": "Reported by",
    "REV": "Revision",
    "WO": "Create work order",
    "Bericht": "SAP message",
    "_id": "Row id",
    "tableId": "Table id",
    "PREV_ORDER_ID": "Previous order id",
    "PICS_REP": "Repeat picture URLs",
}


def field_label(key: str) -> str:
    """The display label for a row field (falls back to the raw key)."""
    return FIELD_LABELS.get(key, key)


def is_image_file(path: str) -> bool:
    """True when *path* holds a decodable image.

    A cached attachment can be missing or corrupt (e.g. a failed/partial
    download); callers use this to decide whether it must be fetched again.
    """
    if not path or not os.path.exists(path):
        return False
    try:
        from PIL import Image

        with Image.open(path) as img:
            img.verify()
        return True
    except Exception:
        return False


COL_WIDTHS = {
    "Auto": 44,
    "Status": 70, "_id": 120, "PG": 50, "FLOC": 120, "WP": 80,
    "WO_TYPE": 70, "WO_KEY": 60, "STEXT": 200, "PRIOR": 50,
    "WO": 40, "USTATUS": 65, "PICS": 60, "REV": 160, "USER": 80,
    "LTEXT": 150, "Bericht": 200,
}
