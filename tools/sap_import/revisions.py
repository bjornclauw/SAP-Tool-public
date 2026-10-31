"""Weeknumber + revisions register (MAX 5) + revision grammar.

Grammar (the Excel's `WxxL3` spelling is obsolete):
  Backlog revision   BL<yy>L<n>  (line) or BL<yy>W<n>  (drum dryer)
  Downtime revision  <WW><yy><lines>   e.g. week 42, year 26, lines L3+L4
                                       -> "4226L3L4"
Only production lines (L3-L7) are generated into the register.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MAX_REVISIONS = 5
_DATA_PATH = Path(__file__).parent.parent.parent / "data" / "revisions.json"

_BACKLOG_RE = re.compile(r"^BL(\d{2})([LW])(\d+)$")
_DOWNTIME_RE = re.compile(r"^(\d{2})(\d{2})((?:L\d{1,2})+)$")
_LINE_KEYS = ("L3", "L4", "L5", "L6", "L7")


@dataclass(frozen=True)
class BacklogRevision:
    year: int   # 2-digit year, e.g. 26
    kind: str   # "line" or "dryer"
    number: int  # line / drum-dryer number


@dataclass(frozen=True)
class DowntimeRevision:
    week: int             # 1-53
    year: int             # 2-digit year, e.g. 26
    lines: tuple[int, ...]  # production lines the revision covers



def week_key(dt: datetime | None = None) -> str:
    """ISO week key like '2026-W35'."""
    d = dt or datetime.now()
    iso_year, iso_week, _ = d.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def current_week_number(dt: datetime | None = None) -> int:
    return (dt or datetime.now()).isocalendar()[1]


def current_year(dt: datetime | None = None) -> int:
    return (dt or datetime.now()).isocalendar()[0]


def _two_digit_year(year: int) -> int:
    return year % 100


def parse_backlog_revision(rev: str | None) -> BacklogRevision | None:
    """Parse a backlog revision (``BL26L3`` / ``BL26W3``) into its parts.

    Returns None for anything that isn't a backlog revision (a downtime
    revision, a custom code, empty).
    """
    match = _BACKLOG_RE.match((rev or "").strip().upper())
    if match is None:
        return None
    return BacklogRevision(
        year=int(match.group(1)),
        kind="line" if match.group(2) == "L" else "dryer",
        number=int(match.group(3)),
    )


def upgrade_backlog_year(rev: str, year: int | None = None) -> str:
    """Rewrite a backlog revision's year to `year` (default: current year).

    Non-backlog revisions are returned unchanged.
    """
    parsed = parse_backlog_revision(rev)
    if parsed is None:
        return rev
    yy = _two_digit_year(year or current_year())
    prefix = "L" if parsed.kind == "line" else "W"
    return f"BL{yy:02d}{prefix}{parsed.number}"


def is_year_upgrade(original: str | None, current: str | None) -> bool:
    """True when `current` is `original` with only its 2-digit year changed."""
    a = parse_backlog_revision(original)
    b = parse_backlog_revision(current)
    return (a is not None and b is not None
            and a.kind == b.kind and a.number == b.number and a.year != b.year)


def parse_downtime_revision(rev: str | None) -> DowntimeRevision | None:
    """Parse a downtime revision (``4226L3L4``) into its parts.

    Returns None for anything that isn't a generated downtime revision
    (a backlog revision, a custom code, empty).
    """
    match = _DOWNTIME_RE.match((rev or "").strip().upper())
    if match is None:
        return None
    lines = tuple(int(n) for n in re.findall(r"L(\d+)", match.group(3)))
    return DowntimeRevision(week=int(match.group(1)), year=int(match.group(2)),
                            lines=lines)


def match_downtime_revision(rev: str | None,
                            register: list[str] | None) -> str | None:
    """The first register entry covering a production-line backlog revision.

    Matching is by line number, not by year: the register entry already carries
    the active week's week + year, so ``BL26L3`` and ``BL27L3`` both land on
    ``4227L3L4`` when that entry covers L3. Drum dryers, blank/custom
    revisions and lines no entry covers return None.
    """
    parsed = parse_backlog_revision(rev)
    if parsed is None or parsed.kind != "line":
        return None
    for candidate in register or []:
        downtime = parse_downtime_revision(candidate)
        if downtime is not None and parsed.number in downtime.lines:
            return candidate
    return None


def downtime_prefix(week_num: int, year: int | None = None) -> str:
    """The ``<WW><yy>`` head shared by every downtime revision for a week."""
    return f"{week_num:02d}{_two_digit_year(year or current_year()):02d}"


def generate_downtime_revision(week_num: int, year: int, lines: list[int]) -> str:
    """<WW><yy><lines>, e.g. week 42, 2026, lines [3, 4] -> "4226L3L4"."""
    suffix = "".join(f"L{n}" for n in lines)
    return f"{downtime_prefix(week_num, year)}{suffix}"


def _load_all() -> dict:
    if not _DATA_PATH.exists():
        return {}
    try:
        data = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_all(data: dict):
    _DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DATA_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_week_key(wk: str) -> tuple[int, int]:
    # "2026-W35" -> (2026, 35)
    try:
        y, w = wk.split("-W")
        return int(y), int(w)
    except Exception:
        return current_year(), current_week_number()


def generate_from_checkboxes(week_num: int, checks: dict, year: int | None = None) -> list[str]:
    """Generate the week's downtime revisions in the new syntax:
      L3 alone -> <WW><yy>L3
      L4 alone -> <WW><yy>L4
      L3 & L4 -> single <WW><yy>L3L4
      L5/L6 same as L3/L4
      L7 alone -> <WW><yy>L7 (separate)
    Production lines only; drum dryers are never generated.
    Returns up to 3 entries, order L3L4-group, L5L6-group, L7.
    """
    revs: list[str] = []
    l3 = bool(checks.get("L3"))
    l4 = bool(checks.get("L4"))
    l5 = bool(checks.get("L5"))
    l6 = bool(checks.get("L6"))
    l7 = bool(checks.get("L7"))
    prefix = downtime_prefix(week_num, year)
    if l3 and l4:
        revs.append(f"{prefix}L3L4")
    elif l3:
        revs.append(f"{prefix}L3")
    elif l4:
        revs.append(f"{prefix}L4")
    if l5 and l6:
        revs.append(f"{prefix}L5L6")
    elif l5:
        revs.append(f"{prefix}L5")
    elif l6:
        revs.append(f"{prefix}L6")
    if l7:
        revs.append(f"{prefix}L7")
    return revs[:MAX_REVISIONS]


def get_checkboxes(week: str | None = None) -> dict:
    wk = week or week_key()
    data = _load_all()
    entry = data.get(wk)
    if isinstance(entry, dict):
        # new format: {"revisions": [...], "checks": {...}}
        checks = entry.get("checks", {})
        if isinstance(checks, dict):
            return {k: bool(checks.get(k)) for k in _LINE_KEYS}
    return {k: False for k in _LINE_KEYS}


def set_checkboxes(checks: dict, week: str | None = None) -> list[str]:
    """Persist checkboxes for week and regenerate revisions into register.
    Returns generated revisions. Overwrites previous register for that week.
    """
    wk = week or week_key()
    yr, wn = parse_week_key(wk)
    clean_checks = {k: bool(checks.get(k)) for k in _LINE_KEYS}
    revs = generate_from_checkboxes(wn, clean_checks, year=yr)
    data = _load_all()
    data[wk] = {"revisions": revs, "checks": clean_checks}
    _save_all(data)
    return revs


def get_revisions(week: str | None = None) -> list[str]:
    wk = week or week_key()
    data = _load_all()
    entry = data.get(wk)
    if entry is None:
        return []
    if isinstance(entry, dict):
        vals = entry.get("revisions", [])
    elif isinstance(entry, list):
        # legacy MAX 5 list format
        vals = entry
    else:
        return []
    if not isinstance(vals, list):
        return []
    cleaned = [str(v).strip() for v in vals if str(v).strip()]
    return cleaned[:MAX_REVISIONS]


def set_revisions(revisions: list[str], week: str | None = None):
    """Manual override of register (edit popup). Preserves checkboxes."""
    wk = week or week_key()
    cleaned = [str(v).strip() for v in revisions if str(v).strip()][:MAX_REVISIONS]
    data = _load_all()
    entry = data.get(wk)
    checks = {}
    if isinstance(entry, dict):
        checks = entry.get("checks", {})
    if cleaned:
        data[wk] = {"revisions": cleaned, "checks": checks}
    else:
        # keep checks but empty revisions? clear entry if no checks either
        if checks and any(checks.values()):
            data[wk] = {"revisions": [], "checks": checks}
        else:
            data.pop(wk, None)
    _save_all(data)


def data_path() -> Path:
    return _DATA_PATH
