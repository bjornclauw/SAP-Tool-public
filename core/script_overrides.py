"""Per-user overrides for tool-window button scripts.

``config/toolbar_config.json`` keeps the shipped script. A local edit lives in
``data/user_toolbar.json`` under ``script_overrides``, each entry carrying the
user's script, the base script it was edited from and that base's hash.

``core.config.load_config`` applies the resolved script to the returned
buttons. Only tool-window buttons are handled here; main-toolbar scripts keep
their existing user-file routing in ``core.config``.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path

from . import config as cfg
from .updater import git_executable, no_window_flags

MERGE_TIMEOUT_S = 30

OVERRIDES_KEY = "script_overrides"
STATUS_MODIFIED = "modified"
STATUS_REVIEW = "review"

# Resolved statuses, keyed by (user file, button id, shipped script hash).
# ``load_config`` fills this while the window is still withdrawn, so opening
# Settings does not run Git on the Tk thread again. A changed shipped script
# produces a new key and is recomputed.
_STATUS_CACHE: dict[tuple[str, str, str], tuple[str, str]] = {}

# Which tool a button belongs to. Prefixes cover whole families; the two
# odd names are listed exactly. A button that maps to nothing (main toolbar)
# is deliberately not shown in the catalogue.
TOOL_BUTTON_PREFIXES = {
    "purchase_request": ("prh_", "atb_"),
    "print_orders": ("po_", "os_"),
    "sap_import": ("si_",),
    "planner": (),
    "revisions": (),
}
TOOL_EXTRA_BUTTONS = {
    "proforma_upload_btn": "purchase_request",
    "refresh_attachments_btn": "purchase_request",
}


def normalize_script(text: object) -> str:
    """One line-ending form for storage, hashing and comparison."""
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


def script_hash(text: object) -> str:
    return hashlib.sha256(normalize_script(text).encode("utf-8")).hexdigest()


def load_overrides(user_path: Path | None = None) -> dict:
    data = cfg.load_user_file(user_path)
    overrides = data.get(OVERRIDES_KEY)
    return overrides if isinstance(overrides, dict) else {}


def clear_status_cache() -> None:
    """Forget cached resolutions (tests and a manual config reload)."""
    _STATUS_CACHE.clear()


def _path_key(user_path: Path | None) -> str:
    return str(user_path if user_path is not None else cfg.USER_TOOLBAR_PATH)


def _cached_resolve(
    entry: dict,
    shipped_script: str,
    button_id: str,
    user_path: Path | None,
) -> tuple[str, str]:
    key = (_path_key(user_path), button_id, script_hash(shipped_script))
    cached = _STATUS_CACHE.get(key)
    if cached is not None:
        return cached
    resolved = resolve_override(entry, shipped_script)
    _STATUS_CACHE[key] = resolved
    return resolved


def _invalidate(button_id: str) -> None:
    for key in [key for key in _STATUS_CACHE if key[1] == button_id]:
        _STATUS_CACHE.pop(key, None)


def tool_for_button(button_id: str) -> str | None:
    """The catalogue tool that owns this button script, if any."""
    if button_id in TOOL_EXTRA_BUTTONS:
        return TOOL_EXTRA_BUTTONS[button_id]
    for tool, prefixes in TOOL_BUTTON_PREFIXES.items():
        if any(button_id.startswith(prefix) for prefix in prefixes):
            return tool
    return None


def button_ids_for_tool(
    tool_key: str, shipped_config: dict | None = None
) -> list[str]:
    config = (shipped_config if shipped_config is not None
              else cfg.load_shipped_config())
    buttons = config.get("customcomponents", {}).get("buttons", {})
    return [button_id for button_id in buttons
            if tool_for_button(button_id) == tool_key]


def statuses_for_tool(
    tool_key: str,
    *,
    shipped_config: dict | None = None,
    user_path: Path | None = None,
) -> dict[str, tuple[str, str]]:
    """Overridden scripts of one tool: button id -> (effective, status)."""
    config = (shipped_config if shipped_config is not None
              else cfg.load_shipped_config())
    overrides = load_overrides(user_path)
    buttons = config.get("customcomponents", {}).get("buttons", {})
    found: dict[str, tuple[str, str]] = {}
    for button_id in button_ids_for_tool(tool_key, config):
        entry = overrides.get(button_id)
        if not isinstance(entry, dict):
            continue
        shipped = buttons.get(button_id)
        if not isinstance(shipped, dict):
            continue
        found[button_id] = _cached_resolve(
            entry, shipped.get("script", ""), button_id, user_path)
    return found


def _shipped_script(button_id: str, shipped_config: dict | None = None):
    config = (shipped_config if shipped_config is not None
              else cfg.load_shipped_config())
    entry = cfg.get_button_cfg(config, button_id)
    if not entry:
        return None
    return entry.get("script", "")


def record_tool_script(
    button_id: str,
    script: str,
    *,
    shipped_config: dict | None = None,
    user_path: Path | None = None,
) -> bool:
    """Store a tool-window override, or clear it when the edit is the default.

    Main-toolbar ids are ignored: those scripts keep their existing routing.
    Returns whether the user file changed.
    """
    if cfg.is_user_entry(button_id):
        return False
    return record_override(
        button_id, script, shipped_config=shipped_config, user_path=user_path)


def record_override(
    button_id: str,
    script: str,
    *,
    shipped_config: dict | None = None,
    user_path: Path | None = None,
) -> bool:
    base = _shipped_script(button_id, shipped_config)
    if base is None:
        return False
    base = normalize_script(base)
    script = normalize_script(script)

    data = cfg.load_user_file(user_path)
    overrides = data.get(OVERRIDES_KEY)
    if not isinstance(overrides, dict):
        overrides = {}

    changed = False
    if script == base:
        if button_id in overrides:
            overrides.pop(button_id)
            changed = True
    else:
        wanted = {
            "script": script,
            "base_script": base,
            "base_hash": script_hash(base),
        }
        if overrides.get(button_id) != wanted:
            overrides[button_id] = wanted
            changed = True

    if not changed:
        return False
    if overrides:
        data[OVERRIDES_KEY] = overrides
    else:
        data.pop(OVERRIDES_KEY, None)
    _save_user(data, user_path)
    _invalidate(button_id)
    return True


def clear_overrides(
    button_ids, user_path: Path | None = None
) -> int:
    """Remove the given overrides only. Returns how many were dropped."""
    wanted = set(button_ids)
    data = cfg.load_user_file(user_path)
    overrides = data.get(OVERRIDES_KEY)
    if not isinstance(overrides, dict):
        return 0
    removed = [key for key in overrides if key in wanted]
    for key in removed:
        overrides.pop(key)
    if not removed:
        return 0
    if overrides:
        data[OVERRIDES_KEY] = overrides
    else:
        data.pop(OVERRIDES_KEY, None)
    _save_user(data, user_path)
    for key in removed:
        _invalidate(key)
    return len(removed)


def restore_in_config(
    config: dict,
    button_ids,
    *,
    shipped_config: dict | None = None,
) -> None:
    """Put the shipped scripts back into a live config after a reset.

    Tool windows share one config dict, so mutating it here takes effect in
    already-open windows without a restart.
    """
    shipped = (shipped_config if shipped_config is not None
               else cfg.load_shipped_config())
    buttons = config.get("customcomponents", {}).get("buttons")
    shipped_buttons = shipped.get("customcomponents", {}).get("buttons", {})
    if not isinstance(buttons, dict):
        return
    for button_id in button_ids:
        target = buttons.get(button_id)
        source = shipped_buttons.get(button_id)
        if isinstance(target, dict) and isinstance(source, dict):
            target["script"] = source.get("script", "")


def resolve_override(entry: dict, shipped_script: str) -> tuple[str, str]:
    """(effective script, status) for one stored override."""
    base = normalize_script(entry.get("base_script", ""))
    user = normalize_script(entry.get("script", ""))
    shipped = normalize_script(shipped_script)
    if entry.get("base_hash") == script_hash(shipped):
        return user, STATUS_MODIFIED
    merged = merge_three_way(base, user, shipped)
    if merged is not None:
        return merged, STATUS_MODIFIED
    # A real conflict, or no Git to decide it: keep the user's script so the
    # workflow does not silently revert, and surface it for review.
    return user, STATUS_REVIEW


def merge_three_way(base: str, user: str, shipped: str) -> str | None:
    """Git's three-way merge of the user's edit onto the shipped script.

    Returns the merged text, or ``None`` on conflict or when Git cannot run.
    Conflict markers from Git are never returned.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="sapmerge_") as tmp:
            root = Path(tmp)
            user_path = root / "user.txt"
            base_path = root / "base.txt"
            shipped_path = root / "shipped.txt"
            for path, text in (
                (user_path, normalize_script(user)),
                (base_path, normalize_script(base)),
                (shipped_path, normalize_script(shipped)),
            ):
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
            result = subprocess.run(
                [git_executable(), "merge-file", "-p",
                 str(user_path), str(base_path), str(shipped_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=MERGE_TIMEOUT_S,
                creationflags=no_window_flags(),
            )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return normalize_script(result.stdout)


def apply_overrides(
    config: dict, shipped_config: dict, user_path: Path | None = None
) -> dict:
    """Apply stored overrides to a loaded config and drop the storage key."""
    config.pop(OVERRIDES_KEY, None)
    overrides = load_overrides(user_path)
    if not overrides:
        return config
    shipped_buttons = shipped_config.get("customcomponents", {}).get(
        "buttons", {})
    buttons = config.get("customcomponents", {}).get("buttons")
    if not isinstance(buttons, dict):
        return config
    for button_id, entry in overrides.items():
        shipped = shipped_buttons.get(button_id)
        target = buttons.get(button_id)
        if not isinstance(entry, dict) or not isinstance(shipped, dict):
            continue
        if not isinstance(target, dict):
            continue
        script, _status = _cached_resolve(
            entry, shipped.get("script", ""), button_id, user_path)
        target["script"] = script
    return config


def status_for(
    button_id: str,
    *,
    shipped_config: dict | None = None,
    user_path: Path | None = None,
) -> tuple[str, str] | None:
    """(status, effective script) for one button, or None when unmodified."""
    entry = load_overrides(user_path).get(button_id)
    if not isinstance(entry, dict):
        return None
    base = _shipped_script(button_id, shipped_config)
    if base is None:
        return None
    script, status = _cached_resolve(entry, base, button_id, user_path)
    return status, script


def _save_user(data: dict, user_path: Path | None) -> None:
    if data:
        cfg.save_user_file(data, user_path)
        return
    path = Path(user_path) if user_path is not None else cfg.USER_TOOLBAR_PATH
    try:
        path.unlink()
    except FileNotFoundError:
        pass
