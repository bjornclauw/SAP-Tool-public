"""The Import grid's runtime layout, kept out of the tracked config.

Column visibility, order, display names and per-column widths are per-user UI
state, so they live in an untracked ``data/si_grid.json``. The tracked
``toolbar_config.json`` ``si_import_grid`` node is only the seed for a fresh
install; this file overrides it and is what the Columns menu writes to.
"""

from __future__ import annotations

import json
from pathlib import Path

_DATA_PATH = Path(__file__).parent.parent.parent / "data" / "si_grid.json"


def _clean_list(values) -> list[str]:
    out: list[str] = []
    for v in values if isinstance(values, list) else []:
        text = str(v).strip()
        if text and text not in out:
            out.append(text)
    return out


def _clean_names(values) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}
    return {str(k): str(v) for k, v in values.items()}


def _clean_widths(values) -> dict[str, int]:
    if not isinstance(values, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in values.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _clean(settings: dict) -> dict:
    return {
        "visible_columns": _clean_list(settings.get("visible_columns", [])),
        "column_order": _clean_list(settings.get("column_order", [])),
        "column_display_names": _clean_names(settings.get("column_display_names", {})),
        "column_widths": _clean_widths(settings.get("column_widths", {})),
    }


def _load_raw() -> dict:
    if not _DATA_PATH.exists():
        return {}
    try:
        data = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def default_settings(config_node: dict) -> dict:
    """The seed layout from the tracked config (what a fresh install shows)."""
    return _clean(config_node)


def load_settings(config_node: dict) -> dict:
    """The seed layout with any saved runtime overrides applied key by key."""
    settings = default_settings(config_node)
    raw = _load_raw()
    for key in settings:
        if key in raw:
            settings[key] = _clean({key: raw[key]})[key]
    return settings


def save_settings(settings: dict) -> None:
    _DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DATA_PATH.write_text(json.dumps(_clean(settings), indent=2, ensure_ascii=False),
                          encoding="utf-8")


def file_path() -> Path:
    return _DATA_PATH
