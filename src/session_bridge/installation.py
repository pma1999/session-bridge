"""How the running Session Bridge was installed, and whether it can update itself.

``scripts/install.ps1`` records each installation in a receipt under ``%LOCALAPPDATA%\\SessionBridge``:
the project folder it installed from, the runtime (virtual environment) it created and the command
it placed on the PATH. Only a run from exactly that runtime is treated as that installation.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import jsonfile
from .releases import REPOSITORY

MANAGED = "managed"  # Installed from a release ZIP: updated from the next release ZIP.
CHECKOUT = "checkout"  # Installed from a Git clone of this repository: updated with Git.
OTHER = "other"  # Anything else (a wheel, a development run, Linux): only told about new versions.

MARKER = ".sessionbridge-managed"  # In every folder the updater creates, and only those.
_REMOTES = {
    f"https://github.com/{REPOSITORY}",
    f"https://github.com/{REPOSITORY}.git",
    f"git@github.com:{REPOSITORY}.git",
    f"ssh://git@github.com/{REPOSITORY}.git",
}
GIT_TIMEOUT = 20


@dataclass(frozen=True)
class Installation:
    kind: str
    project: Path | None = None
    runtime: Path | None = None
    command: Path | None = None
    python: Path | None = None
    blocker: str | None = None  # Why a Git checkout cannot be updated now, as a short code.

    @property
    def updatable(self) -> bool:
        return self.kind in (MANAGED, CHECKOUT) and self.blocker is None


def root() -> Path:
    """Where ``install.ps1`` keeps its receipt; ``SESSIONBRIDGE_HOME`` does not move it."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "SessionBridge"


def receipt_path() -> Path:
    return root() / "installation.json"


def receipt() -> dict:
    return jsonfile.read(receipt_path())


def _same(first: Path, second: Path) -> bool:
    try:
        return os.path.normcase(first.resolve()) == os.path.normcase(second.resolve())
    except OSError:
        return False


def base_python(runtime: Path) -> Path | None:
    """The interpreter a virtual environment was created from, as its ``pyvenv.cfg`` records it."""
    values = {}
    try:
        for line in (runtime / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key.strip().lower()] = value.strip()
    except (OSError, UnicodeError):
        return None
    candidates = []
    if values.get("executable"):
        candidates.append(Path(values["executable"]))
    if values.get("home"):
        candidates.append(Path(values["home"]) / ("python.exe" if os.name == "nt" else "python3"))
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def detect(*, prefix: str | None = None, platform: str | None = None) -> Installation:
    """Describe the running installation. Reads files and, for a checkout, runs read-only Git."""
    if (platform or sys.platform) != "win32":
        return Installation(OTHER)
    record = receipt()
    project, command = record.get("project"), record.get("command")
    if not isinstance(project, str) or not isinstance(command, str):
        return Installation(OTHER)
    project_path, command_path = Path(project), Path(command)
    runtime = record.get("runtime")
    # Receipts written before 0.8.0 have no runtime: it was always the project's .venv.
    runtime_path = Path(runtime) if isinstance(runtime, str) else project_path / ".venv"
    if not _same(Path(prefix or sys.prefix), runtime_path) or not command_path.is_file():
        return Installation(OTHER)
    python = base_python(runtime_path)
    if python is None:
        return Installation(OTHER)
    if (project_path / ".git").exists():
        return Installation(CHECKOUT, project_path, runtime_path, command_path, python, checkout_blocker(project_path))
    return Installation(MANAGED, project_path, runtime_path, command_path, python)


def git(project: Path, *args: str, timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    """Run Git in ``project`` without prompting; arguments are a list, never a shell string."""
    executable = shutil.which("git")
    if executable is None:
        raise FileNotFoundError("git")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
    return subprocess.run(
        [executable, "-C", str(project), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _output(project: Path, *args: str) -> str | None:
    result = git(project, *args)
    return result.stdout.strip() if result.returncode == 0 else None


def checkout_blocker(project: Path) -> str | None:
    """Why this Git checkout cannot fast-forward to a release by itself, or None when it can.

    Read-only: it neither fetches nor changes anything. The final check, against the release tag,
    happens just before updating.
    """
    try:
        if _output(project, "rev-parse", "--abbrev-ref", "HEAD") != "main":
            return "branch"
        if _output(project, "rev-parse", "--abbrev-ref", "main@{upstream}") != "origin/main":
            return "branch"
        remote = _output(project, "remote", "get-url", "origin")
        if remote is None or remote.rstrip("/") not in _REMOTES:
            return "remote"
        status = git(project, "status", "--porcelain", "--untracked-files=no")
        if status.returncode != 0 or status.stdout.strip():
            return "dirty"
        ahead = _output(project, "rev-list", "--count", "origin/main..HEAD")
        if ahead is None or not re.fullmatch(r"\d+", ahead) or int(ahead):
            return "diverged"
    except FileNotFoundError:
        return "git_missing"
    except (OSError, subprocess.SubprocessError):
        return "git_failed"
    return None
