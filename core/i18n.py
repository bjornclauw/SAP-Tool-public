"""Small JSON-backed translation layer for tool windows.

The main toolbar is intentionally not translated. Tool windows call ``t()``
for app-owned text; SAP exports and column headers are handled separately by
``core.sap_fields``.

Language files live in ``config/i18n/<code>.json`` and use flat keys:

    {"print_orders.title": "Print Orders"}

Missing keys fall back to English, then to the caller's default, then to the
key itself. A missing or broken language file never prevents the app from
starting.
"""

from __future__ import annotations

import json
import locale
import os
from pathlib import Path

SUPPORTED_LANGUAGES: dict[str, str] = {
    "nl": "Nederlands",
    "fr": "Français",
    "en": "English",
    "es": "Español",
}
DEFAULT_LANGUAGE = "nl"
AUTO_LANGUAGE = "auto"
DEFAULT_DIR = Path(__file__).parent.parent / "config" / "i18n"

_current_language: str | None = None
_catalogs: dict[str, dict[str, str]] = {}


def available_languages() -> dict[str, str]:
    return dict(SUPPORTED_LANGUAGES)


def language_label(code: str) -> str:
    return SUPPORTED_LANGUAGES.get(code, SUPPORTED_LANGUAGES[DEFAULT_LANGUAGE])


def _normalise_code(code: object) -> str | None:
    text = str(code or "").strip().lower().replace("_", "-")
    if not text:
        return None
    base = text.split("-", 1)[0]
    return base if base in SUPPORTED_LANGUAGES else None


def detect_system_language() -> str:
    """Best-effort Windows UI language, then environment, then default."""
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(85)
        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        if ctypes.windll.kernel32.GetLocaleInfoW(lcid, 0x59, buffer, 85):
            code = _normalise_code(buffer.value)
            if code:
                return code
    except Exception:
        pass

    for variable in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        raw = os.environ.get(variable)
        if raw:
            for part in raw.split(":"):
                code = _normalise_code(part)
                if code:
                    return code

    try:
        code = _normalise_code(locale.getlocale()[0])
        if code:
            return code
    except Exception:
        pass
    return DEFAULT_LANGUAGE


def resolve_language(setting: object = AUTO_LANGUAGE) -> str:
    if str(setting or "").strip().lower() == AUTO_LANGUAGE:
        return detect_system_language()
    return _normalise_code(setting) or DEFAULT_LANGUAGE


def _load_catalog(language: str, directory: Path | str | None = None) -> dict[str, str]:
    base = Path(directory) if directory is not None else DEFAULT_DIR
    path = base / f"{language}.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def catalog(language: str | None = None) -> dict[str, str]:
    code = _normalise_code(language) or get_language()
    if code not in _catalogs:
        _catalogs[code] = _load_catalog(code)
    return _catalogs[code]


def set_language(setting: object = AUTO_LANGUAGE) -> str:
    """Select the active language. Returns the concrete code used."""
    global _current_language
    _current_language = resolve_language(setting)
    catalog(_current_language)
    return _current_language


def get_language() -> str:
    global _current_language
    if _current_language is None:
        set_language(AUTO_LANGUAGE)
    assert _current_language is not None
    return _current_language


def t(key: str, default: str | None = None, **params) -> str:
    """Translate ``key`` in the active language with English fallback."""
    value = catalog().get(key)
    if value is None:
        value = catalog("en").get(key)
    if value is None:
        value = default if default is not None else key
    if params:
        try:
            return value.format(**params)
        except (KeyError, IndexError, ValueError):
            return value
    return value


def clear_cache() -> None:
    """Reset active language and loaded catalogs (tests and manual reload)."""
    global _current_language
    _current_language = None
    _catalogs.clear()


__all__ = [
    "AUTO_LANGUAGE",
    "DEFAULT_LANGUAGE",
    "SUPPORTED_LANGUAGES",
    "available_languages",
    "catalog",
    "clear_cache",
    "detect_system_language",
    "get_language",
    "language_label",
    "resolve_language",
    "set_language",
    "t",
]
