"""Supplier name lookup for the PRH Vendor field.

Reads the static tab-separated `private/suppliers.txt` file into a
{name: SAP_code} mapping and answers case-insensitive name queries. Pure
logic -- no UI. The combobox in the PRH tool window calls search_suppliers();
the mapping itself is loaded once and reused.

The same module owns the vendor CRUD table API (read_table / write_table /
validate_vendor): rows are the raw tab-split columns (col 4 = city, col 5 =
name, col 6 = code), leading non-data lines (empty line + header) travel
alongside untouched, and bytes round-trip exactly (latin-1, CRLF) when no
row changed.
"""

import os
import re

DATA_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "private", "suppliers.txt")

# Column indexes in the tab-separated file.
CITY_COL, NAME_COL, CODE_COL = 4, 5, 6

_CODE_RE = re.compile(r"\d{3,5}")


def load_suppliers() -> dict[str, str]:
    """Load supplier name -> SAP code mapping from tab-separated data file.
    Returns {name: code} for entries with a 4-5 digit numeric code."""
    mapping = {}
    if not os.path.exists(DATA_FILE):
        return mapping
    with open(DATA_FILE, encoding="latin-1") as f:
        for line in f:
            parts = line.strip("\n\r").split("\t")
            if len(parts) < 7:
                continue
            code = parts[6].strip()
            if not re.fullmatch(r"\d{3,5}", code):
                continue
            name = parts[5].strip()
            if name:
                mapping[name] = code
    return mapping


def search_suppliers(mapping: dict[str, str], query: str) -> list[tuple[str, str]]:
    """Return list of (name, code) matching the query (case-insensitive prefix or substring)."""
    if not query:
        return sorted(mapping.items(), key=lambda x: x[0].lower())
    q = query.lower()
    results = [(n, c) for n, c in mapping.items() if q in n.lower()]
    results.sort(key=lambda x: (not x[0].lower().startswith(q), x[0].lower()))
    return results


def _is_data_line(line: str) -> bool:
    """True when the line is a vendor row: 7+ columns with a numeric code.

    Only used to find where the leading head (empty lines, header, notes)
    ends -- everything after the first data line stays rows verbatim, so
    mid-file junk is never lost. Trade-off: a file whose FIRST rows carry
    non-numeric codes hides those rows in the head (the mapping couldn't
    use them anyway -- load_suppliers needs 3-5 digits).
    """
    parts = line.split("\t")
    return len(parts) >= 7 and _CODE_RE.fullmatch(parts[CODE_COL] or "") is not None


def read_table(path: str | None = None) -> tuple[str, list[str], list[list[str]]]:
    """Read the vendor file as (newline, head, rows).

    head holds the leading non-data lines (empty line + header) verbatim;
    rows holds every remaining line tab-split and padded to at least
    NAME_COL/CODE_COL columns. Short/junk lines stay rows (never dropped)
    so write_table() reproduces the file byte-for-byte when untouched.

    path defaults to DATA_FILE resolved at CALL time, so tests can patch
    the module constant (a def-time default would pin the real file). A
    missing private file yields an empty table instead of breaking PRH.
    """
    if path is None:
        path = DATA_FILE
    if not os.path.exists(path):
        return "\r\n", [], []
    with open(path, "rb") as f:
        raw = f.read().decode("latin-1")
    newline = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.split(newline)
    if lines and lines[-1] == "":
        lines.pop()  # trailing newline, re-added on write
    head = []
    while lines and not _is_data_line(lines[0]):
        head.append(lines.pop(0))
    rows = []
    for line in lines:
        parts = line.split("\t")
        while len(parts) < 7:
            parts.append("")
        rows.append(parts)
    return newline, head, rows


def write_table(newline: str, head: list[str], rows: list[list[str]],
                path: str | None = None) -> None:
    """Write head + rows back with the detected newline (latin-1)."""
    if path is None:
        path = DATA_FILE
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as f:
        f.write((newline.join(head + ["\t".join(r) for r in rows]) + newline).encode("latin-1"))


def validate_vendor(name: str, code: str) -> str | None:
    """Validate a vendor edit. Returns an error message, or None when valid.

    The code rule mirrors load_suppliers(): anything but 3-5 digits never
    reaches the PRH combobox, so accepting it would silently lose the entry.
    """
    if not (name or "").strip():
        return "Name is required."
    if not re.fullmatch(r"\d{3,5}", (code or "").strip()):
        return "Code must be 3-5 digits (anything else stays invisible in PRH)."
    return None


def combo_entries(rows: list[list[str]]) -> list[tuple[str, str, str]]:
    """Picker entries as (display, name, code) for table rows.

    One display string per pickable master: unique names show plain, while
    duplicate names (same vendor name on several masters -- e.g. two
    cities) get a city suffix so the picker can offer every row instead of
    collapsing them like the {name: code} mapping does. Rows no mapping
    could ever see (empty name, non-numeric code) are skipped -- the vendor
    manager flags those instead of the picker hiding them silently.
    """
    usable: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for parts in rows:
        name = parts[NAME_COL].strip() if len(parts) > NAME_COL else ""
        code = parts[CODE_COL].strip() if len(parts) > CODE_COL else ""
        city = parts[CITY_COL].strip() if len(parts) > CITY_COL else ""
        if not name or _CODE_RE.fullmatch(code) is None:
            continue
        if (name, city, code) in seen:
            continue  # byte-identical masters: same code either way, one entry
        seen.add((name, city, code))
        usable.append((name, city, code))
    by_name: dict[str, list[int]] = {}
    for i, (name, _city, _code) in enumerate(usable):
        by_name.setdefault(name, []).append(i)

    ordered = sorted(by_name, key=str.lower)
    entries: list[tuple[str, str, str]] = []
    used: set[str] = set()
    # Unique names first (plain display), dupes after (suffixed) -- a plain
    # name can never steal a suffixed display this way.
    for name in [n for n in ordered if len(by_name[n]) == 1]:
        entries.append((name, name, usable[by_name[name][0]][2]))
        used.add(name)
    for name in [n for n in ordered if len(by_name[n]) > 1]:
        for i in by_name[name]:
            _n, city, code = usable[i]
            candidates = (
                [f"{name} ({city})"] if city else []
            ) + [f"{name} [{code}]"]
            if city:
                candidates.append(f"{name} ({city}) [{code}]")
            display = next((d for d in candidates if d not in used), name)
            used.add(display)
            entries.append((display, name, code))
    entries.sort(key=lambda e: (e[1].lower(), e[0].lower()))
    return entries
