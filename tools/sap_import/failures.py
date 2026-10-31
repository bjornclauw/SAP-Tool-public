"""Failure provenance on an Import row.

The engine knows the SAP message and the failing command (line number and
source text); these helpers store that on the row, clear it after a later
success, and render the readable detail shared by the context menu and modal.
"""

from __future__ import annotations


def record_failure(row: dict, script: str, message: str,
                   line_no, raw: str) -> None:
    row["Status"] = "3"
    row["Bericht"] = message or "SAP script failed"
    row["_fail_script"] = script or ""
    row["_fail_line"] = "" if line_no is None else str(line_no)
    row["_fail_text"] = (raw or "").strip()[:300]


def clear_failure(row: dict) -> None:
    row.pop("_fail_script", None)
    row.pop("_fail_line", None)
    row.pop("_fail_text", None)


def has_failure(row: dict) -> bool:
    return (row.get("Status") == "3"
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
