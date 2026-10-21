"""Installing an update: only verified, well-formed releases reach the installer, and failures change nothing."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from update_fakes import FakeWeb, portable_zip, publish

from session_bridge import installation, net, updater, updates
from session_bridge.errors import BridgeError
from session_bridge.installation import CHECKOUT, MANAGED, MARKER, Installation
from session_bridge.releases import Version

NEXT = "99.0.0"


@pytest.fixture
def web(monkeypatch):
    fake = FakeWeb()
    monkeypatch.setattr(net, "_open", fake)
    return fake


@pytest.fixture
def root(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    return tmp_path / "LocalAppData" / "SessionBridge"


@pytest.fixture
def installer(monkeypatch, root, tmp_path):
    """Stands in for the release's install.ps1: records the call and writes the receipt it would write."""
    calls = []
    command = tmp_path / "bin" / "sessionbridge.exe"

    def install(project, current, version, runtime=None):
        calls.append({"project": project, "version": version, "runtime": runtime})
        if installer.fail:
            raise BridgeError("update_failed", log="install.log")
        command.parent.mkdir(parents=True, exist_ok=True)
        command.write_bytes(b"new")
        record = {"project": str(project), "command": str(command), "sha256": "x"}
        if runtime is not None:
            record["runtime"] = str(runtime)
        root.mkdir(parents=True, exist_ok=True)
        (root / "installation.json").write_text(json.dumps(record), encoding="utf-8")

    installer.fail = False
    installer.calls = calls
    installer.command = command
    monkeypatch.setattr(updater, "_install", install)
    return installer


def _in_use(place: Installation) -> Installation:
    """Write the receipt install.ps1 writes, so ``place`` is the installation in use."""
    record = {"project": str(place.project), "command": str(place.command), "sha256": "x"}
    if place.kind == CHECKOUT:
        record["runtime"] = str(place.runtime)
    installation.root().mkdir(parents=True, exist_ok=True)
    installation.receipt_path().write_text(json.dumps(record), encoding="utf-8")
    return place


def _managed(tmp_path) -> Installation:
    command = tmp_path / "bin" / "sessionbridge.exe"
    return _in_use(Installation(MANAGED, tmp_path / "old", tmp_path / "old" / ".venv", command, Path(sys.executable)))


def _offer(place, version=NEXT) -> updates.Offer:
    return updates.Offer(updates.current(), updates.refresh(), place)


# -- From a release ZIP ---------------------------------------------------------------------------


def test_a_verified_release_is_unpacked_in_its_own_folder_and_handed_to_its_installer(web, root, installer, tmp_path):
    publish(web, NEXT)
    steps = []
    command = updater.apply(_offer(_managed(tmp_path)), lambda step, **values: steps.append(step))
    target = root / "app" / NEXT
    assert command == installer.command
    assert installer.calls == [{"project": target / "session-bridge", "version": Version.parse(NEXT), "runtime": None}]
    assert (target / MARKER).is_file()
    assert (target / "session-bridge" / "scripts" / "install.ps1").is_file()
    assert [child.name for child in (root / "app").iterdir()] == [NEXT]  # No staging left behind.
    assert steps == ["download", "verify", "install"]
    # The download went to GitHub's file host after the redirect, and nowhere else.
    assert all(request.full_url.startswith("https://") for request in web.requests)


@pytest.mark.parametrize("tamper", ["archive", "digest"])
def test_a_download_that_does_not_match_its_checksums_installs_nothing(web, root, installer, tmp_path, tamper):
    if tamper == "archive":
        publish(web, NEXT, sums_for=b"other bytes")
    else:
        publish(web, NEXT, digest="0" * 64)
    with pytest.raises(BridgeError) as raised:
        updater.apply(_offer(_managed(tmp_path)))
    assert raised.value.code == "update_integrity"
    assert installer.calls == []
    assert list((root / "app").iterdir()) == []


