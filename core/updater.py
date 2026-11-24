"""Check and apply updates with Git.

An install is a Git clone, so the update source is the clone's own upstream
(``origin``): a development clone pulls development commits, an installed clone
pulls whatever it was cloned from. This module only asks Git what is new and
resets the working tree to it, then installs missing managed packages from the
tracked wheel set before relaunch.

Per-machine state lives in ignored files (``data/``, ``private/``), so a reset
never touches it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core import dependencies

APP_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = "VERSION"
DEFAULT_UPSTREAM = "origin/master"
STATE_FILE = Path("data") / "update_state.json"
PENDING_FILE = Path("data") / "pending_dependencies.json"

_RELEASE_HEADING = re.compile(
    r"^##\s+(\d{4}\.\d{2}\.\d{2}(?:\.\d+)?)\s*[—–-]\s*(\d{4}-\d{2}-\d{2})\s*$",
    re.MULTILINE,
)

GitRunner = Callable[[Path, list[str]], str]
DependencyInstaller = Callable[[Path], tuple[str, ...]]


class UpdateError(Exception):
    """A failed check or update."""


class DependencyUpdateError(UpdateError):
    """The Git update succeeded but managed dependencies did not."""


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    subject: str
    date: str


@dataclass(frozen=True)
class ReleaseNote:
    version: str
    date: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class UpdateCheck:
    local: str = ""
    remote: str | None = None
    upstream: str | None = None
    newer: bool = False
    commits: tuple[CommitInfo, ...] = ()
    notes: tuple[ReleaseNote, ...] = ()
    error: str | None = None


def no_window_flags() -> int:
    """Keep console helpers (git, cmd) invisible when running under pythonw."""
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def git_executable(root: Path | str = APP_ROOT) -> str:
    """The bundled portable Git when present, otherwise Git on PATH."""
    bundled = Path(root) / "git" / "cmd" / "git.exe"
    return str(bundled) if bundled.is_file() else "git"


def run_git(root: Path | str, args: list[str], timeout: int = 120) -> str:
    try:
        result = subprocess.run(
            [git_executable(root), *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=no_window_flags(),
        )
    except FileNotFoundError as exc:
        raise UpdateError(
            "Git is not available. Add the portable git/ folder or install Git."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("Git timed out. Check the network connection.") from exc
    if result.returncode != 0:
        lines = [
            line
            for line in (result.stderr or result.stdout).splitlines()
            if line.strip()
        ]
        if lines:
            raise UpdateError(lines[-1])
        raise UpdateError(f"git {' '.join(args)} failed")
    return result.stdout


def is_git_install(root: Path | str = APP_ROOT) -> bool:
    return (Path(root) / ".git").exists()


def read_version(root: Path | str = APP_ROOT) -> str:
    path = Path(root) / VERSION_FILE
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise UpdateError(f"Cannot read {VERSION_FILE}: {exc}") from exc


def upstream_ref(root: Path | str = APP_ROOT, git: GitRunner = run_git) -> str:
    try:
        ref = git(
            Path(root),
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        ).strip()
    except UpdateError:
        return DEFAULT_UPSTREAM
    return ref or DEFAULT_UPSTREAM


def check_for_update(root: Path | str = APP_ROOT, git: GitRunner = run_git) -> UpdateCheck:
    root = Path(root)
    try:
        local = read_version(root)
        if not is_git_install(root):
            raise UpdateError(
                "This install has no Git metadata. Updates need a git clone, "
                "not a copied folder."
            )
        git(root, ["fetch", "--prune", "origin"])
        upstream = upstream_ref(root, git)
        count = git(root, ["rev-list", "--count", f"HEAD..{upstream}"])
        commits = _parse_log(
            git(root, ["log", "--format=%h%x1f%s%x1f%cs", f"HEAD..{upstream}"])
        )
        try:
            remote = git(root, ["show", f"{upstream}:{VERSION_FILE}"]).strip()
        except UpdateError:
            remote = None
        newer = int(count.strip() or "0") > 0
        notes = _notes_since(local, release_notes(root, upstream, git)) if newer else ()
        return UpdateCheck(
            local=local,
            remote=remote,
            upstream=upstream,
            newer=newer,
            commits=tuple(commits),
            notes=notes,
        )
    except (UpdateError, ValueError) as exc:
        return UpdateCheck(error=str(exc))


def release_notes(root: Path | str, ref: str,
                  git: GitRunner = run_git) -> list[ReleaseNote]:
    """User-facing release sections from the upstream README, newest first.

    The public README is the changelog; a clone without those sections simply
    returns no notes and the dialog falls back to commit subjects.
    """
    try:
        text = git(Path(root), ["show", f"{ref}:README.md"])
    except Exception:
        return []
    return parse_release_notes(text)


def parse_release_notes(text: str) -> list[ReleaseNote]:
    headings = list(_RELEASE_HEADING.finditer(text))
    notes: list[ReleaseNote] = []
    for index, match in enumerate(headings):
        end = (headings[index + 1].start()
               if index + 1 < len(headings) else len(text))
        lines: list[str] = []
        for raw in text[match.end():end].splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("- "):
                lines.append(line[2:].strip())
            elif lines:
                lines[-1] = f"{lines[-1]} {line}"
        notes.append(ReleaseNote(match.group(1), match.group(2), tuple(lines)))
    return notes


def _version_key(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return ()


def _notes_since(local: str, notes: list[ReleaseNote]) -> tuple[ReleaseNote, ...]:
    local_key = _version_key(local)
    if not local_key:
        return tuple(notes[:3])
    return tuple(note for note in notes
                 if _version_key(note.version) > local_key)


def _parse_log(output: str) -> list[CommitInfo]:
    commits: list[CommitInfo] = []
    for line in output.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            commits.append(CommitInfo(*parts))
    return commits


def _pending_path(root: Path) -> Path:
    return root / PENDING_FILE


def _write_pending(root: Path, error: str) -> None:
    try:
        path = _pending_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"error": error}, indent=2), encoding="utf-8")
    except OSError:
        pass


def _clear_pending(root: Path) -> None:
    try:
        _pending_path(root).unlink()
    except OSError:
        pass


def _ensure_with_pending(root: Path) -> tuple[str, ...]:
    try:
        installed = dependencies.ensure_dependencies(root)
    except dependencies.DependencyError as exc:
        _write_pending(root, str(exc))
        raise
    _clear_pending(root)
    return installed


def apply_update(
    root: Path | str = APP_ROOT,
    git: GitRunner = run_git,
    deps_fn: DependencyInstaller = _ensure_with_pending,
    on_dependencies: Callable[[], None] | None = None,
) -> str:
    """Fetch and hard-reset the tracked files to the upstream tip.

    Records the commit and version that were installed before the reset, so
    the update dialog can offer a one-step revert.
    """
    root = Path(root)
    if not is_git_install(root):
        raise UpdateError(
            "This install has no Git metadata. Updates need a git clone, "
            "not a copied folder."
        )
    git(root, ["fetch", "--prune", "origin"])
    upstream = upstream_ref(root, git)
    try:
        previous_sha = git(root, ["rev-parse", "HEAD"]).strip()
    except Exception:
        previous_sha = ""
    try:
        previous_version = read_version(root)
    except UpdateError:
        previous_version = ""
    git(root, ["reset", "--hard", upstream])
    version = read_version(root)
    state = {
        "from_sha": previous_sha,
        "from_version": previous_version,
        "to_version": version,
        "applied_at": datetime.now().isoformat(timespec="seconds"),
        "deps_lock_sha": dependencies.lock_hash(root),
        "dependencies": [],
    }
    if previous_sha:
        _write_state(root, state)
    if on_dependencies is not None:
        on_dependencies()
    try:
        installed = deps_fn(root)
    except dependencies.DependencyError as exc:
        raise DependencyUpdateError(
            f"Version {version} was installed, but dependencies failed: {exc}. "
            "Use Retry dependencies or restart the app."
        ) from exc
    state["dependencies"] = list(installed)
    state["deps_lock_sha"] = dependencies.lock_hash(root)
    if previous_sha:
        _write_state(root, state)
    return version


def previous_update(root: Path | str = APP_ROOT) -> dict | None:
    """The state recorded by the last successful update, or None."""
    try:
        data = json.loads(_state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("from_sha") or not data.get("from_version"):
        return None
    return data


def revert_update(
    root: Path | str = APP_ROOT,
    git: GitRunner = run_git,
    deps_fn: DependencyInstaller = _ensure_with_pending,
    on_dependencies: Callable[[], None] | None = None,
) -> str:
    """Hard-reset to the version recorded before the last update."""
    root = Path(root)
    state = previous_update(root)
    if state is None:
        raise UpdateError("No previous version is recorded.")
    sha = str(state["from_sha"])
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        raise UpdateError("The recorded previous version is invalid.")
    git(root, ["reset", "--hard", sha])
    version = read_version(root)
    if on_dependencies is not None:
        on_dependencies()
    try:
        deps_fn(root)
    except dependencies.DependencyError as exc:
        raise DependencyUpdateError(
            f"Reverted to {version}, but dependencies failed: {exc}. "
            "Use Retry dependencies or restart the app."
        ) from exc
    try:
        _state_path(root).unlink()
    except OSError:
        pass
    return version


def missing_dependencies(root: Path | str = APP_ROOT) -> tuple[str, ...]:
    """Managed packages missing or at the wrong version."""
    return dependencies.missing_dependencies(Path(root))


def retry_dependencies(root: Path | str = APP_ROOT) -> tuple[str, ...]:
    """Retry managed dependencies, recording a restart when files are locked."""
    return _ensure_with_pending(Path(root))


def finish_pending_dependencies(root: Path | str = APP_ROOT) -> str | None:
    """Finish a locked dependency install before the next app UI opens."""
    root = Path(root)
    state = previous_update(root)
    pending = _pending_path(root).exists()
    incomplete_state = bool(
        state and state.get("deps_lock_sha") and not state.get("dependencies")
    )
    if not pending and not incomplete_state:
        return None
    try:
        installed = _ensure_with_pending(root)
    except dependencies.DependencyError as exc:
        return str(exc)
    if state:
        state["dependencies"] = list(installed)
        state["deps_lock_sha"] = dependencies.lock_hash(root)
        _write_state(root, state)
    return None


def _state_path(root: Path | str) -> Path:
    return Path(root) / STATE_FILE


def _write_state(root: Path | str, data: dict) -> None:
    try:
        path = _state_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def relaunch(root: Path | str = APP_ROOT) -> None:
    """Start the app again, detached, after the working tree changed."""
    root = Path(root)
    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    if pythonw.is_file():
        executable = pythonw
    flags = no_window_flags() | int(getattr(subprocess, "DETACHED_PROCESS", 0))
    subprocess.Popen(
        [str(executable), "main.py"],
        cwd=str(root),
        close_fds=True,
        creationflags=flags,
    )
