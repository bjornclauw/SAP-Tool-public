"""The Import grid's Excel palette.

One place for the cell colours the sheet uses. ``cell_colour(row, column)`` is a
pure function: it maps a queue row and a column name to the background colour
the grid should paint that cell, or ``None`` to leave it unpainted. ImportGrid
is the only caller.

The rules and hex values are lifted from the workbook's conditional formatting
(``2SAP_v2_2.xlsm``, ``Import`` sheet). Each rule below names the Excel site it
comes from so a future change can be checked against the sheet. These are the
workbook's data colours, not the app theme -- ``ui_styles.COLORS`` still owns
the app's own chrome.
"""

from __future__ import annotations

from .models import auto_upload_eligible

# ---- the palette (Excel fills, RGB) ---------------------------------------
COLOURS = {
    "green": "#00B050",        # status 2, 7; PG WP1
    "red": "#FF0000",          # status 3
    "orange": "#FFC000",       # status 9, 4-5
    "blue": "#0070C0",         # status 6
    "yellow": "#FFFF00",       # status 10 (cancelled)
    "orange_red": "#FF3300",   # PG WP2
    "blue_dark": "#006DD0",    # PG WP3
    # Tinted theme colours, computed with the OOXML tint formula.
    "light_amber": "#FFF2CC",  # WO_TYPE ZA03; WO? = 0 (melding)
    "light_blue": "#DEEBF7",   # WO_TYPE ZA02
    "green_medium": "#C6DEB5", # REV present (!= 0)
}

# ---- priority scale (Excel 3-point colour scale on PRIOR) ------------------
_SCALE_RED = "#FF0000"
_SCALE_YELLOW = "#FFEB84"
_SCALE_GREEN = "#00B050"

# ---- StatusID (column A) --------------------------------------------------
_STATUS_COLOURS = {
    "2": COLOURS["green"],
    "3": COLOURS["red"],
    "4": COLOURS["orange"],    # cancel requested (amber)
    "5": COLOURS["orange"],
    "6": COLOURS["blue"],
    "7": COLOURS["green"],
    "9": COLOURS["orange"],
    "10": COLOURS["yellow"],   # cancelled
}

# ---- Planner group (column G, values WP1/WP2/WP3) -------------------------
_PG_COLOURS = {
    "WP1": COLOURS["green"],
    "WP2": COLOURS["orange_red"],
    "WP3": COLOURS["blue_dark"],
}

# ---- Order type (column J) ------------------------------------------------
_WO_TYPE_COLOURS = {
    "ZA03": COLOURS["light_amber"],
    "ZA02": COLOURS["light_blue"],
}


def _cell_text(value) -> str:
    return str(value).strip() if value is not None else ""


def _lookup(table: dict, row: dict, key: str) -> str | None:
    return table.get(_cell_text(row.get(key)))


def _mix(c1: str, c2: str, t: float) -> str:
    a = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(c2[i:i + 2], 16) for i in (1, 3, 5))
    out = tuple(round(x + (y - x) * t) for x, y in zip(a, b))
    return f"#{out[0]:02X}{out[1]:02X}{out[2]:02X}"


def priority_colour(value) -> str | None:
    """Excel's colour scale on PRIOR: 1 red, 3 yellow, 4 green."""
    try:
        v = float(_cell_text(value))
    except ValueError:
        return None
    if v <= 1:
        return _SCALE_RED
    if v >= 4:
        return _SCALE_GREEN
    if v <= 3:
        return _mix(_SCALE_RED, _SCALE_YELLOW, (v - 1) / 2)
    return _mix(_SCALE_YELLOW, _SCALE_GREEN, (v - 3) / 1)


def cell_colour(row: dict, column: str) -> str | None:
    """The Excel background colour for one cell, or None to leave it plain."""
    if column == "Status":
        return _lookup(_STATUS_COLOURS, row, "Status")
    if column == "PG":
        return _lookup(_PG_COLOURS, row, "PG")
    if column == "WO_TYPE":
        return _lookup(_WO_TYPE_COLOURS, row, "WO_TYPE")
    if column == "WO":
        # WO? = 0 means a melding: no work order yet.
        return COLOURS["light_amber"] if _cell_text(row.get("WO")) == "0" else None
    if column == "REV":
        rev = _cell_text(row.get("REV"))
        if not rev or rev == "0":
            return None
        # A revision the user overrode and then sent back to the backlog (or
        # picked by hand) is amber -- the same yellow as PRIOR 3 -- so the
        # cell shows the revision's state, independent of upload eligibility.
        if row.get("_auto_assign_skip") and not row.get("_auto_assigned"):
            return _SCALE_YELLOW
        return COLOURS["green_medium"]
    if column == "PRIOR":
        return priority_colour(row.get("PRIOR"))
    return None


_ELIGIBLE_GREY = "#B0B4B9"


def cell_text_colour(row: dict, column: str) -> str | None:
    """The foreground colour for one cell, or None to leave it plain.

    Only the Auto indicator uses this: a green dot when the next auto-upload
    tick would send the row, a grey dot when it would not.
    """
    if column == "Auto":
        return COLOURS["green"] if auto_upload_eligible(row) else _ELIGIBLE_GREY
    return None
