from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path
from types import SimpleNamespace

from session_bridge import cli
from session_bridge.models import Conversation, Event, SessionRef


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.refs = [
            SessionRef(profile_id="local", native_id="abc", tool="codex", title="A [bold]title\x1b[31m")
        ]

    def list_sessions(self, **kwargs):
        self.calls.append(("list_sessions", kwargs))
        return self.refs

    def preview(self, *args, **kwargs):
        self.calls.append(("preview", args, kwargs))
        return {"preview": True}

    def convert(self, *args, **kwargs):
        self.calls.append(("convert", args, kwargs))
        return SimpleNamespace(id="tr_test", model_dump=lambda mode="json": {"id": "tr_test"})

    def preview_package(self, *args, **kwargs):
        self.calls.append(("preview_package", args, kwargs))
        return {"package_preview": True}

    def import_package(self, *args, **kwargs):
        self.calls.append(("import_package", args, kwargs))
        return {"package_imported": True}

    def resume(self, key, launch=True):
        self.calls.append(("resume", key, launch))
        return {"mode": "native", "argv": ["codex", "resume", "id"], "launched": launch}

    def recover(self, key):
        self.calls.append(("recover", key))
        return {"state": "recovery"}

    def show(self, key, snapshot=False):
        self.calls.append(("show", key, snapshot))
        return Conversation(
            source=self.refs[0], events=[Event(id="1", kind="message", role="user", text="[bold]body\x1b[2J")]
        )

    def profiles(self):
        return []

    def register_profile(self, profile):
        self.calls.append(("register_profile", profile))
        return profile


def _run_main(monkeypatch, capsys, argv):
    monkeypatch.setattr(cli, "_bridge", lambda: fake)
    monkeypatch.setattr(sys, "argv", ["sessionbridge", *argv])
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code
    else:
        code = 0
    return code, capsys.readouterr()


def test_list_defaults_to_launch_directory_and_all_projects_is_explicit(monkeypatch, capsys):
    global fake
    fake = FakeBridge()
    code, output = _run_main(monkeypatch, capsys, ["list", "--json"])
    assert code == 0, output
    assert fake.calls[-1][1]["cwd"] == str(Path.cwd())
    fake.calls.clear()
    code, output = _run_main(monkeypatch, capsys, ["list", "--all-projects", "--json"])
    assert code == 0, output
    assert fake.calls[-1][1]["cwd"] is None


def test_list_cannot_silently_combine_global_scope_and_explicit_directory(monkeypatch, capsys):
    global fake
    fake = FakeBridge()
    code, output = _run_main(monkeypatch, capsys, ["list", "--all-projects", "--cwd", "/project", "--json"])
    assert code == 2
    assert json.loads(output.out)["error"]["code"] == "invalid_arguments"
    assert fake.calls == []


def test_interactive_entrypoint_passes_launch_directory_and_explicit_global_scope(monkeypatch, capsys):
    global fake
    fake = FakeBridge()
    calls = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    def home(bridge, **options):
        calls.append(options)
        return {"cancelled": True}

    monkeypatch.setattr("session_bridge.interactive.run_home", home)
    assert _run_main(monkeypatch, capsys, [])[0] == 0
    assert _run_main(monkeypatch, capsys, ["--all-projects"])[0] == 0
    assert calls == [{"cwd": str(Path.cwd())}, {"cwd": None}]


def test_json_global_flag_after_command_and_metadata_filters(monkeypatch, capsys):
    global fake
    fake = FakeBridge()
    code, result = _run_main(
        monkeypatch,
        capsys,
        ["list", "--from", "codex", "--search", "demo", "--since", "2026-01-02", "--json"],
    )
    assert code == 0, result
    assert result.err == ""
    envelope = json.loads(result.out)
    assert envelope["schema_version"] == 1 and envelope["ok"] is True
    assert fake.calls == [
        (
            "list_sessions",
            {
                "tool": "codex",
                "profile": None,
                "search": "demo",
                "cwd": str(Path.cwd()),
                "archived": None,
                "parent": None,
                "since": "2026-01-02",
                "until": None,
            },
        )
    ]


