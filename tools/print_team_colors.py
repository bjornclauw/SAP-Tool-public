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
