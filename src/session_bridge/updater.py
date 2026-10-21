"""Install a newer release: fetch and verify it, then hand over to that release's own installer.

This code always runs in the version being replaced, so a mistake here cannot be fixed by a later
release. It therefore stays small and follows the handoff contract in ``docs/CONTRACT.md``:
everything that depends on how a release is laid out is done by that release's ``install.ps1``.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from . import installation, net, releases, updates
from .errors import BridgeError
from .installation import CHECKOUT, MARKER, Installation
from .locks import ByteLock
from .releases import Release, Version

PREFIX = "session-bridge"
SUMS = "SHA256SUMS"
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_SUMS = 16 * 1024
MAX_EXTRACTED = 512 * 1024 * 1024
MAX_MEMBERS = 20_000
DOWNLOAD_TIMEOUT = 60
FETCH_TIMEOUT = 120
INSTALL_TIMEOUT = 15 * 60
STALE_STAGING = 24 * 60 * 60
LOCK_WAIT = 15  # Seconds an update waits for the start-up cleanup; another window's update takes longer.
_SUM_LINE = re.compile(r"([0-9a-f]{64}) [ *]?([A-Za-z0-9._-]{1,128})")

Progress = Callable[..., None]


def _quiet(step: str, **values) -> None:
    pass


def apply(offer: updates.Offer, progress: Progress = _quiet) -> Path:
    """Install ``offer``'s release, or the newer one GitHub now lists, and return the new command.

    On failure the installation in use is left as it was, and the error says why.
    """
    current = offer.installation
    if not current.updatable:
        raise BridgeError("update_not_installable", f"error.update_not_installable.{current.blocker or 'kind'}")
    try:
        with _exclusive(wait=LOCK_WAIT):
            # Another window may have updated since this one started: then the installation in use
            # is no longer this one, and its folders must not be touched.
            if not _receipt_points_to(current.project, current.runtime):
                raise BridgeError("update_unavailable", "error.update_unavailable.already_updated")
            release = updates.refresh()
            if release is None or release.version <= offer.current:
                raise BridgeError("update_unavailable", "error.update_unavailable.gone")
            if current.kind == CHECKOUT:
                return _from_checkout(current, release, progress)
            return _from_archive(current, release, progress)
    except (OSError, subprocess.SubprocessError):
        # A folder that cannot be written, a Git command that timed out: explained, never a crash.
        raise BridgeError("update_failed", "error.update_failed.system") from None


@contextmanager
def _exclusive(wait: float = 0.0):
    """Hold ``update.lock``; ``wait`` covers a short holder such as the cleanup run at start."""
    root = installation.root()
    root.mkdir(parents=True, exist_ok=True)
    lock = ByteLock(root / "update.lock")
    if lock.path.is_symlink():
        raise BridgeError("update_busy")
    deadline = time.monotonic() + wait
    while not lock.acquire():
        if time.monotonic() >= deadline:
            raise BridgeError("update_busy")
        time.sleep(0.1)
    try:
        yield
    finally:
        lock.release()


# -- From a release ZIP ---------------------------------------------------------------------------


def _from_archive(current: Installation, release: Release, progress: Progress) -> Path:
    app = installation.root() / "app"
    app.mkdir(parents=True, exist_ok=True)
    staging: Path | None = Path(tempfile.mkdtemp(prefix=".staging-", dir=app))
    target = app / str(release.version)
    try:
        _mark(staging)
        archive = _download(release, staging, progress)
        progress("verify")
        _extract(archive, staging)
        archive.unlink()
        _same_version(staging / PREFIX / "pyproject.toml", release.version)
        _discard(target)
        os.replace(staging, target)
        staging = None
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    project = target / PREFIX
    progress("install")
    try:
        _install(project, current, release.version)
    except BaseException:
        # The receipt, not the exit code, says which installation is in use.
        if not _receipt_points_to(project, None):
            shutil.rmtree(target, ignore_errors=True)
            raise
    return _installed(project, None, release.version)


def _download(release: Release, folder: Path, progress: Progress) -> Path:
    archive, sums = release.assets.get(release.portable), release.assets.get(SUMS)
    if archive is None or sums is None:
        raise BridgeError("update_integrity", "error.update_integrity.missing")
    try:
        reply = net.fetch(sums.url, hosts=releases.HOSTS, max_bytes=MAX_SUMS, timeout=DOWNLOAD_TIMEOUT)
    except net.NetError as error:
        raise BridgeError("update_unavailable", reason=error.reason, status=error.status) from None
    if sums.sha256 and hashlib.sha256(reply.body).hexdigest() != sums.sha256:
        raise BridgeError("update_integrity", "error.update_integrity.checksum")
    expected = _sums(reply.body).get(archive.name)
    if expected is None or (archive.sha256 is not None and archive.sha256 != expected):
        raise BridgeError("update_integrity", "error.update_integrity.checksum")
    progress("download", version=str(release.version), size=archive.size)
    try:
        return net.download(
            archive.url,
            folder,
            hosts=releases.HOSTS,
            max_bytes=MAX_ARCHIVE,
            size=archive.size,
            sha256=expected,
            timeout=DOWNLOAD_TIMEOUT,
        )
    except net.NetError as error:
        if error.reason in ("digest", "size"):
            raise BridgeError("update_integrity", "error.update_integrity.checksum") from None
        raise BridgeError("update_unavailable", reason=error.reason, status=error.status) from None


def _sums(body: bytes) -> dict[str, str]:
    try:
        lines = body.decode("ascii").splitlines()
    except UnicodeError:
        return {}
    found = {}
    for line in lines:
        match = _SUM_LINE.fullmatch(line.strip())
        if match:
            found[match.group(2)] = match.group(1)
    return found


def _extract(archive: Path, folder: Path) -> None:
    """Unpack only regular files and folders under ``session-bridge/``, within fixed size limits."""
    unsafe = BridgeError("update_integrity", "error.update_integrity.archive")
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > MAX_MEMBERS:
                raise unsafe
            total = 0
            for member in members:
                name = member.filename
                parts = PurePosixPath(name).parts
                if (
                    "\\" in name
                    or ":" in name
                    or name.startswith("/")
                    or not parts
                    or parts[0] != PREFIX
                    or any(part in ("", ".", "..") for part in parts)
                    or stat.S_ISLNK(member.external_attr >> 16)
                ):
                    raise unsafe
                total += member.file_size
                if total > MAX_EXTRACTED:
                    raise unsafe
                path = folder.joinpath(*parts)
                if member.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, path.open("xb") as output:
                    written = 0
                    while block := source.read(1024 * 1024):
                        written += len(block)
                        if written > member.file_size:
                            raise unsafe
                        output.write(block)
    except BridgeError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, EOFError):
        raise unsafe from None


def _same_version(pyproject: Path | bytes, version: Version) -> None:
    try:
        raw = pyproject if isinstance(pyproject, bytes) else pyproject.read_bytes()
        declared = tomllib.loads(raw.decode("utf-8"))["project"]["version"]
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        declared = None
    if declared != str(version):
        raise BridgeError("update_integrity", "error.update_integrity.version", version=str(version))


# -- From a Git checkout --------------------------------------------------------------------------


def _from_checkout(current: Installation, release: Release, progress: Progress) -> Path:
    project, tag = current.project, release.version.tag
    progress("git", version=str(release.version))
    blocker = installation.checkout_blocker(project)
    if blocker is not None:
        raise BridgeError("update_not_installable", f"error.update_not_installable.{blocker}")
    previous = _git(project, "rev-parse", "HEAD")
    fetched = installation.git(project, "fetch", "--quiet", "--no-tags", "origin", f"+refs/tags/{tag}:refs/tags/{tag}", timeout=FETCH_TIMEOUT)
    if fetched.returncode != 0:
        raise BridgeError("update_unavailable", "error.update_unavailable.git_fetch", tag=tag)
    if installation.git(project, "merge-base", "--is-ancestor", "HEAD", tag).returncode != 0:
        raise BridgeError("update_not_installable", "error.update_not_installable.diverged")
    shown = installation.git(project, "show", f"{tag}:pyproject.toml")
    if shown.returncode != 0:
        raise BridgeError("update_integrity", "error.update_integrity.version", version=str(release.version))
    _same_version(shown.stdout.encode("utf-8"), release.version)
    if installation.git(project, "merge", "--ff-only", "--quiet", tag).returncode != 0:
        raise BridgeError("update_failed", "error.update_failed.git", tag=tag)
    # The open window still runs from the current runtime, so the new one goes to a folder of its own.
    runtime = installation.root() / "runtime" / str(release.version)
    progress("install")
    try:
        _discard(runtime)
        runtime.mkdir(parents=True)
        _mark(runtime)
        _install(project, current, release.version, runtime)
    except BaseException:
        # Moved back only while the previous installation is still the one in use.
        if not _receipt_points_to(project, runtime):
            _undo_merge(project, tag, previous)
            shutil.rmtree(runtime, ignore_errors=True)
            raise
    return _installed(project, runtime, release.version)


def _git(project: Path, *args: str) -> str:
    result = installation.git(project, *args)
    if result.returncode != 0:
        raise BridgeError("update_not_installable", "error.update_not_installable.git_failed")
    return result.stdout.strip()


def _undo_merge(project: Path, tag: str, previous: str) -> None:
    """Move the checkout back, only if it still sits exactly where the update left it."""
    try:
        head = installation.git(project, "rev-parse", "HEAD").stdout.strip()
        moved = installation.git(project, "rev-parse", f"{tag}^{{commit}}").stdout.strip()
        if head and head == moved and head != previous:
            installation.git(project, "reset", "--keep", previous)
    except (OSError, subprocess.SubprocessError):
        pass


# -- Handing over to the release's installer ------------------------------------------------------


def log_path(version: Version) -> Path:
    from .profiles import data_home

    return data_home() / "logs" / f"update-{version}.log"


def _powershell() -> str:
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0"
    bundled = system / "powershell.exe"
    found = str(bundled) if bundled.is_file() else shutil.which("powershell") or shutil.which("pwsh")
    if not found:
        raise BridgeError("update_failed", "error.update_failed.powershell")
    return found


def _install(project: Path, current: Installation, version: Version, runtime: Path | None = None) -> None:
    script = project / "scripts" / "install.ps1"
    if not script.is_file():
        raise BridgeError("update_integrity", "error.update_integrity.archive")
    command = [
        _powershell(),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-Python",
        str(current.python),
        "-CommandDirectory",
        str(current.command.parent),
    ]
    if runtime is not None:
        command += ["-Runtime", str(runtime)]
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME")}
    log = log_path(version)
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                cwd=str(project),
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            code = _wait(process)
    except subprocess.TimeoutExpired:
        _restore_command(current)
        raise BridgeError("update_failed", "error.update_failed.timeout", log=str(log)) from None
    except OSError:
        raise BridgeError("update_failed", log=str(log)) from None
    if code != 0:
        _restore_command(current)
        raise BridgeError("update_failed", log=str(log))


def _wait(process: subprocess.Popen) -> int:
    """Wait for the installer; Ctrl+C does not abandon it halfway through replacing the command."""
    deadline = time.monotonic() + INSTALL_TIMEOUT
    while True:
        try:
            return process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except KeyboardInterrupt:
            continue
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            raise


def _kill_tree(process: subprocess.Popen) -> None:
    """Stop the installer and what it started (pip, Python), so nothing writes after this returns."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            capture_output=True,
            timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        process.kill()
    try:
        process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        pass