def test_dry_run_only_previews(monkeypatch, capsys):
    global fake
    fake = FakeBridge()
    code, result = _run_main(
        monkeypatch, capsys, ["convert", "local:abc", "--to", "other", "--dry-run", "--json"]
    )
    assert code == 0
    assert json.loads(result.out)["data"] == {"preview": True}
    assert [call[0] for call in fake.calls] == ["preview"]


def test_import_dry_run_uses_package_preview(monkeypatch, capsys, tmp_path):
    global fake
    fake = FakeBridge()
    package = tmp_path / "conversation.sbridge"
    code, result = _run_main(
        monkeypatch, capsys, ["import", str(package), "--to", "other", "--dry-run", "--json"]
    )
    assert code == 0
    assert json.loads(result.out)["data"] == {"package_preview": True}
    assert [call[0] for call in fake.calls] == ["preview_package"]


def test_convert_resume_in_json_returns_exact_argv_without_launch(monkeypatch, capsys):
    global fake
    fake = FakeBridge()
    code, result = _run_main(
        monkeypatch, capsys, ["convert", "local:abc", "--to", "other", "--resume", "--json"]
    )
    assert code == 0
    assert [call[0] for call in fake.calls] == ["convert", "resume"]
    assert fake.calls[-1] == ("resume", "tr_test", False)
    assert json.loads(result.out)["data"]["transfer"]["id"] == "tr_test"


def test_recover_calls_journal_recovery(monkeypatch, capsys):
    global fake
    fake = FakeBridge()
    code, result = _run_main(monkeypatch, capsys, ["recover", "tr_123", "--json"])
    assert code == 0
    assert fake.calls == [("recover", "tr_123")]
    assert json.loads(result.out)["data"] == {"state": "recovery"}


def test_profile_add_preserves_linux_home_and_helper_paths(monkeypatch, capsys):
    global fake
    fake = FakeBridge()
    code, result = _run_main(
        monkeypatch,
        capsys,
        [
            "profile",
            "add",
            "--id",
            "codex-ubuntu",
            "--tool",
            "codex",
            "--home",
            "/home/p/.codex",
            "--environment",
            "wsl",
            "--distro",
            "Ubuntu",
            "--helper",
            "/home/p/.local/venv/bin/python",
            "--version",
            "1.2.3",
            "--machine",
            "wsl:Ubuntu",
            "--json",
        ],
    )
    assert code == 0
    data = json.loads(result.out)["data"]
    assert data["home"] == "/home/p/.codex"
    assert data["helper"] == "/home/p/.local/venv/bin/python"
    assert data["version"] == "1.2.3" and data["machine"] == "wsl:Ubuntu"


def test_show_displays_body_with_controls_inert(monkeypatch, capsys):
    global fake
    fake = FakeBridge()
    code, result = _run_main(monkeypatch, capsys, ["show", "local:abc"])
    assert code == 0
    assert "[bold]body\\x1b[2J" in result.out
    assert "\x1b" not in result.out


def test_list_text_names_the_assistant_counts_results_and_keeps_controls_inert(monkeypatch, capsys):
    global fake
    fake = FakeBridge()
    fake.refs.append(SessionRef(profile_id="local", native_id="def", tool="claude", title="  "))
    monkeypatch.setenv("COLUMNS", "200")
    code, result = _run_main(monkeypatch, capsys, ["list"])
    assert code == 0, result
    assert result.out.splitlines()[0].split() == ["Título", "Asistente", "Actualizada", "ID"]
    assert "Codex" in result.out and "Claude Code" in result.out and "Sin título" in result.out
    assert "\x1b" not in result.out and "A [bold]title\\x1b[31m" in result.out
    assert "2 conversaciones en" in result.err and "--all-projects" in result.err


