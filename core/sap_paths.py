"""Resolve ``*`` wildcards in SAP GUI element paths.

SAP GUI's ``findById`` needs a literal id, but some ids are not stable: the
MEGUI subscreens (``SAPLMEGUI:0013``, ``:0016``, ``:0019``, ...) are layout
variants that SAP selects at runtime. A script can mark such a segment with
``*``; the backend expands it through the container's ``Children`` collection
and keeps only candidates whose remaining literal path actually resolves.

That last check matters when several variants are mounted at once: the item
overview (``0013``/``0016``) and the item detail (``0019``) can both exist, so
matching the wildcard alone is not enough to pick the right branch.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, Iterable


class SapPathError(LookupError):
    """A wildcard path had no match, or its literal remainder did not resolve."""


_MEGUI_ID = re.compile(r"SAPLMEGUI:\d{4}", re.IGNORECASE)


def wildcard_megui_ids(script_text: str) -> tuple[str, int]:
    """Replace every ``SAPLMEGUI:<4 digits>`` in a script with ``*``.

    Only the MEGUI family is rewritten: those are the screen variants SAP
    selects at runtime. Other screen families stay literal until they show
    the same drift. Returns the new text and the number of replacements.
    """
    return _MEGUI_ID.subn("SAPLMEGUI:*", script_text)


def _matches(name: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(name.lower(), pattern.lower())


def resolve_path(
    path: str,
    find_exact: Callable[[str], object],
    child_names: Callable[[str], Iterable[str]],
) -> str:
    """Return the first concrete path matching ``path``.

    ``find_exact`` must raise when a path does not exist; ``child_names``
    returns the immediate child names of a container path. Only segments
    containing ``*`` are expanded, and the literal remainder has to resolve,
    so a wildcard cannot silently select the wrong branch.

    The returned path is still resolved by the caller (the probe call is only
    used to prove the candidate exists).
    """
    if "*" not in path:
        find_exact(path)
        return path

    segments = path.split("/")
    index = next(i for i, segment in enumerate(segments) if "*" in segment)
    if index == 0:
        raise SapPathError(
            f"A wildcard cannot be the first segment of {path!r}")

    prefix = "/".join(segments[:index])
    pattern = segments[index]
    tail = segments[index + 1:]
    try:
        names = list(child_names(prefix))
    except Exception as exc:
        raise SapPathError(
            f"Cannot list the children of {prefix!r}: {exc}") from exc

    matches = [name for name in names if _matches(name, pattern)]
    available = ", ".join(sorted(names)) or "none"
    if not matches:
        raise SapPathError(
            f"No child of {prefix!r} matches {pattern!r}. "
            f"Available: {available}")

    last_error: Exception | None = None
    for name in matches:
        candidate = "/".join(segments[:index] + [name] + tail)
        try:
            return resolve_path(candidate, find_exact, child_names)
        except Exception as exc:  # try the next matching branch
            last_error = exc

    raise SapPathError(
        f"No match for {path!r} below {prefix!r}. "
        f"Available: {available}. Last error: {last_error}") from last_error
