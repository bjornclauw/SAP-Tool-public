"""Managed offline runtime dependencies installed by the internal updater."""

from __future__ import annotations

import hashlib
import importlib.metadata
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

LOCK_FILE = Path("vendor") / "requirements.lock"
WHEELS_DIR = Path("vendor") / "wheels"
_LOCK_LINE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)")

PipRunner = Callable[[Path, list[str]], str]


class DependencyError(RuntimeError):
    """A required runtime package could not be installed."""


def _lock_path(root: Path) -> Path:
    return root / LOCK_FILE


def locked_dependencies(root: Path | str) -> dict[str, str]:
    """Read pinned package versions from the standard pip hash lock."""
    path = _lock_path(Path(root))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    text = text.replace("\\\n", "")
    locked: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "--")):
            continue
        match = _LOCK_LINE.match(line)
        if match:
            name = re.sub(r"[-_.]+", "-", match.group(1)).casefold()
            locked[name] = match.group(2)
    return locked


def lock_hash(root: Path | str) -> str:
    """SHA-256 of the active lock, or an empty string when there is no lock."""
    try:
        return hashlib.sha256(_lock_path(Path(root)).read_bytes()).hexdigest()
    except OSError:
        return ""


def missing_dependencies(root: Path | str) -> tuple[str, ...]:
    """Pinned packages missing or installed at the wrong version."""
    missing = []
    for name, version in locked_dependencies(root).items():
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            installed = ""
        if installed != version:
            missing.append(f"{name}=={version}")
    return tuple(missing)


def _python_executable() -> str:
    executable = Path(sys.executable)
    console = executable.with_name("python.exe")
    return str(console) if console.is_file() else str(executable)


def _invoke_python(root: Path, args: list[str], timeout: int = 180):
    try:
        return subprocess.run(
            [_python_executable(), "-m", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except FileNotFoundError as exc:
        raise DependencyError("The portable Python module runner is not available.") from exc
    except subprocess.TimeoutExpired as exc:
        raise DependencyError("Dependency installation timed out.") from exc


def _failure_detail(result, fallback: str) -> str:
    lines = [
        line.strip()
        for line in (result.stderr or result.stdout).splitlines()
        if line.strip()
    ]
    return lines[-1] if lines else fallback


def run_pip(root: Path, args: list[str]) -> str:
    """Run pip, repairing a broken portable pip with ensurepip when needed."""
    result = _invoke_python(root, ["pip", *args])
    detail = _failure_detail(result, "pip failed")
    if result.returncode != 0 and (
        "pip._internal" in detail or "No module named 'pip'" in detail
    ):
        repair = _invoke_python(root, ["ensurepip", "--upgrade"], timeout=120)
        if repair.returncode != 0:
            raise DependencyError(
                "pip is broken and ensurepip could not repair it: "
                + _failure_detail(repair, "ensurepip failed")
            )
        result = _invoke_python(root, ["pip", *args])
        detail = _failure_detail(result, "pip failed")
    if result.returncode != 0:
        raise DependencyError(detail)
    return result.stdout


def ensure_dependencies(root: Path | str, pip: PipRunner = run_pip) -> tuple[str, ...]:
    """Install missing pinned packages from bundled wheels, offline.

    Returns the managed package names. A tree without a lock is a no-op so older
    versions remain revertible.
    """
    root = Path(root)
    locked = locked_dependencies(root)
    if not locked:
        return ()
    missing = missing_dependencies(root)
    if not missing:
        return ()
    wheels = root / WHEELS_DIR
    if not wheels.is_dir():
        raise DependencyError(
            "Bundled dependency wheels are missing. Reinstall or update the app."
        )
    args = [
        "install",
        "--disable-pip-version-check",
        "--no-index",
        "--require-hashes",
        "--find-links", str(wheels),
        "-r", str(_lock_path(root)),
    ]
    try:
        pip(root, args)
    except DependencyError as exc:
        raise DependencyError(
            f"Could not install required packages ({', '.join(missing)}): {exc}"
        ) from exc
    except Exception as exc:
        raise DependencyError(
            f"Could not install required packages ({', '.join(missing)}): {exc}"
        ) from exc
    remaining = missing_dependencies(root)
    if remaining:
        raise DependencyError(
            "Dependencies are still missing after installation: "
            + ", ".join(remaining)
        )
    return tuple(locked)
