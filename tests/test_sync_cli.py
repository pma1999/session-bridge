import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from session_bridge import cli, sync_cli
from session_bridge.errors import BridgeError
from session_bridge.sync_store import SyncStore


def test_check_calls_service_and_preserves_versioned_json(monkeypatch):
    calls = []

    class Service:
        def poll(self, link=None):
            calls.append(link)
            return {"changed": 2, "links": [], "errors": []}

    monkeypatch.setattr(sync_cli, "_service", Service)
    monkeypatch.setitem(cli._state, "json", True)
    result = CliRunner().invoke(sync_cli.sync_app, ["check", "my-link"])
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope == {"schema_version": 1, "ok": True, "data": {"changed": 2, "links": [], "errors": []}}
    assert calls == ["my-link"]


def test_link_starts_worker_only_after_success_and_honors_no_start(monkeypatch):
    calls = []

    class Service:
        def link(self, key, *, name=None, transfer_id=None):
            calls.append(("link", key, name, transfer_id))
            return {"id": "link-1", "name": name or "link-1"}

    monkeypatch.setattr(sync_cli, "_service", Service)
    monkeypatch.setattr(sync_cli, "_ensure_worker", lambda: calls.append(("start",)) or {"started": True})
    result = CliRunner().invoke(sync_cli.sync_app, ["link", "codex:thread-1", "--name", "Daily"])
    assert result.exit_code == 0, result.output
    assert calls == [("link", "codex:thread-1", "Daily", None), ("start",)]

    calls.clear()
    result = CliRunner().invoke(
        sync_cli.sync_app, ["link", "codex:thread-1", "--name", "Daily", "--no-start"]
    )
    assert result.exit_code == 0, result.output
    assert calls == [("link", "codex:thread-1", "Daily", None)]