@pytest.mark.parametrize(
    "members",
    [
        {"session-bridge/../evil.txt": b"x"},
        {"../evil.txt": b"x"},
        {"/absolute.txt": b"x"},
        {"other-folder/file.txt": b"x"},
        {"session-bridge\\..\\evil.txt": b"x"},
        {"session-bridge/C:/evil.txt": b"x"},
    ],
)
def test_an_archive_with_unexpected_paths_installs_nothing(web, root, installer, tmp_path, members):
    publish(web, NEXT, portable_zip(NEXT, members=members))
    with pytest.raises(BridgeError) as raised:
        updater.apply(_offer(_managed(tmp_path)))
    assert raised.value.key == "error.update_integrity.archive"
    assert installer.calls == [] and list((root / "app").iterdir()) == []
    assert not (tmp_path / "evil.txt").exists() and not (root / "evil.txt").exists()


def test_an_archive_with_a_link_installs_nothing(web, root, installer, tmp_path):
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        link = zipfile.ZipInfo("session-bridge/link")
        link.external_attr = (0o120777 << 16)
        archive.writestr(link, "/etc/passwd")
    publish(web, NEXT, buffer.getvalue())
    with pytest.raises(BridgeError) as raised:
        updater.apply(_offer(_managed(tmp_path)))
    assert raised.value.key == "error.update_integrity.archive"


def test_a_package_of_another_version_installs_nothing(web, root, installer, tmp_path):
    publish(web, NEXT, portable_zip(NEXT, declared="1.0.0"))
    with pytest.raises(BridgeError) as raised:
        updater.apply(_offer(_managed(tmp_path)))
    assert raised.value.key == "error.update_integrity.version"
    assert installer.calls == []


def test_a_failed_installer_leaves_the_installation_in_use_untouched(web, root, installer, tmp_path):
    publish(web, NEXT)
    place = _managed(tmp_path)
    before = (root / "installation.json").read_text(encoding="utf-8")
    installer.fail = True
    with pytest.raises(BridgeError) as raised:
        updater.apply(_offer(place))
    assert raised.value.code == "update_failed"
    assert (root / "installation.json").read_text(encoding="utf-8") == before
    assert not (root / "app" / NEXT).exists()


def test_a_release_that_disappeared_meanwhile_is_not_installed(web, root, installer, tmp_path):
    publish(web, NEXT)
    offer = _offer(_managed(tmp_path))
    publish(web, "0.0.1", etag='"other"')
    with pytest.raises(BridgeError) as raised:
        updater.apply(offer)
    assert raised.value.key == "error.update_unavailable.gone"


def test_only_one_window_updates_at_a_time(web, root, installer, tmp_path, monkeypatch):
    from session_bridge.locks import ByteLock

    monkeypatch.setattr(updater, "LOCK_WAIT", 0.2)
    publish(web, NEXT)
    root.mkdir(parents=True)
    held = ByteLock(root / "update.lock")
    assert held.acquire()
    try:
        with pytest.raises(BridgeError) as raised:
            updater.apply(_offer(_managed(tmp_path)))
        assert raised.value.code == "update_busy"
    finally:
        held.release()


def test_a_folder_the_updater_did_not_create_is_never_replaced(web, root, installer, tmp_path):
    publish(web, NEXT)
    foreign = root / "app" / NEXT
    foreign.mkdir(parents=True)
    (foreign / "mine.txt").write_text("user data", encoding="utf-8")
    with pytest.raises(BridgeError) as raised:
        updater.apply(_offer(_managed(tmp_path)))
    assert raised.value.key == "error.update_failed.folder"
    assert (foreign / "mine.txt").read_text(encoding="utf-8") == "user data"


def test_an_installation_that_cannot_update_is_refused_before_any_download(web, root, installer, tmp_path):
    place = Installation(installation.OTHER)
    publish(web, NEXT)
    offer = _offer(place)
    web.requests.clear()
    with pytest.raises(BridgeError) as raised:
        updater.apply(offer)
    assert raised.value.key == "error.update_not_installable.kind"
    assert web.requests == []


# -- Cleaning up ----------------------------------------------------------------------------------


