"""Stable Print Orders team colors with optional per-machine overrides."""

from __future__ import annotations

import hashlib
import re

_PALETTE = (
    "#b86a15",
    "#00796b",
    "#5c6bc0",
    "#2e7d32",
    "#795548",
    "#6d65e8",
    "#8e44ad",
    "#546e7a",
)
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _key(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def contrast_text(color: object) -> str:
    """Readable black or white text for a ``#rrggbb`` team color."""
    if hasattr(color, "hexval"):
        color = color.hexval()
    value = str(color or "").strip().lower().lstrip("#")
    if value.startswith("0x"):
        value = value[2:]
    if not _HEX_COLOR.fullmatch(f"#{value}"):
        return "#ffffff"
    channels = [int(value[index:index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [channel / 12.92 if channel <= .04045
              else ((channel + .055) / 1.055) ** 2.4 for channel in channels]
    luminance = .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2]
    return "#1a1a1a" if luminance > .45 else "#ffffff"


def team_color(workcenter: object, overrides: dict | None = None) -> str:
    """Return a stable team color, preferring a valid saved override.

    Workcenter matching is case-insensitive and ignores surrounding/repeated
    whitespace. Removing an override automatically restores the generated color.
    """
    key = _key(workcenter)
    if key and isinstance(overrides, dict):
        for name, color in overrides.items():
            if _key(name) == key and isinstance(color, str) and _HEX_COLOR.fullmatch(color):
                return color.lower()

    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return _PALETTE[int.from_bytes(digest[:4], "big") % len(_PALETTE)]
