"""SAP list fields -- stable ids resolved from localised export headers.

SAP list exports carry display labels, not field names. Those labels differ by
SAP language, list layout and user settings. Code must never depend on a
column's position or on one language's spelling.

``config/sap_fields.json`` maps a stable field id (``order``, ``short_text``,
...) to every known header spelling. ``FieldRegistry.resolve`` turns one
export's headers into ``field -> column`` matches and reports missing,
duplicate and unknown columns.

The registry is data, not behaviour -- no UI imports live here.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent.parent / "config" / "sap_fields.json"

_RELEASED = "released"
_NOT_RELEASED = "not_released"
_UNKNOWN = "unknown"


class MissingColumnsError(ValueError):
    """Raised when a caller requires fields the export does not contain."""

    def __init__(self, missing: list[str], headers: list[str]):
        self.missing = list(missing)
        self.headers = list(headers)
        detail = ", ".join(self.missing)
        available = ", ".join(self.headers) if self.headers else "(none)"
        super().__init__(
            f"Export is missing required column(s): {detail}. "
            f"Available columns: {available}"
        )


def normalise_header(text: object) -> str:
    """Case-, accent- and punctuation-insensitive header identity.

    ``"Désign."``, ``"Designation"`` and ``"désignation"`` all reduce to a
    comparable key, so a SAP language switch does not break column matching.
    """
    value = unicodedata.normalize("NFKD", str(text))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold()
    value = re.sub(r"[^0-9a-z]+", " ", value)
    return " ".join(value.split())


@dataclass(frozen=True)
class FieldSpec:
    """One stable field id and every header spelling that maps to it."""

    name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class ColumnMatch:
    """A resolved field: where it lives in this export."""

    field: str
    header: str
    index: int


class ColumnMap:
    """The field-to-column result for one export.

    ``unknown_headers`` are valid columns the registry does not know; callers
    keep them visible instead of dropping user data. ``warnings`` describe
    duplicates, ambiguity and missing optional fields.
    """

    def __init__(
        self,
        headers: list[str],
        matches: dict[str, ColumnMatch],
        unknown_headers: list[str],
        warnings: list[str],
    ):
        self.headers = list(headers)
        self.matches = dict(matches)
        self.unknown_headers = list(unknown_headers)
        self.warnings = list(warnings)

    def __contains__(self, field: str) -> bool:
        return field in self.matches

    def header(self, field: str) -> str | None:
        match = self.matches.get(field)
        return match.header if match else None

    def index(self, field: str) -> int | None:
        match = self.matches.get(field)
        return match.index if match else None

    def missing(self, fields) -> list[str]:
        return [field for field in fields if field not in self.matches]

    def require(self, *fields: str) -> None:
        missing = self.missing(fields)
        if missing:
            raise MissingColumnsError(missing, self.headers)

    def value(self, field: str, row: list[str]) -> str:
        """The row's value for ``field``; empty when the column is absent."""
        index = self.index(field)
        if index is None or index >= len(row):
            return ""
        return str(row[index]).strip()


class FieldRegistry:
    """Alias table plus status-token vocabulary, loaded from JSON."""

    def __init__(self, data: dict):
        self._specs: dict[str, FieldSpec] = {}
        self._alias_to_field: dict[str, str] = {}
        self._ambiguous_aliases: dict[str, list[str]] = {}

        for name, spec in (data.get("fields") or {}).items():
            aliases = [str(alias) for alias in (spec.get("aliases") or [])]
            self._specs[name] = FieldSpec(name, tuple(aliases))
            for alias in aliases:
                key = normalise_header(alias)
                if not key:
                    continue
                existing = self._alias_to_field.get(key)
                if existing is not None and existing != name:
                    self._ambiguous_aliases.setdefault(key, [existing]).append(name)
                    continue
                self._alias_to_field[key] = name

        tokens = data.get("status_tokens") or {}
        self.released_tokens = {
            str(token).upper() for token in (tokens.get("released") or [])
        }
        self.not_released_tokens = {
            str(token).upper() for token in (tokens.get("not_released") or [])
        }

    @classmethod
    def load(cls, path: Path | str | None = None) -> FieldRegistry:
        source = Path(path) if path is not None else DEFAULT_PATH
        with open(source, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError(f"SAP field config must be an object: {source}")
        return cls(data)

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def aliases(self, field: str) -> tuple[str, ...]:
        spec = self._specs.get(field)
        return spec.aliases if spec else ()

    def field_for_header(self, header: object) -> str | None:
        """The stable field id for one header, if the alias table knows it."""
        key = normalise_header(header)
        if not key:
            return None
        if key in self._ambiguous_aliases:
            return None
        return self._alias_to_field.get(key)

    def canonical_name(self, name: object) -> str:
        """A config column name reduced to its stable id when known.

        Unknown names pass through unchanged, so user-added or future SAP
        columns keep their own identity.
        """
        text = str(name).strip()
        if text in self._specs:
            return text
        return self.field_for_header(text) or text

    def resolve(self, headers: list[str]) -> ColumnMap:
        """Map every known header to its field without using positions."""
        candidates: dict[str, list[ColumnMatch]] = {}
        warnings: list[str] = []
        unknown: list[str] = []

        for index, header in enumerate(headers):
            field = self.field_for_header(header)
            if field is None:
                if normalise_header(header):
                    unknown.append(header)
                continue
            candidates.setdefault(field, []).append(
                ColumnMatch(field, header, index)
            )

        matches: dict[str, ColumnMatch] = {}
        for field, found in candidates.items():
            matches[field] = found[0]
            if len(found) > 1:
                names = ", ".join(match.header for match in found)
                warnings.append(
                    f"multiple columns match '{field}' ({names}); "
                    f"using '{found[0].header}'"
                )

        if self._ambiguous_aliases:
            for key, fields in sorted(self._ambiguous_aliases.items()):
                warnings.append(
                    f"alias '{key}' is ambiguous between {', '.join(fields)}"
                )

        return ColumnMap(headers, matches, unknown, warnings)

    def classify_status(self, raw: object) -> str:
        """Map a system/user-status cell to released, not_released or unknown.

        SAP status abbreviations are translated per language. Only exact
        tokens from the configured vocabulary count; an unrecognised value is
        ``unknown`` instead of silently treating the order as not released.
        """
        tokens = {
            token
            for token in re.split(r"[\s,;/]+", str(raw or "").upper())
            if token
        }
        if tokens & self.released_tokens:
            return _RELEASED
        if tokens & self.not_released_tokens:
            return _NOT_RELEASED
        return _UNKNOWN


_default_registry: FieldRegistry | None = None
_default_registry_path: Path | None = None


def get_registry(path: Path | str | None = None) -> FieldRegistry:
    """Load the shipped registry once; ``path`` is for tests and tools."""
    global _default_registry, _default_registry_path
    source = Path(path) if path is not None else DEFAULT_PATH
    if (
        _default_registry is None
        or _default_registry_path != source
    ):
        _default_registry = FieldRegistry.load(source)
        _default_registry_path = source
    return _default_registry


def classify_status(raw: object, path: Path | str | None = None) -> str:
    return get_registry(path).classify_status(raw)


__all__ = [
    "ColumnMap",
    "ColumnMatch",
    "FieldRegistry",
    "FieldSpec",
    "MissingColumnsError",
    "classify_status",
    "get_registry",
    "normalise_header",
]
