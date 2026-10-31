"""JSON config I/O -- the only place button text, default values, scripts,
and column mappings live. Python code should never hardcode a UI string that
you might want to tweak later; read it from here instead.

Two files, split by ownership:

- ``config/toolbar_config.json`` (tracked): the app's shipped config. The main
  toolbar entries live here as stubs -- id, label, var name -- with no script.
- ``data/user_toolbar.json`` (ignored): the user's main-toolbar customisations.
  Only values that differ from the shipped stubs are stored, so shipped changes
  still reach every button the user did not touch.

An entry whose id starts with ``toolbar_`` is user-owned; everything else is
app-owned. ``load_config`` merges the two, ``save_config`` routes each edit back
to the file it belongs in.

Layout, however, is NOT config-driven on purpose (see sap_toolbar.py).
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config" / "toolbar_config.json"
USER_TOOLBAR_PATH = Path(__file__).parent.parent / "data" / "user_toolbar.json"

USER_MAIN_PREFIX = "toolbar_"
_MISSING = object()


def _resolve(path: Path | None) -> Path:
    # Resolve at call time (not as a def-time default) so tests can point
    # DEFAULT_CONFIG_PATH at a temp copy and never touch the real file.
    return path if path is not None else DEFAULT_CONFIG_PATH


def _resolve_user(path: Path | None) -> Path:
    return path if path is not None else USER_TOOLBAR_PATH


def is_user_entry(entry_id: str) -> bool:
    """Whether the main toolbar (and therefore the user) owns this entry."""
    return entry_id.startswith(USER_MAIN_PREFIX)


def _read_json_file(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_config(path: Path | None = None, user_path: Path | None = None) -> dict:
    """The shipped config with the user's main-toolbar overrides applied.

    An explicit ``path`` is treated as a standalone file for tests and tools:
    the user layer is only merged for the real, default config.
    """
    resolved = _resolve(path)
    data = _read_json_file(resolved)
    if path is not None and user_path is None:
        return data
    if not isinstance(data.get("customcomponents"), dict):
        return data
    overrides = _load_overrides(_resolve_user(user_path))
    if not overrides:
        return data
    return _merge(data, overrides)


def save_config(
    data: dict, path: Path | None = None, user_path: Path | None = None
) -> None:
    """Write app-owned entries to the shipped config and toolbar edits to the
    user file. The shipped config never receives a user script."""
    if path is None and user_path is None and isinstance(
        data.get("customcomponents"), dict
    ):
        app_data, overrides = _split(data, _resolve(path))
        _write_json(_resolve(path), app_data)
        _write_json(_resolve_user(user_path), overrides)
        return
    _write_json(_resolve(path), data)


def _write_json(path: Path, data: dict) -> None:
    # Write to a sibling then os.replace, so a reader (another window, a test
    # run) never sees a half-written file. os.replace is atomic on Windows.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _load_overrides(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _merge(base: dict, overrides: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _merge(current, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _diff(value: object, base: object) -> object:
    """Only the parts of ``value`` that differ from ``base`` (``_MISSING``)."""
    if isinstance(value, dict) and (isinstance(base, dict) or base is _MISSING):
        base_dict = base if isinstance(base, dict) else {}
        out: dict = {}
        for key, child in value.items():
            difference = _diff(child, base_dict.get(key, _MISSING))
            if difference is not _MISSING:
                out[key] = difference
        return out if out else _MISSING
    if base is not _MISSING and value == base:
        return _MISSING
    return copy.deepcopy(value)


def _split(data: dict, shipped_path: Path) -> tuple[dict, dict]:
    """Split a merged config into (app config to write, user overrides)."""
    try:
        shipped = _read_json_file(shipped_path)
    except (OSError, ValueError):
        shipped = {}
    app_data = copy.deepcopy(shipped)
    app_components = app_data.setdefault("customcomponents", {})
    overrides: dict = {"customcomponents": {}}

    for key, value in data.items():
        if key != "customcomponents":
            app_data[key] = copy.deepcopy(value)

    for component_type, entries in data.get("customcomponents", {}).items():
        if not isinstance(entries, dict):
            app_components[component_type] = copy.deepcopy(entries)
            continue
        app_entries = app_components.setdefault(component_type, {})
        user_entries: dict = {}
        for entry_id, entry in entries.items():
            if not is_user_entry(entry_id):
                app_entries[entry_id] = copy.deepcopy(entry)
                continue
            base = app_entries.get(entry_id, _MISSING)
            difference = _diff(entry, base)
            if difference is not _MISSING:
                user_entries[entry_id] = difference
        if user_entries:
            overrides["customcomponents"][component_type] = user_entries
    if not overrides["customcomponents"]:
        overrides = {}
    return app_data, overrides


def get_button_cfg(config: dict, button_id: str) -> dict:
    return config.get("customcomponents", {}).get("buttons", {}).get(button_id, {})


def get_textinput_cfg(config: dict, entry_id: str) -> dict:
    return config.get("customcomponents", {}).get("textinput", {}).get(entry_id, {})


def get_header_cfg(config: dict, header_id: str) -> dict:
    return config.get("customcomponents", {}).get("headers", {}).get(header_id, {})


def get_grid_cfg(config: dict, grid_id: str) -> dict:
    return config.get("customcomponents", {}).get("datagridview", {}).get(grid_id, {})