def test_cleanup_keeps_the_version_in_use_and_the_one_before(root, tmp_path):
    app = root / "app"
    for name in ("97.0.0", "98.0.0", "99.0.0", "100.0.0"):
        (app / name / "session-bridge").mkdir(parents=True)
        updater._mark(app / name)
    (app / "unmarked-1.0.0").mkdir()
    (app / "1.0.0").mkdir()  # Not created by the updater: no marker.
    command = tmp_path / "bin" / "sessionbridge.exe"
    command.parent.mkdir()
    command.write_bytes(b"new")
    (command.parent / "sessionbridge.exe.old-20260101").write_bytes(b"old")
    (command.parent / "other.exe").write_bytes(b"other")
    project = app / "99.0.0" / "session-bridge"
    updater.prune(Installation(MANAGED, project, project / ".venv", command))
    assert sorted(child.name for child in app.iterdir()) == ["1.0.0", "98.0.0", "99.0.0", "unmarked-1.0.0"]
    assert sorted(child.name for child in command.parent.iterdir()) == ["other.exe", "sessionbridge.exe"]


# -- From a Git copy ------------------------------------------------------------------------------


def _git(folder, *args):
    result = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _commit(folder, version):
    (folder / "pyproject.toml").write_text(f'[project]\nname = "session-bridge"\nversion = "{version}"\n')
    _git(folder, "add", "pyproject.toml")
    _git(folder, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", version)
    _git(folder, "-c", "user.name=t", "-c", "user.email=t@example.com", "tag", "-a", f"v{version}", "-m", version)


@pytest.fixture
def checkout(monkeypatch, tmp_path, root):
    if shutil.which("git") is None:
        pytest.skip("Git is not installed")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    author = tmp_path / "author"
    subprocess.run(["git", "clone", "-q", str(origin), str(author)], check=True, capture_output=True)
    _git(author, "checkout", "-q", "-b", "main")
    _commit(author, "0.1.0")
    _git(author, "push", "-q", "origin", "main", "--tags")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    _commit(author, NEXT)
    _git(author, "push", "-q", "origin", "main", "--tags")
    monkeypatch.setattr(installation, "_REMOTES", {str(origin)})
    return clone


def _checkout_place(clone, tmp_path):
    blocker = installation.checkout_blocker(clone)
    command = tmp_path / "bin" / "sessionbridge.exe"
    return _in_use(Installation(CHECKOUT, clone, tmp_path / "rt", command, Path(sys.executable), blocker))


def test_a_clean_git_copy_moves_forward_to_the_release_tag_and_installs_from_it(
    web, root, installer, tmp_path, checkout
):
    publish(web, NEXT)
    command = updater.apply(_offer(_checkout_place(checkout, tmp_path)))
    assert command == installer.command
    assert _git(checkout, "describe", "--tags") == f"v{NEXT}"
    runtime = root / "runtime" / NEXT
    assert installer.calls == [{"project": checkout, "version": Version.parse(NEXT), "runtime": runtime}]
    assert (runtime / MARKER).is_file()


@pytest.mark.parametrize("change", ["dirty", "diverged", "branch"])
def test_a_git_copy_with_its_own_work_is_never_moved(web, root, installer, tmp_path, checkout, change):
    publish(web, NEXT)
    before = _git(checkout, "rev-parse", "HEAD")
    if change == "dirty":
        (checkout / "pyproject.toml").write_text("edited", encoding="utf-8")
    elif change == "diverged":
        (checkout / "mine.txt").write_text("mine", encoding="utf-8")
        _git(checkout, "add", "mine.txt")
        _git(checkout, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "mine")
        before = _git(checkout, "rev-parse", "HEAD")
    else:
        _git(checkout, "checkout", "-q", "-b", "feature")
    place = _checkout_place(checkout, tmp_path)
    assert place.blocker == change and not place.updatable
    with pytest.raises(BridgeError) as raised:
        updater.apply(_offer(place))
    assert raised.value.key == f"error.update_not_installable.{change}"
    assert _git(checkout, "rev-parse", "HEAD") == before and installer.calls == []


def test_a_failed_install_moves_the_git_copy_back(web, root, installer, tmp_path, checkout):
    publish(web, NEXT)
    before = _git(checkout, "rev-parse", "HEAD")
    installer.fail = True
    with pytest.raises(BridgeError):
        updater.apply(_offer(_checkout_place(checkout, tmp_path)))
    assert _git(checkout, "rev-parse", "HEAD") == before
    assert not (root / "runtime" / NEXT).exists()


# -- The handoff contract with release packages and install.ps1 -----------------------------------


def test_the_handoff_contract_matches_the_release_build_and_the_installer():
    """The updater of every released version relies on these names; changing them breaks updating."""
    project = Path(__file__).resolve().parents[1]
    build = (project / "scripts" / "build_portable.py").read_text(encoding="utf-8")
    assert 'f"session-bridge-{version}-portable.zip"' in build
    assert '"session-bridge/" + file.relative_to(ROOT).as_posix()' in build
    assert '(destination / "SHA256SUMS")' in build
    from session_bridge.releases import Release

    assert Release(Version(1, 2, 3), "x").portable == "session-bridge-1.2.3-portable.zip"
    script = (project / "scripts" / "install.ps1").read_text(encoding="utf-8")
    for parameter in ("[string]$Python", "[string]$CommandDirectory", "[string]$Runtime"):
        assert parameter in script
    assert "runtime = $Runtime" in script and "'installation.json'" in script
    assert updater.PREFIX == "session-bridge" and updater.SUMS == "SHA256SUMS"


# -- Review findings: stale windows, failures after the swap, interruptions ------------------------


def test_a_window_started_before_another_update_never_touches_the_installation_now_in_use(
    web, root, installer, tmp_path
):
    publish(web, NEXT)
    stale = _managed(tmp_path)
    # Another window already installed NEXT: the receipt names its folder, which is in use.
    live = root / "app" / NEXT / "session-bridge"
    live.mkdir(parents=True)
    updater._mark(live.parent)
    (live / "module.py").write_text("in use", encoding="utf-8")
    installation.receipt_path().write_text(json.dumps({"project": str(live), "command": "c"}), encoding="utf-8")
    with pytest.raises(BridgeError) as raised:
        updater.apply(_offer(stale))
    assert raised.value.key == "error.update_unavailable.already_updated"
    assert (live / "module.py").read_text(encoding="utf-8") == "in use" and installer.calls == []


def test_the_folder_the_receipt_names_is_never_discarded(root, tmp_path):
    folder = root / "app" / NEXT
    (folder / "session-bridge").mkdir(parents=True)
    updater._mark(folder)
    installation.receipt_path().write_text(json.dumps({"project": str(folder / "session-bridge")}), encoding="utf-8")
    with pytest.raises(BridgeError):
        updater._discard(folder)
    assert folder.is_dir()


def test_an_installer_that_fails_after_switching_still_counts_as_installed(web, root, installer, tmp_path, monkeypatch):
    publish(web, NEXT)
    place = _managed(tmp_path)
    switch = updater._install  # The fake installer, which writes the receipt.

    def switch_then_fail(project, current, version, runtime=None):
        switch(project, current, version, runtime)  # The receipt now names the new folder.
        raise BridgeError("update_failed", log="install.log")

    monkeypatch.setattr(updater, "_install", switch_then_fail)
    assert updater.apply(_offer(place)) == installer.command
    assert (root / "app" / NEXT / "session-bridge").is_dir()


def test_a_git_copy_is_not_moved_back_when_the_new_version_is_already_in_use(
    web, root, installer, tmp_path, checkout, monkeypatch
):
    publish(web, NEXT)
    switch = updater._install

    def switch_then_fail(project, current, version, runtime=None):
        switch(project, current, version, runtime)
        raise BridgeError("update_failed", log="install.log")

    monkeypatch.setattr(updater, "_install", switch_then_fail)
    updater.apply(_offer(_checkout_place(checkout, tmp_path)))
    assert _git(checkout, "describe", "--tags") == f"v{NEXT}"


def test_a_file_or_system_error_is_explained_instead_of_crashing(web, root, installer, tmp_path, monkeypatch):
    publish(web, NEXT)
    place = _managed(tmp_path)

    def no_space(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(updater.tempfile, "mkdtemp", no_space)
    with pytest.raises(BridgeError) as raised:
        updater.apply(_offer(place))
    assert raised.value.key == "error.update_failed.system"


def test_an_interrupted_installer_puts_the_renamed_command_back(root, tmp_path):
    place = _managed(tmp_path)
    place.command.parent.mkdir(parents=True)
    (place.command.parent / "sessionbridge.exe.old-20260101").write_bytes(b"old command")
    updater._restore_command(place)
    assert place.command.read_bytes() == b"old command"


def test_the_renamed_command_is_kept_while_the_command_is_missing(root, tmp_path):
    place = _managed(tmp_path)
    place.command.parent.mkdir(parents=True)
    aside = place.command.parent / "sessionbridge.exe.old-20260101"
    aside.write_bytes(b"old command")
    updater.prune(place)
    assert aside.is_file()


def test_ctrl_c_does_not_abandon_the_installer_halfway(monkeypatch):
    class Process:
        pid = 1
        calls = 0

        def wait(self, timeout=None):
            Process.calls += 1
            if Process.calls == 1:
                raise KeyboardInterrupt
            return 0

    assert updater._wait(Process()) == 0 and Process.calls == 2


def test_an_installer_that_never_ends_is_stopped_with_everything_it_started(monkeypatch):
    stopped = []

    class Process:
        pid = 42

        def wait(self, timeout=None):
            if stopped:
                return 1
            raise subprocess.TimeoutExpired("install.ps1", timeout)

    monkeypatch.setattr(updater, "INSTALL_TIMEOUT", 0)
    monkeypatch.setattr(updater, "_kill_tree", lambda process: stopped.append(process.pid))
    with pytest.raises(subprocess.TimeoutExpired):
        updater._wait(Process())
    assert stopped == [42]


def test_cleanup_keeps_a_folder_another_window_still_runs_from(root, tmp_path, monkeypatch):
    app = root / "app"
    for name in ("97.0.0", "98.0.0", "99.0.0"):
        (app / name / "session-bridge").mkdir(parents=True)
        updater._mark(app / name)
    real_rename = Path.rename

    def busy(self, target):
        if self.name == "97.0.0":
            raise PermissionError(32, "in use")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", busy)
    project = app / "99.0.0" / "session-bridge"
    updater.prune(Installation(MANAGED, project, project / ".venv"))
    assert sorted(child.name for child in app.iterdir()) == ["97.0.0", "98.0.0", "99.0.0"]
    monkeypatch.setattr(Path, "rename", real_rename)
    updater.prune(Installation(MANAGED, project, project / ".venv"))
    assert sorted(child.name for child in app.iterdir()) == ["98.0.0", "99.0.0"]


def test_an_offline_start_waits_an_hour_before_asking_github_again():
    updates.failed(now=1_000)
    assert not updates.due(now=1_000 + 60)
    assert updates.due(now=1_000 + updates.RETRY_INTERVAL + 1)


def test_a_failed_background_check_is_remembered(monkeypatch):
    from session_bridge import update_flow

    monkeypatch.setattr(installation, "detect", lambda: Installation(installation.OTHER))
    monkeypatch.setattr(updates, "due", lambda: True)

    def offline():
        raise BridgeError("update_unavailable", reason="network")

    monkeypatch.setattr(updates, "refresh", offline)
    check = update_flow.StartupCheck()
    check._done.wait(5)
    assert "failed_at" in json.loads(updates.state_path().read_text(encoding="utf-8"))


def test_a_new_command_copied_before_the_receipt_changed_is_replaced_by_the_previous_one(root, tmp_path):
    import hashlib

    place = _managed(tmp_path)
    place.command.parent.mkdir(parents=True)
    old = b"old command"
    record = json.loads(installation.receipt_path().read_text(encoding="utf-8"))
    record["sha256"] = hashlib.sha256(old).hexdigest()
    installation.receipt_path().write_text(json.dumps(record), encoding="utf-8")
    (place.command.parent / "sessionbridge.exe.old-20260101").write_bytes(old)
    place.command.write_bytes(b"new command of a folder about to be removed")
    updater._restore_command(place)
    assert place.command.read_bytes() == old
    # The command the receipt records is left alone.
    (place.command.parent / "sessionbridge.exe.old-20260102").write_bytes(b"other")
    updater._restore_command(place)
    assert place.command.read_bytes() == old


def test_an_update_waits_for_the_cleanup_run_at_start_instead_of_reporting_busy(root):
    import threading

    from session_bridge.locks import ByteLock

    root.mkdir(parents=True)
    cleanup = ByteLock(root / "update.lock")
    assert cleanup.acquire()
    threading.Timer(0.3, cleanup.release).start()
    with updater._exclusive(wait=5):
        pass
