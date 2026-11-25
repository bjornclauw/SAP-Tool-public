"""Managed offline runtime dependencies installed from verified wheels."""

from __future__ import annotations

import hashlib
import importlib.metadata
import re
import shutil
import sysconfig
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

LOCK_FILE = Path("vendor") / "requirements.lock"
WHEELS_DIR = Path("vendor") / "wheels"
_LOCK_LINE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)")
_HASH_LINE = re.compile(r"--hash=sha256:([0-9a-fA-F]{64})")

WheelInstaller = Callable[[Path, dict[tuple[str, str], str], Path], None]


class DependencyError(RuntimeError):
    """A required runtime package could not be installed."""


def _normalise(name: object) -> str:
    return re.sub(r"[-_.]+", "-", str(name)).casefold()


def _lock_path(root: Path) -> Path:
    return root / LOCK_FILE


def locked_wheels(root: Path | str) -> dict[tuple[str, str], str]:
    """Read pinned package versions and SHA-256 hashes from the lock."""
    path = _lock_path(Path(root))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    text = text.replace("\\\n", "")
    locked: dict[tuple[str, str], str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LOCK_LINE.match(line)
        hash_match = _HASH_LINE.search(line)
        if match and hash_match:
            locked[(_normalise(match.group(1)), match.group(2))] = (
                hash_match.group(1).lower()
            )
    return locked


def locked_dependencies(root: Path | str) -> dict[str, str]:
    """Read pinned package versions from the standard pip-style hash lock."""
    return {name: version for name, version in locked_wheels(root)}


def lock_hash(root: Path | str) -> str:
    """SHA-256 of the active lock, or an empty string when there is no lock."""
    try:
        return hashlib.sha256(_lock_path(Path(root)).read_bytes()).hexdigest()
    except OSError:
        return ""


def _installed_versions(target_dir: Path | None) -> dict[str, str]:
    versions: dict[str, str] = {}
    if target_dir is None:
        distributions = importlib.metadata.distributions()
    else:
        try:
            entries = list(target_dir.iterdir())
        except OSError:
            return versions
        distributions = []
        for entry in entries:
            if not entry.name.endswith((".dist-info", ".egg-info")):
                continue
            try:
                distributions.append(importlib.metadata.Distribution.at(entry))
            except (OSError, ValueError):
                continue
    for distribution in distributions:
        name = distribution.metadata.get("Name")
        if name:
            versions[_normalise(name)] = distribution.version
    return versions


def _missing_packages(
    root: Path | str, target_dir: Path | None,
) -> tuple[tuple[str, str], ...]:
    installed = _installed_versions(target_dir)
    return tuple(
        key for key in locked_dependencies(root).items()
        if installed.get(key[0]) != key[1]
    )


def missing_dependencies(
    root: Path | str, target_dir: Path | str | None = None,
) -> tuple[str, ...]:
    """Pinned packages missing or installed at the wrong version."""
    target = Path(target_dir) if target_dir is not None else None
    return tuple(f"{name}=={version}"
                 for name, version in _missing_packages(root, target))


def site_packages_dir() -> Path:
    """The running portable interpreter's pure-Python package directory."""
    return Path(sysconfig.get_path("purelib"))


def _wheel_key(filename: str) -> tuple[str, str] | None:
    parts = filename[:-4].split("-") if filename.endswith(".whl") else []
    if len(parts) < 5:
        return None
    return _normalise(parts[0]), parts[1]


def _safe_wheel_path(info: zipfile.ZipInfo) -> PurePosixPath | None:
    relative = PurePosixPath(info.filename)
    if (
        "\\" in info.filename
        or relative.is_absolute()
        or ".." in relative.parts
        or (relative.parts and ":" in relative.parts[0])
    ):
        raise DependencyError(f"unsafe path in wheel: {info.filename}")
    if info.is_dir():
        return None
    parts = relative.parts
    if parts and parts[0].endswith(".data"):
        if len(parts) < 3 or parts[1] not in {"purelib", "platlib"}:
            return None
        relative = PurePosixPath(*parts[2:])
    return relative if relative.parts else None


def _metadata_project(name: str) -> str | None:
    """The normalised project name of a ``.dist-info``/``.egg-info`` entry."""
    for suffix in (".dist-info", ".egg-info"):
        if name.endswith(suffix):
            return _normalise(name[:-len(suffix)].split("-", 1)[0])
    return None


def _clear_stale_staging(target_dir: Path) -> None:
    """Remove staging folders left behind when locked files could not be deleted."""
    for entry in target_dir.parent.glob(".wheel-*"):
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


def _rollback_install(
    target_dir: Path,
    moved_in: list[str],
    moved_out: dict[str, Path],
) -> None:
    """Best-effort restore after a failed swap."""
    for name in moved_in:
        path = target_dir / name
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists() or path.is_symlink():
                path.unlink()
        except OSError:
            pass
    for name, backup in moved_out.items():
        destination = target_dir / name
        if destination.exists() or destination.is_symlink():
            continue
        try:
            shutil.move(str(backup), str(destination))
        except OSError:
            pass


def _install_wheel(
    wheel: Path,
    key: tuple[str, str],
    target_dir: Path,
    staging_root: Path,
) -> None:
    """Replace one installed project with a verified wheel, keeping a rollback copy."""
    staging = staging_root / "new"
    backup_dir = staging_root / "old"
    staging.mkdir()
    backup_dir.mkdir()
    roots: set[str] = set()
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            relative = _safe_wheel_path(info)
            if relative is None:
                continue
            roots.add(relative.parts[0])
            destination = staging.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(destination, "wb") as output:
                shutil.copyfileobj(source, output)

    project = key[0]
    replace = set(roots)
    replace.update(
        entry.name for entry in target_dir.iterdir()
        if _metadata_project(entry.name) == project
    )

    moved_out: dict[str, Path] = {}
    moved_in: list[str] = []
    try:
        for name in sorted(replace):
            existing = target_dir / name
            if not (existing.exists() or existing.is_symlink()):
                continue
            shutil.move(str(existing), str(backup_dir / name))
            moved_out[name] = backup_dir / name
        for child in staging.iterdir():
            shutil.move(str(child), str(target_dir / child.name))
            moved_in.append(child.name)
    except OSError as exc:
        _rollback_install(target_dir, moved_in, moved_out)
        raise DependencyError(f"Could not install {wheel.name}: {exc}") from exc


def install_wheels(
    root: Path,
    locked: dict[tuple[str, str], str],
    target_dir: Path,
) -> None:
    """Install the given locked wheels directly, without invoking pip."""
    available: dict[tuple[str, str], Path] = {}
    for wheel in sorted((root / WHEELS_DIR).glob("*.whl")):
        key = _wheel_key(wheel.name)
        if key:
            available[key] = wheel

    selected: list[tuple[Path, tuple[str, str]]] = []
    for key, expected_hash in locked.items():
        wheel = available.get(key)
        if wheel is None:
            name, version = key
            raise DependencyError(f"Missing bundled wheel {name}=={version}")
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if digest != expected_hash:
            raise DependencyError(f"Wheel hash mismatch: {wheel.name}")
        selected.append((wheel, key))

    target_dir.mkdir(parents=True, exist_ok=True)
    _clear_stale_staging(target_dir)
    for wheel, key in selected:
        try:
            with tempfile.TemporaryDirectory(
                prefix=".wheel-", dir=target_dir.parent,
                ignore_cleanup_errors=True,
            ) as tmp:
                _install_wheel(wheel, key, target_dir, Path(tmp))
        except DependencyError:
            raise
        except (OSError, zipfile.BadZipFile) as exc:
            raise DependencyError(f"Could not install {wheel.name}: {exc}") from exc


def ensure_dependencies(
    root: Path | str,
    target_dir: Path | str | None = None,
    installer: WheelInstaller = install_wheels,
) -> tuple[str, ...]:
    """Install missing pinned packages from bundled wheels, offline.

    Returns the managed package names. A tree without a lock is a no-op so older
    versions remain revertible.
    """
    root = Path(root)
    locked = locked_wheels(root)
    if not locked:
        return ()
    target = Path(target_dir) if target_dir is not None else site_packages_dir()
    missing = _missing_packages(root, target)
    if not missing:
        return ()
    selected = {key: locked[key] for key in missing}
    display = ", ".join(f"{name}=={version}" for name, version in missing)
    try:
        installer(root, selected, target)
    except DependencyError as exc:
        raise DependencyError(
            f"Could not install required packages ({display}): {exc}"
        ) from exc
    remaining = _missing_packages(root, target)
    if remaining:
        raise DependencyError(
            "Dependencies are still missing after installation: "
            + ", ".join(f"{name}=={version}" for name, version in remaining)
        )
    return tuple(name for name, _version in locked)