def test_open_passes_materialization_options_without_launching_in_non_tty(monkeypatch):
    calls = []

    class Service:
        def open(self, link, target, **options):
            calls.append((link, target, options))
            return {"link_id": link, "profile": target, "launched": options["launch"]}

    monkeypatch.setattr(sync_cli, "_service", Service)
    monkeypatch.setattr(sync_cli, "_launch_allowed", lambda: False)
    monkeypatch.setattr(sync_cli, "_ensure_worker", lambda: {"running": True})
    monkeypatch.setitem(cli._state, "json", True)
    result = CliRunner().invoke(
        sync_cli.sync_app,
        ["open", "work", "--to", "codex-win", "--branch", "rv_1", "--cwd", "C:/work", "--model", "m"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["launched"] is False
    assert calls == [
        (
            "work",
            "codex-win",
            {
                "branch": "rv_1",
                "cwd": "C:/work",
                "model": "m",
                "launch": False,
            },
        )
    ]


def test_launch_requires_text_mode_and_both_terminal_streams(monkeypatch):
    class Stream:
        def __init__(self, tty):
            self.tty = tty

        def isatty(self):
            return self.tty

    monkeypatch.setattr(sync_cli.sys, "stdin", Stream(True))
    monkeypatch.setattr(sync_cli.sys, "stdout", Stream(True))
    monkeypatch.setitem(cli._state, "json", False)
    assert sync_cli._launch_allowed() is True

    monkeypatch.setitem(cli._state, "json", True)
    assert sync_cli._launch_allowed() is False

    monkeypatch.setitem(cli._state, "json", False)
    monkeypatch.setattr(sync_cli.sys, "stdout", Stream(False))
    assert sync_cli._launch_allowed() is False


def test_non_json_cli_output_has_no_terminal_color_sequences(monkeypatch):
    class Service:
        def list(self):
            return [{"id": "link-1", "state": "waiting"}]

    monkeypatch.setattr(sync_cli, "_service", Service)
    monkeypatch.setitem(cli._state, "json", False)
    result = CliRunner().invoke(sync_cli.sync_app, ["list"])

    assert result.exit_code == 0, result.output
    assert "link-1" in result.output
    assert "\x1b[" not in result.output


def test_convert_sync_dry_run_has_no_link_worker_or_data_write(monkeypatch, tmp_path):
    calls = []

    class Bridge:
        data_dir = tmp_path

        def preview(self, key, target, **options):
            calls.append(("preview", key, target, options))
            return {"preview": True}

        def convert(self, *args, **kwargs):
            raise AssertionError("dry-run must not convert")

    monkeypatch.setattr(cli, "_bridge", lambda: Bridge())
    monkeypatch.setattr(sync_cli, "_ensure_worker", lambda *args: pytest.fail("dry-run started worker"))
    monkeypatch.setitem(cli._state, "json", True)
    result = CliRunner().invoke(
        cli.app,
        [
            "--json",
            "convert",
            "codex:thread-1",
            "--to",
            "claude-win",
            "--sync",
            "--sync-name",
            "Daily",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"] == {"preview": True}
    assert calls == [
        (
            "preview",
            "codex:thread-1",
            "claude-win",
            {
                "cwd": None,
                "context": "current",
                "context_from": None,
                "snapshot": False,
                "mode": "native",
                "model": None,
                "variant": None,
            },
        )
    ]
    assert list(Path(tmp_path).iterdir()) == []


def test_convert_refuses_sync_handoff_and_sync_name_without_sync(monkeypatch):
    class Bridge:
        def preview(self, *args, **kwargs):
            pytest.fail("invalid sync options reached preview")

        def convert(self, *args, **kwargs):
            pytest.fail("invalid sync options reached conversion")

    monkeypatch.setattr(cli, "_bridge", lambda: Bridge())
    monkeypatch.setattr(
        sync_cli, "_ensure_worker", lambda *args: pytest.fail("invalid sync option started worker")
    )

    for args in (
        ["--json", "convert", "codex:thread-1", "--to", "claude-win", "--mode", "handoff", "--sync"],
        ["--json", "convert", "codex:thread-1", "--to", "claude-win", "--sync-name", "Daily"],
        # Sync follows the version the source resumes, so another one cannot be linked.
        ["--json", "convert", "codex:thread-1", "--to", "claude-win", "--sync", "--variant", "v_1"],
    ):
        result = CliRunner().invoke(cli.app, args)
        assert isinstance(result.exception, BridgeError)
        assert result.exception.code == "invalid_options"


def test_convert_sync_links_exact_transfer_and_resume_opens_link_through_service(monkeypatch, tmp_path):
    calls = []

    class Transfer:
        id = "tr_exact"

        def model_dump(self, mode="json"):
            return {"id": self.id, "state": "completed"}

    class Bridge:
        data_dir = tmp_path

        def convert(self, key, target, **options):
            calls.append(("convert", key, target, options))
            return Transfer()

    class Service:
        def __init__(self, *, bridge):
            assert bridge is fake_bridge

        def link(self, key, *, name, transfer_id):
            calls.append(("link", key, name, transfer_id))
            return {"id": "ln_exact", "name": name}

        def open(self, link, target, *, cwd, model, launch):
            calls.append(("open", link, target, cwd, model, launch))
            return {"link_id": link, "profile": target, "launched": launch, "native_id": "native-current"}

    fake_bridge = Bridge()
    worker_calls = []
    monkeypatch.setattr(cli, "_bridge", lambda: fake_bridge)
    monkeypatch.setattr("session_bridge.sync.SyncService", Service)
    monkeypatch.setattr(
        sync_cli, "_ensure_worker", lambda root: worker_calls.append(root) or {"running": True}
    )
    monkeypatch.setattr(sync_cli, "_launch_allowed", lambda: False)
    result = CliRunner().invoke(
        cli.app,
        [
            "--json",
            "convert",
            "codex:thread-1",
            "--to",
            "claude-win",
            "--sync",
            "--sync-name",
            "Daily",
            "--resume",
            "--cwd",
            "C:/work",
            "--model",
            "chosen",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0] == "convert"
    assert calls[1] == ("link", "codex:thread-1", "Daily", "tr_exact")
    assert calls[2] == ("open", "ln_exact", "claude-win", "C:/work", "chosen", False)
    assert worker_calls == [tmp_path]
    envelope = json.loads(result.output)
    assert envelope["data"]["transfer"]["id"] == "tr_exact"
    assert envelope["data"]["resume"]["native_id"] == "native-current"


def test_resume_routes_exact_linked_key_through_sync_only_with_store_and_non_tty(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "sync.sqlite3").touch()
    calls = []

    class Bridge:
        def __init__(self, root):
            self.data_dir = root

        def resume(self, *args, **kwargs):
            pytest.fail("linked session bypassed sync service")

    bridge = Bridge(data_dir)

    class Service:
        def __init__(self, *, bridge):
            assert bridge is bridge_arg

        def lookup(self, key):
            calls.append(("lookup", key))
            return ("ln_exact", "codex-win")

        def open(self, link, target, *, launch):
            calls.append(("open", link, target, launch))
            return {"link_id": link, "profile": target, "launched": launch}

    bridge_arg = bridge
    worker_calls = []
    monkeypatch.setattr(cli, "_bridge", lambda: bridge)
    monkeypatch.setattr("session_bridge.sync.SyncService", Service)
    monkeypatch.setattr(
        sync_cli, "_ensure_worker", lambda root: worker_calls.append(root) or {"running": True}
    )
    result = CliRunner().invoke(cli.app, ["--json", "resume", "codex:exact-native-id"])

    assert result.exit_code == 0, result.output
    assert calls == [("lookup", "codex:exact-native-id"), ("open", "ln_exact", "codex-win", False)]
    assert worker_calls == [data_dir]
    assert json.loads(result.output)["data"]["launched"] is False


def test_resume_keeps_unlinked_oneoff_on_bridge_and_does_not_start_worker(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    calls = []

    class Bridge:
        def __init__(self, root):
            self.data_dir = root

        def resume(self, key, *, launch):
            calls.append((key, launch))
            return {"mode": "native", "argv": ["codex", "resume", key], "launched": launch}

    monkeypatch.setattr(cli, "_bridge", lambda: Bridge(data_dir))
    monkeypatch.setattr(
        "session_bridge.sync.SyncService", lambda **kwargs: pytest.fail("no sync store exists")
    )
    monkeypatch.setattr(sync_cli, "_ensure_worker", lambda *args: pytest.fail("one-off started worker"))
    result = CliRunner().invoke(cli.app, ["--json", "resume", "codex:oneoff-id"])

    assert result.exit_code == 0, result.output
    assert calls == [("codex:oneoff-id", False)]
    payload = json.loads(result.output)["data"]
    assert payload["launched"] is False
    assert payload["reason"].startswith("Use una terminal interactiva")


@pytest.mark.parametrize("precreate_destination", [False, True])
def test_sync_restore_publishes_only_verified_sync_store(monkeypatch, tmp_path, precreate_destination):
    source = SyncStore(tmp_path / "source")
    backup = tmp_path / "history.sqlite3"
    source.backup(backup)
    destination = tmp_path / "restored"
    if precreate_destination:
        destination.mkdir()
    monkeypatch.setattr(cli, "_bridge", lambda: pytest.fail("restore must not open Bridge"))
    monkeypatch.setattr(sync_cli, "_ensure_worker", lambda *args: pytest.fail("restore started worker"))

    result = CliRunner().invoke(
        cli.app, ["--json", "sync", "restore", str(backup), "--into", str(destination)]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"] == {
        "path": str(destination / "sync.sqlite3"),
        "schema_version": SyncStore.SCHEMA_VERSION,
        "history_only": True,
    }
    assert sorted(path.name for path in destination.iterdir()) == ["sync.sqlite3"]
    assert not (destination / "bridge.sqlite3").exists()


def test_sync_restore_refuses_nonempty_target_without_touching_it(monkeypatch, tmp_path):
    source = SyncStore(tmp_path / "source")
    backup = tmp_path / "history.sqlite3"
    source.backup(backup)
    destination = tmp_path / "existing"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("unrelated", encoding="utf-8")
    monkeypatch.setattr(cli, "_bridge", lambda: pytest.fail("restore must not open Bridge"))
    monkeypatch.setattr(sync_cli, "_ensure_worker", lambda *args: pytest.fail("restore started worker"))

    result = CliRunner().invoke(
        cli.app, ["--json", "sync", "restore", str(backup), "--into", str(destination)]
    )

    assert isinstance(result.exception, BridgeError)
    assert result.exception.code == "sync_restore_not_empty"
    assert list(destination.iterdir()) == [marker]