def _restore_command(current: Installation) -> None:
    """An installer stopped while replacing the command may leave it renamed aside: put it back.

    While the receipt still names the previous installation, the command must be the one the receipt
    records; a missing command, or a new one copied before the receipt was written, is replaced by
    the renamed previous command.
    """
    command = current.command
    if command is None or not _receipt_points_to(current.project, current.runtime):
        return
    aside = sorted(command.parent.glob(command.name + ".old-*"))
    if not aside:
        return
    recorded = installation.receipt().get("sha256")
    if command.exists() and (not isinstance(recorded, str) or _sha256(command) == recorded.lower()):
        return
    try:
        os.replace(aside[-1], command)
    except OSError:
        pass


def _sha256(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return None


def _receipt_points_to(project: Path | None, runtime: Path | None) -> bool:
    """Whether the receipt names this installation: its runtime when given, otherwise its project."""
    record = installation.receipt()
    recorded = record.get("project")
    if not isinstance(recorded, str) or project is None:
        return False
    if runtime is None:
        return installation._same(Path(recorded), project)
    # Receipts written before 0.8.0 have no runtime: it was always the project's .venv.
    named = record.get("runtime")
    return installation._same(Path(named) if isinstance(named, str) else Path(recorded) / ".venv", runtime)


def _installed(project: Path, runtime: Path | None, version: Version) -> Path:
    """The installer succeeded only if the receipt now names the new folder and a command exists."""
    command = installation.receipt().get("command")
    if not _receipt_points_to(project, runtime) or not isinstance(command, str) or not Path(command).is_file():
        raise BridgeError("update_failed", log=str(log_path(version)))
    return Path(command)


# -- Folders the updater owns ---------------------------------------------------------------------


def _mark(folder: Path) -> None:
    (folder / MARKER).write_text("Created by the Session Bridge updater; safe to delete when unused.\n")


def _owned(folder: Path) -> bool:
    junction = getattr(folder, "is_junction", lambda: False)
    return folder.is_dir() and not folder.is_symlink() and not junction() and (folder / MARKER).is_file()


def _discard(folder: Path) -> None:
    """Remove an earlier attempt at the same version; a folder the updater did not create is never touched."""
    if not os.path.lexists(folder):
        return
    if not _owned(folder):
        raise BridgeError("update_failed", "error.update_failed.folder", folder=str(folder))
    record = installation.receipt()
    if any(isinstance(path, str) and _inside(Path(path), folder) for path in (record.get("project"), record.get("runtime"))):
        raise BridgeError("update_unavailable", "error.update_unavailable.already_updated")
    shutil.rmtree(folder)


def prune(current: Installation, *, now: float | None = None) -> None:
    """Delete updater folders no longer needed: all but the one in use and the one before it.

    Run by the new version, never by the one being replaced; a folder still in use is skipped.
    """
    if current.kind not in (installation.MANAGED, CHECKOUT):
        return
    try:
        with _exclusive():
            _prune(current, time.time() if now is None else now)
    except (BridgeError, OSError):
        pass  # Another window is updating, or the folder is unavailable: try again next time.


def _prune(current: Installation, now: float) -> None:
    if current.command is not None and current.command.is_file():
        # install.ps1 renames a command that was running when it was replaced; nothing runs it now.
        # Never while the command itself is missing: the renamed one is then the only copy.
        for leftover in current.command.parent.glob(current.command.name + ".old-*"):
            try:
                leftover.unlink()
            except OSError:
                pass
    in_use = [path for path in (current.project, current.runtime, Path(sys.prefix)) if path is not None]
    for base in (installation.root() / "app", installation.root() / "runtime"):
        try:
            children = [child for child in base.iterdir() if _owned(child)]
        except OSError:
            continue
        mine = next((child for child in children if any(_inside(path, child) for path in in_use)), None)
        versions = sorted(
            (version, child) for child in children if (version := Version.parse(child.name)) is not None
        )
        mine_version = Version.parse(mine.name) if mine is not None else None
        older = [child for version, child in versions if mine_version is not None and version < mine_version]
        keep = {mine, older[-1] if older else None}
        for child in children:
            if child in keep:
                continue
            if child.name.startswith(".staging-"):
                try:
                    if now - child.stat().st_mtime < STALE_STAGING:
                        continue
                except OSError:
                    continue
            elif not child.name.startswith(".trash-") and (Version.parse(child.name) is None or mine is None):
                continue
            _remove_unused(child)


def _remove_unused(folder: Path) -> None:
    """Delete ``folder`` only if no program runs from it.

    Windows refuses to rename a folder while a program inside it runs, so the folder is first moved
    out of the way; if that fails, another window still uses it and it is kept for a later start.
    """
    if folder.name.startswith(".trash-"):
        shutil.rmtree(folder, ignore_errors=True)
        return
    trash = folder.with_name(f".trash-{folder.name}-{os.urandom(4).hex()}")
    try:
        folder.rename(trash)
    except OSError:
        return
    shutil.rmtree(trash, ignore_errors=True)


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (OSError, ValueError):
        return False
