"""Per-machine UI settings, kept out of the tracked configuration.

These values used to live in ``config/toolbar_config.json`` under
``customcomponents._meta``. That made every dock/debug/fake toggle dirty a
tracked file, which blocks clean updates and overwrites defaults. They now
live in an ignored ``data/user_settings.json``.

Loading migrates a legacy ``_meta`` block once, so an existing machine keeps
its choices after the split.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent.parent / "data" / "user_settings.json"

DEFAULTS: dict[str, object] = {
    "fake_sap": False,
    "show_console": False,
    "dock_enabled": False,
    "dock_autohide": False,
    "always_on_top": False,
    "debug_mode": False,
    "power_user": False,
    # SAP Import setup, seeded from config/textinput on a fresh install.
    "si_pg": "WP1",
    "si_system": "PRD400",
    "si_upload_pics": "YES",
    # Print Orders behaviour (per-machine).
    "po_auto_merge": True,
    "po_stop_on_error": False,
    "po_max_attachments": "4",
    "po_pdf_layout": "auto",
}


def _resolve(path: Path | None) -> Path:
    # Call-time lookup so tests can point DEFAULT_PATH at a temp copy.
    return path if path is not None else DEFAULT_PATH


def load_user_settings(path: Path | None = None) -> dict:
    """Saved settings merged over the defaults; reads legacy ``_meta`` once."""
    p = _resolve(path)
    data: dict = {}
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except Exception:
            data = {}
    else:
        legacy = _migrate_legacy()
        if legacy:
            data = legacy
            save_user_settings(data, p)

    merged = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        value = data.get(key, default)
        if isinstance(default, bool):
            merged[key] = bool(value)
        else:
            merged[key] = str(value) if value is not None else default
    for key, value in data.items():
        merged.setdefault(key, value)
    return merged


def save_user_settings(data: dict, path: Path | None = None) -> None:
    """Atomic write, same pattern as the main config."""
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def update_user_settings(updates: dict, path: Path | None = None) -> dict:
    """Merge ``updates`` and write only when something actually changed."""
    current = load_user_settings(path)
    if all(current.get(key) == value for key, value in updates.items()):
        return current
    current.update(updates)
    save_user_settings(current, path)
    return current


def _migrate_legacy() -> dict:
    """Read the old ``customcomponents._meta`` block, if it is still there."""
    try:
        from core import config as cfg

        data = cfg.load_config()
        meta = data.get("customcomponents", {}).get("_meta", {})
        if not isinstance(meta, dict):
            return {}
        return {key: bool(value) for key, value in meta.items() if key in DEFAULTS}
    except Exception:
        return {}
