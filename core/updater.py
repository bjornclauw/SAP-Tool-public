"""Check and apply updates with Git.

An install is a Git clone, so the update source is the clone's own upstream
(``origin``): a development clone pulls development commits, an installed clone
pulls whatever it was cloned from. This module only asks Git what is new and
resets the working tree to it -- nothing is downloaded or unpacked here.

Per-machine state lives in ignored files (``data/``, ``private/``), so a reset
never touches it.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = "VERSION"
DEFAULT_UPSTREAM = "origin/master"

GitRunner = Callable[[Path, list[str]], str]


class UpdateError(Exception):
    """A failed check or update."""


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    subject: str
    date: str


@dataclass(frozen=True)
class UpdateCheck:
    local: str = ""
    remote: str | None = None
    upstream: str | None = None
    newer: bool = False
    commits: tuple[CommitInfo, ...] = ()
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
        return UpdateCheck(
            local=local,
            remote=remote,
            upstream=upstream,
            newer=int(count.strip() or "0") > 0,
            commits=tuple(commits),
        )
    except (UpdateError, ValueError) as exc:
        return UpdateCheck(error=str(exc))


def _parse_log(output: str) -> list[CommitInfo]:
    commits: list[CommitInfo] = []
    for line in output.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            commits.append(CommitInfo(*parts))
    return commits


def apply_update(root: Path | str = APP_ROOT, git: GitRunner = run_git) -> str:
    """Fetch and hard-reset the tracked files to the upstream tip."""
    root = Path(root)
    if not is_git_install(root):
        raise UpdateError(
            "This install has no Git metadata. Updates need a git clone, "
            "not a copied folder."
        )
    git(root, ["fetch", "--prune", "origin"])
    upstream = upstream_ref(root, git)
    git(root, ["reset", "--hard", upstream])
    return read_version(root)


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
