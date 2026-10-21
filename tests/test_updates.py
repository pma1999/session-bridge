"""Looking for updates: the network rules, GitHub's answer, the installation in use and what the user decided."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from update_fakes import REPO, FakeWeb, publish

from session_bridge import __version__, cli, installation, net, releases, update_flow, updates
from session_bridge.errors import BridgeError
from session_bridge.installation import CHECKOUT, MANAGED, OTHER, Installation
from session_bridge.releases import Version

NEXT = "99.0.0"


@pytest.fixture
def web(monkeypatch):
    fake = FakeWeb()
    monkeypatch.setattr(net, "_open", fake)
    return fake


@pytest.fixture
def local_app_data(monkeypatch, tmp_path):
    folder = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(folder))
    return folder / "SessionBridge"


def managed(tmp_path) -> Installation:
    command = tmp_path / "bin" / "sessionbridge.exe"
    return Installation(MANAGED, tmp_path / "project", tmp_path / "project" / ".venv", command, Path(sys.executable))


# -- Versions and GitHub's answer -----------------------------------------------------------------


def test_the_package_version_is_a_plain_release_number():
    assert str(Version.parse(__version__)) == __version__
    assert updates.current() == Version.parse(__version__)


@pytest.mark.parametrize("text", ["1.2", "v1.2.3-rc1", "1.2.3.4", "latest", "", None, " 1.2.3"])
def test_only_final_release_numbers_are_versions(text):
    assert Version.parse(text) is None


def test_versions_compare_as_numbers_not_text():
    assert Version.parse("v0.10.0") > Version.parse("0.9.9") > Version.parse("0.9.0")
    assert Version.parse("v1.0.0").tag == "v1.0.0"


def test_a_release_keeps_only_this_repositorys_assets_and_their_digests(web):
    payload = publish(web, NEXT)
    payload["assets"].append(
        {"name": "evil.zip", "size": 1, "browser_download_url": "https://example.com/evil.zip", "digest": None}
    )
    release = releases.parse(payload)
    assert release.version == Version.parse(NEXT)
    assert set(release.assets) == {f"session-bridge-{NEXT}-portable.zip", "SHA256SUMS"}
    assert all(asset.sha256 and len(asset.sha256) == 64 for asset in release.assets.values())
    assert releases.Release.from_dict(release.as_dict()) == release


@pytest.mark.parametrize(
    "change",
    [
        {"prerelease": True},
        {"draft": True},
        {"tag_name": "v1.2.3-beta"},
        {"html_url": "https://github.com/someone/else/releases/tag/v99.0.0"},
    ],
)
def test_anything_but_a_final_release_of_this_repository_is_ignored(web, change):
    payload = {**publish(web, NEXT), **change}
    assert releases.parse(payload) is None


def test_github_is_asked_conditionally_and_a_304_keeps_the_known_release(web):
    publish(web, NEXT, etag='"abc"')
    assert updates.refresh(now=1000).version == Version.parse(NEXT)
    web.routes[releases.API_URL].status = 304
    assert updates.refresh(now=2000).version == Version.parse(NEXT)
    assert web.requests[-1].get_header("If-none-match") == '"abc"'
    assert updates.known().version == Version.parse(NEXT)


def test_github_limiting_requests_is_explained(web):
    publish(web, NEXT)
    web.routes[releases.API_URL].status = 403
    with pytest.raises(BridgeError) as raised:
        updates.refresh()
    assert raised.value.code == "update_unavailable"
    assert raised.value.key == "error.update_unavailable.rate_limited"


def test_no_network_is_an_update_unavailable_error(web):
    with pytest.raises(BridgeError) as raised:
        releases.latest()
    assert raised.value.code == "update_unavailable"


# -- Network rules ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "redirected", "allowed"),
    [
        ("https://api.github.com/repos/x", False, True),
        ("https://github.com/x", False, True),
        ("http://github.com/x", False, False),
        ("https://github.com:8443/x", False, False),
        ("https://user@github.com/x", False, False),
        ("https://github.com.evil.example/x", False, False),
        ("https://release-assets.githubusercontent.com/x", False, False),
        ("https://release-assets.githubusercontent.com/x", True, True),
        ("https://evilgithubusercontent.com/x", True, False),
        ("https://example.com/x", True, False),
    ],
)
def test_requests_go_only_to_github_and_downloads_only_to_its_file_host(url, redirected, allowed):
    assert releases.HOSTS.allows(url, redirected=redirected) is allowed


def test_a_redirect_to_another_host_is_refused_before_it_is_followed():
    handler = net._Redirects(releases.HOSTS)
    with pytest.raises(net.NetError):
        handler.redirect_request(None, None, 302, "Found", {}, "https://example.com/payload.zip")


def test_a_download_is_kept_only_when_size_and_digest_match(web, tmp_path):
    import hashlib

    body = b"release bytes"
    web.add("https://github.com/file", body)
    good = net.download(
        "https://github.com/file", tmp_path, hosts=releases.HOSTS, max_bytes=100, timeout=1,
        sha256=hashlib.sha256(body).hexdigest(), size=len(body),
    )
    assert good.read_bytes() == body
    for options in ({"sha256": "0" * 64}, {"sha256": hashlib.sha256(body).hexdigest(), "max_bytes": 4}):
        with pytest.raises(net.NetError):
            net.download("https://github.com/file", tmp_path, hosts=releases.HOSTS, timeout=1,
                         **{"max_bytes": 100, **options})
    assert sorted(path.name for path in tmp_path.iterdir()) == [good.name]


# -- The installation in use ----------------------------------------------------------------------


def _receipt(local_app_data: Path, **values) -> None:
    local_app_data.mkdir(parents=True, exist_ok=True)
    (local_app_data / "installation.json").write_text(json.dumps(values), encoding="utf-8")


def _runtime(folder: Path) -> Path:
    folder.mkdir(parents=True)
    (folder / "pyvenv.cfg").write_text(f"home = x\nexecutable = {sys.executable}\n", encoding="utf-8")
    return folder


def test_an_installation_from_a_release_zip_can_update_itself(local_app_data, tmp_path):
    project = tmp_path / "session-bridge"
    runtime = _runtime(project / ".venv")
    command = tmp_path / "bin" / "sessionbridge.exe"
    command.parent.mkdir()
    command.write_bytes(b"exe")
    # A receipt from 0.7 has no runtime: it was the project's .venv.
    _receipt(local_app_data, project=str(project), command=str(command), sha256="x")
    found = installation.detect(prefix=str(runtime), platform="win32")
    assert (found.kind, found.updatable, found.python) == (MANAGED, True, Path(sys.executable))
    # Another interpreter, such as a development run, is not that installation.
    assert installation.detect(prefix=str(tmp_path), platform="win32").kind == OTHER
    assert installation.detect(prefix=str(runtime), platform="linux").kind == OTHER


def test_a_git_copy_is_a_checkout_whose_blocker_is_reported(local_app_data, tmp_path, monkeypatch):
    project = tmp_path / "clone"
    (project / ".git").mkdir(parents=True)
    runtime = _runtime(tmp_path / "runtime")
    command = tmp_path / "sessionbridge.exe"
    command.write_bytes(b"exe")
    _receipt(local_app_data, project=str(project), runtime=str(runtime), command=str(command))
    monkeypatch.setattr(installation, "checkout_blocker", lambda path: "dirty")
    found = installation.detect(prefix=str(runtime), platform="win32")
    assert (found.kind, found.blocker, found.updatable) == (CHECKOUT, "dirty", False)


def test_without_a_receipt_or_base_interpreter_nothing_can_update_itself(local_app_data, tmp_path):
    assert installation.detect(platform="win32").kind == OTHER
    project = tmp_path / "p"
    (project / ".venv").mkdir(parents=True)
    command = tmp_path / "sessionbridge.exe"
    command.write_bytes(b"exe")
    _receipt(local_app_data, project=str(project), command=str(command))
    assert installation.detect(prefix=str(project / ".venv"), platform="win32").kind == OTHER


# -- What the user decided ------------------------------------------------------------------------


def test_github_is_asked_at_most_once_a_day(web):
    publish(web, NEXT)
    assert updates.due(now=1_000_000)
    updates.refresh(now=1_000_000)
    assert not updates.due(now=1_000_000 + 3600)
    assert updates.due(now=1_000_000 + updates.CHECK_INTERVAL + 1)


def test_not_now_waits_a_day_and_skip_waits_for_the_next_version(web, tmp_path):
    publish(web, NEXT)
    updates.refresh(now=0)
    place = managed(tmp_path)
    assert updates.pending(place, now=10).latest == Version.parse(NEXT)
    updates.snooze(now=10)
    assert updates.pending(place, now=20) is None
    assert updates.pending(place, now=10 + updates.SNOOZE + 1) is not None
    updates.skip(Version.parse(NEXT))
    assert updates.pending(place, now=10**9) is None
    publish(web, "99.0.1")
    updates.refresh(now=10**9)
    assert updates.pending(place, now=10**9).latest == Version.parse("99.0.1")


def test_a_notice_for_an_installation_that_cannot_update_shows_once_a_day(web):
    publish(web, NEXT)
    updates.refresh(now=0)
    other = Installation(OTHER)
    assert updates.pending(other, now=100) is not None
    updates.noticed(now=100)
    assert updates.pending(other, now=200) is None
    assert updates.pending(other, now=100 + updates.NOTICE_INTERVAL + 1) is not None


def test_an_older_or_equal_release_is_never_offered(web, tmp_path):
    publish(web, __version__)
    updates.refresh()
    assert updates.offer(managed(tmp_path)) is None


def test_a_damaged_state_file_reads_as_no_state(tmp_path):
    updates.state_path().parent.mkdir(parents=True, exist_ok=True)
    updates.state_path().write_text("{not json", encoding="utf-8")
    assert updates.known() is None and updates.due()


def test_the_startup_check_is_on_unless_the_user_or_environment_turns_it_off(monkeypatch):
    monkeypatch.delenv(updates.DISABLE_VARIABLE)
    assert updates.startup_check()
    updates.set_startup_check(False)
    assert not updates.startup_check()
    updates.set_startup_check(True)
    assert updates.startup_check()
    monkeypatch.setenv(updates.DISABLE_VARIABLE, "1")
    assert not updates.startup_check()


# -- The offer at start ---------------------------------------------------------------------------


def _answer(monkeypatch, value):
    asked = []

    def ask(message, options, *, default=None):
        asked.append((message, [label for _, label in options], default))
        return value

    monkeypatch.setattr("session_bridge.interactive._ask", ask)
    return asked


class _Done:
    def __init__(self, offer):
        self.offer = offer

    def result(self):
        return self.offer


def _offer(web, place) -> updates.Offer:
    publish(web, NEXT)
    return updates.offer(place, updates.refresh())


def test_the_offer_names_both_versions_and_defaults_to_updating(monkeypatch, web, tmp_path, lang):
    lang("en")
    offer = _offer(web, managed(tmp_path))
    asked = _answer(monkeypatch, "later")
    assert update_flow.at_start(_Done(offer), []) is None
    message, labels, default = asked[0]
    assert f"Session Bridge {NEXT} is available (you have {__version__})." in message
    assert f"{REPO}/releases/tag/v{NEXT}" in message
    assert labels == ["Update now", "Not now", "Skip this version"] and default == "now"
    assert updates.pending(offer.installation) is None  # Not now: asked again tomorrow.


def test_escape_is_not_now_and_skip_remembers_the_version(monkeypatch, web, tmp_path):
    from session_bridge.interactive import BACK

    offer = _offer(web, managed(tmp_path))
    _answer(monkeypatch, BACK)
    update_flow.at_start(_Done(offer), [])
    assert json.loads(updates.state_path().read_text(encoding="utf-8"))["snoozed_until"] > 0
    _answer(monkeypatch, "skip")
    update_flow.at_start(_Done(offer), [])
    assert json.loads(updates.state_path().read_text(encoding="utf-8"))["skipped"] == NEXT


def test_updating_now_hands_back_the_new_command_to_open(monkeypatch, web, tmp_path):
    offer = _offer(web, managed(tmp_path))
    _answer(monkeypatch, "now")
    monkeypatch.setattr(update_flow, "install", lambda offer: tmp_path / "new.exe")
    relaunch = update_flow.at_start(_Done(offer), [])
    assert relaunch == update_flow.Relaunch(tmp_path / "new.exe", __version__)


def test_a_failed_update_is_explained_and_the_start_screen_continues(monkeypatch, web, tmp_path, capsys):
    offer = _offer(web, managed(tmp_path))
    _answer(monkeypatch, "now")

    def fail(offer):
        raise BridgeError("update_failed", log="C:/log.txt")

    monkeypatch.setattr(update_flow, "install", fail)
    assert update_flow.at_start(_Done(offer), []) is None
    shown = capsys.readouterr().out
    assert "update_failed" in shown and "C:/log.txt" in shown and __version__ in shown


def test_a_copy_that_cannot_update_gets_one_line_instead_of_a_question(monkeypatch, web, tmp_path, lang):
    lang("en")
    place = Installation(CHECKOUT, tmp_path, tmp_path, tmp_path / "x.exe", Path(sys.executable), blocker="dirty")
    offer = _offer(web, place)
    asked = _answer(monkeypatch, "now")
    notices = []
    assert update_flow.at_start(_Done(offer), notices) is None
    assert asked == []
    assert notices == [
        f"Session Bridge {NEXT} is available. Your Git copy has uncommitted changes. "
        "Update by hand with git pull, then scripts\\install.ps1."
    ]


def test_the_new_version_says_what_it_replaced_once(monkeypatch):
    monkeypatch.setenv(updates.UPDATED_FROM_VARIABLE, "0.7.1")
    notices = []
    update_flow.at_start(None, notices)
    assert notices == ["Actualizado desde 0.7.1."]
    assert updates.UPDATED_FROM_VARIABLE not in __import__("os").environ


def test_the_startup_check_never_waits_for_a_slow_github(monkeypatch):
    import threading

    gate = threading.Event()
    monkeypatch.setattr(update_flow, "STARTUP_WAIT", 0.05)
    monkeypatch.setattr(installation, "detect", lambda: Installation(OTHER))
    monkeypatch.setattr(updates, "due", lambda: True)
    monkeypatch.setattr(updates, "refresh", lambda: gate.wait(5))
    check = update_flow.StartupCheck()
    assert check.result() is None
    gate.set()


# -- The update command ---------------------------------------------------------------------------


def _main(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["sessionbridge", *argv])
    try:
        cli.main()
    except SystemExit as exit_:
        code = exit_.code
    else:
        code = 0
    return code, capsys.readouterr()


def test_update_check_reports_in_json(monkeypatch, capsys, web, local_app_data):
    publish(web, NEXT)
    code, output = _main(monkeypatch, capsys, "--json", "update", "--check")
    data = json.loads(output.out)["data"]
    assert code == 0
    assert data["current"] == __version__ and data["latest"] == NEXT
    assert data["available"] is True and data["kind"] == OTHER and data["installable"] is False


def test_update_says_when_up_to_date(monkeypatch, capsys, web, lang):
    lang("en")
    publish(web, __version__)
    code, output = _main(monkeypatch, capsys, "--lang", "en", "update")
    assert code == 0 and f"Session Bridge {__version__} is up to date." in output.out


def test_update_without_a_terminal_needs_yes(monkeypatch, capsys, web, tmp_path):
    publish(web, NEXT)
    monkeypatch.setattr(installation, "detect", lambda: managed(tmp_path))
    code, output = _main(monkeypatch, capsys, "--json", "update")
    assert code == 2 and json.loads(output.out)["error"]["code"] == "update_needs_yes"


def test_update_yes_installs_and_reports(monkeypatch, capsys, web, tmp_path):
    publish(web, NEXT)
    monkeypatch.setattr(installation, "detect", lambda: managed(tmp_path))
    monkeypatch.setattr(update_flow, "install", lambda offer, quiet=False: tmp_path / "new.exe")
    code, output = _main(monkeypatch, capsys, "--json", "update", "--yes")
    data = json.loads(output.out)["data"]
    assert code == 0 and data["updated"] is True and data["command"] == str(tmp_path / "new.exe")


def test_startup_check_can_be_turned_off_and_on(monkeypatch, capsys):
    monkeypatch.delenv(updates.DISABLE_VARIABLE)
    code, output = _main(monkeypatch, capsys, "update", "--startup-check", "off")
    assert code == 0 and not updates.startup_check()
    assert "no buscará" in output.out
    _main(monkeypatch, capsys, "update", "--startup-check", "on")
    assert updates.startup_check()
    code, output = _main(monkeypatch, capsys, "--json", "update", "--startup-check", "maybe")
    assert code == 2


def test_update_texts_come_from_the_catalogs(monkeypatch, web, tmp_path, pseudo, capsys):
    import re

    def outside(text):
        # Versions, folders and addresses are data; everything else must be catalog text.
        return pseudo(re.sub(r"https://[^\s⟧]+|[A-Za-z]:/[^\s⟧]*", " ", text.replace("\\", "/"))).split()

    offer = _offer(web, managed(tmp_path))
    asked = _answer(monkeypatch, "now")

    def fail(offer):
        raise BridgeError("update_failed", log="C:/logs/update.log")

    monkeypatch.setattr(update_flow, "install", fail)
    update_flow.at_start(_Done(offer), [])
    message, labels, _ = asked[0]
    assert outside(message) == [] and all(outside(label) == [] for label in labels)
    assert outside(capsys.readouterr().out) == []
    for place in (
        Installation(OTHER),
        Installation(CHECKOUT, tmp_path, tmp_path, tmp_path, Path(sys.executable), blocker="diverged"),
    ):
        assert outside(update_flow.notice(updates.Offer(offer.current, offer.release, place))) == []


def test_the_launcher_exe_leaves_the_import_path_so_it_can_be_replaced_while_running(monkeypatch, tmp_path):
    import session_bridge

    launcher = tmp_path / "sessionbridge.exe"
    launcher.write_bytes(b"PK")
    monkeypatch.setattr(sys, "path", [str(launcher), str(tmp_path), *sys.path])
    sys.path_importer_cache[str(launcher)] = None
    session_bridge._release_launcher()
    assert str(launcher) not in sys.path and str(tmp_path) in sys.path
    assert str(launcher) not in sys.path_importer_cache


def test_the_start_screen_reopens_the_new_version_and_ends_with_its_exit_code(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_bridge", lambda: None)
    target = update_flow.Relaunch(tmp_path / "sessionbridge.exe", "0.7.1")
    monkeypatch.setattr("session_bridge.interactive.run_home", lambda bridge, **options: target)
    opened = []
    monkeypatch.setattr(update_flow, "relaunch", lambda relaunch, args: opened.append((relaunch, args)) or 7)
    code, _ = _main(monkeypatch, capsys, "--all-projects")
    assert code == 7 and opened == [(target, ["--all-projects"])]


def test_the_new_version_is_opened_with_the_same_arguments_and_the_old_version_named(monkeypatch, tmp_path):
    seen = {}

    class Process:
        def __init__(self, argv, env):
            seen.update(argv=argv, env=env)

        def wait(self):
            return 3

    monkeypatch.setattr(update_flow.subprocess, "Popen", Process)
    code = update_flow.relaunch(update_flow.Relaunch(tmp_path / "new.exe", "0.7.1"), ["--lang", "en"])
    assert code == 3 and seen["argv"] == [str(tmp_path / "new.exe"), "--lang", "en"]
    assert seen["env"][updates.UPDATED_FROM_VARIABLE] == "0.7.1"


def test_an_answer_that_cannot_be_saved_does_not_stop_the_start_screen(monkeypatch, web, tmp_path):
    offer = _offer(web, managed(tmp_path))
    _answer(monkeypatch, "later")

    def read_only(*args, **kwargs):
        raise PermissionError(13, "read-only data folder")

    monkeypatch.setattr(updates, "snooze", read_only)
    assert update_flow.at_start(_Done(offer), []) is None
