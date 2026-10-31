"""outlook_drop -- read mail attachments via Outlook COM for drag-and-drop.

Outlook drops never arrive as file paths (only FileGroupDescriptor /
FileContents), so at drop time we ask Outlook itself what is selected and
save the attachments to a staging dir. The COM boundary is injectable for
tests: pass `outlook` (anything with ActiveExplorer().Selection); when
omitted, `win32com.client.Dispatch("Outlook.Application")` is used.
"""

from __future__ import annotations

import os


def read_outlook_selection_attachments(
    dest_dir: str, outlook=None
) -> tuple[list[str], str | None]:
    """Save attachments of the current mail-list selection to dest_dir.

    Returns (saved_paths, error): error is None on success, otherwise a
    human-readable reason (meant for the status bar).
    """
    if outlook is None:
        outlook = _connect_outlook()
        if outlook is None:
            return [], (
                "Outlook is not available "
                "(the New Outlook cannot be read this way)"
            )

    saved: list[str] = []
    try:
        candidates = _candidate_items(outlook)
    except Exception:
        return [], "Could not read Outlook selection"
    for item in candidates:
        attachments = getattr(item, "Attachments", [])
        for attachment in list(attachments):
            filename = os.path.basename(attachment.FileName or "")
            if not filename or _is_embedded(attachment):
                continue
            path = _unique_path(dest_dir, filename, saved)
            attachment.SaveAsFile(path)
            saved.append(path)
    return saved, None


def _unique_path(dest_dir: str, filename: str, taken: list[str]) -> str:
    """Non-clashing path: proforma.pdf, proforma (2).pdf, ... Considers
    both files already on disk and names taken earlier in this run."""
    candidate = os.path.join(dest_dir, filename)
    if candidate not in taken and not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(filename)
    n = 2
    while True:
        candidate = os.path.join(dest_dir, f"{stem} ({n}){ext}")
        if candidate not in taken and not os.path.exists(candidate):
            return candidate
        n += 1


def _candidate_items(outlook) -> list:
    """Mails to read, deduped: the open email first (strongest signal of
    intent), then the mail-list selection. Items seen by EntryID are only
    read once, so an open mail that is also selected uploads a single
    time. Missing EntryID means no dedupe for that item."""
    candidates: list = []
    seen_ids: set[str] = set()

    def _claim(item) -> bool:
        try:
            entry_id = item.EntryID
        except Exception:
            entry_id = None
        if entry_id is not None:
            if entry_id in seen_ids:
                return False
            seen_ids.add(str(entry_id))
        candidates.append(item)
        return True

    inspector = outlook.ActiveInspector()
    current = inspector.CurrentItem if inspector is not None else None
    if current is not None:
        _claim(current)
    explorer = outlook.ActiveExplorer()
    selection = list(explorer.Selection) if explorer is not None else []
    for item in selection:
        _claim(item)
    return candidates


# MAPI tag for PR_ATTACH_CONTENT_ID: set on images embedded in the body.
_CONTENT_ID_TAG = "http://schemas.microsoft.com/mapi/proptag/0x3712001F"


def _is_embedded(attachment) -> bool:
    """True for body-embedded images (logos, signatures), False for real
    attachments. Missing property means not embedded -- plain attachments
    simply don't carry a content ID."""
    try:
        content_id = attachment.PropertyAccessor.GetProperty(_CONTENT_ID_TAG)
    except Exception:
        return False
    return bool(content_id and str(content_id).strip())


def _connect_outlook():
    """Attach to a RUNNING Outlook via the running-object table, or None.

    Never falls back to Dispatch: that would silently launch a hidden
    Outlook with no explorer and no selection (which then lingers
    invisibly). No running Outlook means no selection to read.
    """
    try:
        from win32com.client import GetActiveObject
    except ImportError:
        return None
    try:
        return GetActiveObject("Outlook.Application")
    except Exception:
        return None
