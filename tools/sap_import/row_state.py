"""Row-state transitions for SAP Import.

One place answers: given a row and an event, what Status, Bericht and failure
provenance result? The window reports the event and repaints; it never writes
the status fields itself, so the grid, palette, dialogs and counters always
read rows whose state came from here. Flag semantics follow ADR-0005
(docs/decisions/0005-upload-flag-semantics.md): the Upload flag (marker) means
the server stops handing the row out again; a cancel is a request (4) that is
processed by flagging and marking 10.
"""

from __future__ import annotations

from .models import (
    ALREADY_FLAGGED,
    CANCEL_REQUESTED,
    CANCELLED,
    COMPLETED,
    FAILED,
    FORCE_UPLOAD,
    LEGACY_FORCE,
    NO_COMM,
    OK_ERR_REPLY,
    PENDING,
    PROCESSING,
)

FLAG_NOTE = "Flag API failed"
PREFLIGHT_MISSING = "Missing FLOC/STEXT"
FLAG_ERROR = "API error (getFlag)"
CANCEL_MESSAGE = "Geannuleerd"

CLEARING_STATUSES = (PENDING, CANCEL_REQUESTED, FORCE_UPLOAD)


# ---- failure provenance -----------------------------------------------------

def record_failure(row: dict, script: str, message: str,
                   line_no, raw: str) -> None:
    row["Status"] = FAILED
    row["Bericht"] = message or "SAP script failed"
    row["_fail_script"] = script or ""
    row["_fail_line"] = "" if line_no is None else str(line_no)
    row["_fail_text"] = (raw or "").strip()[:300]


def clear_failure(row: dict) -> None:
    row.pop("_fail_script", None)
    row.pop("_fail_line", None)
    row.pop("_fail_text", None)


def has_failure(row: dict) -> bool:
    return (row.get("Status", "") == FAILED
            or bool(row.get("_fail_script") or row.get("_fail_text")
                    or row.get("_fail_line")))


def failure_text(row: dict) -> str:
    """Readable message + script + line + source line for the UI."""
    if not has_failure(row):
        return ""
    script = row.get("_fail_script", "") or "SAP script"
    line = row.get("_fail_line", "") or "?"
    return (
        f"{row.get('Bericht', '') or 'SAP script failed'}\n\n"
        f"Script: {script}\n"
        f"Line {line}: {row.get('_fail_text', '')}"
    )


# ---- transitions ------------------------------------------------------------

def _append_flag_note(row: dict) -> None:
    existing = row.get("Bericht", "")
    row["Bericht"] = (existing + " || " if existing else "") + FLAG_NOTE


def migrate_legacy_status(row: dict) -> None:
    """Retired status 66 (FORCEREN) is the cancel request now."""
    if str(row.get("Status")) == LEGACY_FORCE:
        row["Status"] = CANCEL_REQUESTED


def mark_processing(row: dict) -> None:
    row["Status"] = PROCESSING


def mark_preflight_missing(row: dict) -> None:
    row["Status"] = FAILED
    row["Bericht"] = PREFLIGHT_MISSING


def mark_flag_error(row: dict) -> None:
    row["Status"] = NO_COMM
    row["Bericht"] = FLAG_ERROR


def mark_already_flagged(row: dict) -> None:
    row["Status"] = ALREADY_FLAGGED


def mark_sap_done(row: dict, reply_text: str, kind: str,
                  created_number: str, flag_ok: bool) -> None:
    clear_failure(row)
    row["SAP_NUMBER"] = created_number
    row["SAP_KIND"] = kind
    row["Bericht"] = reply_text or (
        "WO created" if kind == "workorder" else "Notification created")
    if flag_ok:
        row["Status"] = COMPLETED
    else:
        row["Status"] = OK_ERR_REPLY
        _append_flag_note(row)


def mark_cancel_processed(row: dict, flag_ok: bool) -> None:
    row["Bericht"] = CANCEL_MESSAGE
    row["Status"] = CANCELLED if flag_ok else OK_ERR_REPLY
    if not flag_ok:
        _append_flag_note(row)


def mark_cancel_requested(row: dict) -> None:
    row["Status"] = CANCEL_REQUESTED
    row["Bericht"] = ""
    clear_failure(row)


def mark_force_upload(row: dict) -> None:
    row["Status"] = FORCE_UPLOAD


def mark_pending(row: dict) -> None:
    row["Status"] = PENDING
    row["Bericht"] = ""
    clear_failure(row)


def mark_undone(row: dict) -> None:
    """Local undo: no unflag endpoint, so a flagged row re-sends via Force
    Upload; an unflagged row returns to Pending."""
    clear_failure(row)
    if row.get("_id"):
        row["Status"] = FORCE_UPLOAD
        row["Bericht"] = ""
    else:
        mark_pending(row)


def assign_status(row: dict, code: str) -> None:
    """Manual/bulk assignment (quick menu, bulk edit, Force Upload request)."""
    row["Status"] = code
    if code in CLEARING_STATUSES:
        row["Bericht"] = ""
        clear_failure(row)