def test_narrow_terminal_list_keeps_every_title_and_the_full_id(monkeypatch, capsys):
    global fake
    fake = FakeBridge()
    fake.refs = [
        SessionRef(
            profile_id="claude-win",
            native_id="fafa7ad4-f28c-4de5-abfc-dca9704acacb",
            tool="claude",
            title="Ruta inválida en sessionbridge",
        )
    ]
    monkeypatch.setenv("COLUMNS", "60")
    code, result = _run_main(monkeypatch, capsys, ["list"])
    assert code == 0
    assert "Título" not in result.out
    assert "Ruta inválida en sessionbridge" in result.out
    assert "claude-win:fafa7ad4-f28c-4de5-abfc-dca9704acacb" in result.out


def test_empty_list_explains_how_to_widen_the_search(monkeypatch, capsys):
    global fake
    fake = FakeBridge()
    fake.refs = []
    code, result = _run_main(monkeypatch, capsys, ["list", "--search", "nada"])
    assert code == 0
    assert "No hay conversaciones que coincidan. Usa --all-projects" in result.out
    code, result = _run_main(monkeypatch, capsys, ["list", "--all-projects", "--search", "nada"])
    assert result.out.strip() == "No hay conversaciones que coincidan."


def test_interactive_home_that_prints_its_own_result_adds_nothing(monkeypatch, capsys):
    global fake
    fake = FakeBridge()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("session_bridge.interactive.run_home", lambda bridge, **options: None)
    code, result = _run_main(monkeypatch, capsys, [])
    assert code == 0 and result.out == ""


def test_non_tty_no_args_returns_json_error(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)
    code, result = _run_main(monkeypatch, capsys, ["--json"])
    assert code == 2
    envelope = json.loads(result.out)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "interactive_tty_required"
    assert result.err == ""


def test_invalid_date_is_a_stable_json_error(monkeypatch, capsys):
    global fake
    fake = FakeBridge()
    code, result = _run_main(monkeypatch, capsys, ["list", "--since", "tomorrow", "--json"])
    assert code == 2
    assert json.loads(result.out)["error"]["code"] == "invalid_date"
    assert fake.calls == []


def test_real_entrypoint_emits_stable_json_for_usage_errors(tmp_path):
    name = "sessionbridge.exe" if os.name == "nt" else "sessionbridge"
    executable = Path(sysconfig.get_path("scripts")) / name
    env = os.environ.copy()
    env.update(
        TEMP=str(tmp_path),
        TMP=str(tmp_path),
        SESSIONBRIDGE_HOME=str(tmp_path / "state"),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    for args, code in (
        (["unknown", "--json"], 2),
        (["convert", "local:abc", "--json"], 2),
        (["list", "--since", "tomorrow", "--json"], 2),
    ):
        result = subprocess.run(
            [str(executable), *args], capture_output=True, text=True, env=env, check=False
        )
        assert result.returncode == code
        assert result.stderr == ""
        envelope = json.loads(result.stdout)
        assert envelope["schema_version"] == 1 and envelope["ok"] is False


def test_transfers_never_cut_the_transfer_id(monkeypatch, capsys):
    from session_bridge.models import Fidelity, Transfer

    global fake
    fake = FakeBridge()
    identifier = "tr_" + "a" * 32
    fake.transfers = lambda: [
        Transfer(
            id=identifier,
            identity="i",
            state="completed",
            mode="native",
            source_key="local:abc",
            target_profile="codex-win",
            cwd="C:/Projects/app",
            created_at="2026-10-20T10:00:00Z",
            updated_at="2026-10-20T10:00:00Z",
            archive="C:/a.zip",
            fidelity=Fidelity(),
        )
    ]
    for width in ("70", "200"):
        monkeypatch.setenv("COLUMNS", width)
        code, result = _run_main(monkeypatch, capsys, ["transfers"])
        assert code == 0 and identifier in result.out, (width, result.out)
