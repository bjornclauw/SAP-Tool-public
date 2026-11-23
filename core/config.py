"""JSON config I/O -- the only place button text, default values, scripts,
and column mappings live. Python code should never hardcode a UI string that
you might want to tweak later; read it from here instead.

Two files, split by ownership:

- ``config/toolbar_config.json`` (tracked): the app's shipped config. Main
  toolbar entries live here as stubs -- id, label, var name -- with no script.
  App-owned tool-window scripts and power-user button text also ship here.
- ``data/user_toolbar.json`` (ignored): every per-machine end-user edit --
  main-toolbar entries, tool-window script overrides (see
  ``core/script_overrides``), plus any user-changed default value, label, or
  column layout for the app and its tool windows. Only values that differ from
  the shipped file are stored, so shipped changes still reach everything the
  user did not touch.

``save_config`` routes main-toolbar scripts and power-user button text to the
tracked config, and every other user-edited value to the ignored user file.
Tool-window scripts are personal overrides and are recorded by
``core/script_overrides``, never by this split. Updates hard-reset tracked
files, so end-user preferences must never live there.

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
    """Whether the main toolbar owns this entry's script."""
    return entry_id.startswith(USER_MAIN_PREFIX)


def _read_json_file(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_config(path: Path | None = None, user_path: Path | None = None) -> dict:
    """The shipped config with the user's overrides applied.

    An explicit ``path`` is treated as a standalone file for tests and tools:
    the user layer is only merged for the real, default config.
    """
    resolved = _resolve(path)
    shipped = _read_json_file(resolved)
    if path is not None and user_path is None:
        return shipped
    if not isinstance(shipped.get("customcomponents"), dict):
        return shipped
    overrides = _load_overrides(_resolve_user(user_path))
    merged = _merge(shipped, overrides) if overrides else shipped
    from . import script_overrides

    return script_overrides.apply_overrides(merged, shipped, user_path)


def load_shipped_config(path: Path | None = None) -> dict:
    """The tracked config exactly as shipped, without the user layer."""
    return _read_json_file(_resolve(path))


def load_user_file(user_path: Path | None = None) -> dict:
    """The raw ignored user file; empty when missing or unreadable."""
    return _load_overrides(_resolve_user(user_path))


def save_user_file(data: dict, user_path: Path | None = None) -> None:
    """Atomically write the ignored user file."""
    _write_json(_resolve_user(user_path), data)


def save_config(
    data: dict, path: Path | None = None, user_path: Path | None = None
) -> None:
    """Write app-owned scripts/power-user button edits to the shipped config
    and every end-user preference (defaults, labels, columns) to the ignored
    user file. The shipped config never receives a main-toolbar user script."""
    if path is None and user_path is None and isinstance(
        data.get("customcomponents"), dict
    ):
        app_data, overrides = _split(data, _resolve(path))
        # Keep sections owned by other modules (e.g. script_overrides).
        existing_user = _load_overrides(_resolve_user(user_path))
        for key, value in existing_user.items():
            if key != "customcomponents":
                overrides[key] = copy.deepcopy(value)
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
    """Split a merged config into (app config to write, user overrides).

    Main-toolbar entries stay wholly user-owned. Tool-window button edits are
    power-user development changes and ship in the tracked config. Every other
    user-changed field (defaults, labels, column layouts) goes to the ignored
    user file.
    """
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
            base = app_entries.get(entry_id, _MISSING)
            if is_user_entry(entry_id) or not isinstance(entry, dict):
                difference = _diff(entry, base)
                if difference is not _MISSING:
                    user_entries[entry_id] = difference
                continue

            if component_type == "buttons":
                # Tool-window button text is a power-user development change
                # and ships. Its script is a personal override owned by
                # core/script_overrides, so this split never writes it.
                if isinstance(entry, dict):
                    editable = {
                        key: value for key, value in entry.items()
                        if key != "script"
                    }
                else:
                    editable = entry
                if isinstance(base, dict):
                    base_editable = {
                        key: value for key, value in base.items()
                        if key != "script"
                    }
                else:
                    base_editable = base
                difference = _diff(editable, base_editable)
                if difference is not _MISSING:
                    if isinstance(base, dict):
                        app_entries[entry_id].update(copy.deepcopy(difference))
                    else:
                        app_entries[entry_id] = copy.deepcopy(editable)
                continue

            script = entry.get("script", _MISSING)
            user_entry = {
                key: value for key, value in entry.items() if key != "script"
            }
            difference = _diff(user_entry, base)
            if difference is not _MISSING:
                user_entries[entry_id] = difference

            # Scripts keep their existing ownership: app-owned entries ship.
            if script is not _MISSING:
                if not isinstance(base, dict):
                    app_entries[entry_id] = {"script": script}
                elif script != base.get("script", _MISSING):
                    app_entries[entry_id]["script"] = script

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
