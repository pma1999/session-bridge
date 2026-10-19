from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from session_bridge import helper, wsl
from session_bridge.errors import BridgeError
from session_bridge.models import Profile, ProjectedMessage, WriteRequest


def test_wsl_status_decodes_utf16_and_does_not_start(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        raw = (
            "  NAME                   STATE           VERSION\r\n"
            "* Ubuntu Custom          En ejecución    2\r\n"
            "  Debian                 Running         2\r\n"
            "  docker-desktop         Running         2\r\n"
        ).encode("utf-16-le")
        return SimpleNamespace(returncode=0, stdout=raw, stderr=b"")

    monkeypatch.setattr(wsl.shutil, "which", lambda name: "wsl.exe")
    monkeypatch.setattr(wsl.subprocess, "run", run)
    result = wsl.status()
    assert result == {
        "available": True,
        "distributions": [
            {"name": "Ubuntu Custom", "running": True, "version": "2"},
            {"name": "Debian", "running": True, "version": "2"},
        ],
    }
    assert calls == [["wsl.exe", "--list", "--verbose"]]


def test_call_refuses_stopped_distro_before_helper_execution(monkeypatch):
    calls = []
    monkeypatch.setattr(wsl, "_wsl_exe", lambda: "wsl.exe")
    monkeypatch.setattr(
        wsl,
        "status",
        lambda: {"available": True, "distributions": [{"name": "Ubuntu", "running": False, "version": "2"}]},
    )
    monkeypatch.setattr(wsl.subprocess, "run", lambda *a, **k: calls.append(a))
    profile = Profile(
        id="p",
        tool="codex",
        environment="wsl",
        home="/home/me",
        distro="Ubuntu",
        helper="/home/me/.local/share/session-bridge/venv/bin/python",
    )
    with pytest.raises(BridgeError) as error:
        wsl.call(profile, "discover", {"profile": profile.model_dump(mode="json")})
    assert error.value.code == "wsl_distro_stopped"
    assert calls == []


def test_call_uses_fixed_exec_protocol_and_returns_json(monkeypatch):
    calls = []
    monkeypatch.setattr(wsl, "_wsl_exe", lambda: "wsl.exe")
    monkeypatch.setattr(
        wsl,
        "status",
        lambda: {"available": True, "distributions": [{"name": "Ubuntu", "running": True, "version": "2"}]},
    )

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        request = json.loads(kwargs["input"])
        assert request["protocol_version"] == 1
        return SimpleNamespace(
            returncode=0, stdout=b'{"protocol_version":1,"ok":true,"data":["ok"]}', stderr=b""
        )

    monkeypatch.setattr(wsl.subprocess, "run", run)
    profile = Profile(
        id="p",
        tool="codex",
        environment="wsl",
        home="/home/me",
        distro="Ubuntu",
        helper="/home/me/.local/share/session-bridge/venv/bin/python",
    )
    assert wsl.call(profile, "discover", {"profile": profile.model_dump(mode="json")}) == ["ok"]
    assert calls[0][0] == [
        "wsl.exe",
        "--distribution",
        "Ubuntu",
        "--exec",
        profile.helper,
        "-m",
        "session_bridge.helper",
    ]
    assert "shell" not in calls[0][1]
    assert json.loads(calls[0][1]["input"])["args"]["profile"] == profile.model_dump(mode="json")


def test_call_rejects_invalid_distro_and_unknown_operation_before_process(monkeypatch):
    calls = []
    monkeypatch.setattr(wsl, "_wsl_exe", lambda: "wsl.exe")
    monkeypatch.setattr(wsl, "status", lambda: {"available": True, "distributions": []})
    monkeypatch.setattr(wsl.subprocess, "run", lambda *a, **k: calls.append(a))
    profile = Profile(
        id="p",
        tool="codex",
        environment="wsl",
        home="/home/me",
        distro="Ubuntu; echo bad",
        helper="/home/me/python",
    )
    with pytest.raises(BridgeError) as error:
        wsl.call(profile, "discover", {})
    assert error.value.code == "invalid_distro"
    assert calls == []
    profile.distro = "Ubuntu"
    with pytest.raises(BridgeError) as error:
        wsl.call(profile, "run", {})
    assert error.value.code == "unsupported_helper_operation"
    assert calls == []


def test_wsl_install_bootstraps_fixed_python_and_returns_detected_metadata(monkeypatch, tmp_path):
    wheel = tmp_path / "session_bridge-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"placeholder")
    calls = []

    class WindowsWheel:
        name, suffix = wheel.name, wheel.suffix

        def resolve(self, *, strict):
            wheel.resolve(strict=strict)
            return self

        def is_file(self):
            return wheel.is_file()

        def __str__(self):
            return f"C:/fixture with spaces/{self.name}"

    monkeypatch.setattr(wsl, "Path", lambda path: WindowsWheel())

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                b'{"helper":"/home/p/.local/share/session-bridge/venv/bin/python",'
                b'"home":"/home/p","install_root":"/home/p/.local/share/session-bridge",'
                b'"launcher":"/home/p/.local/bin/sessionbridge","path_status":"added","on_path":false,'
                b'"profiles":[{"id":"codex-ubuntu","tool":"codex","environment":"wsl",'
                b'"home":"/home/p/.codex","distro":"Ubuntu","helper":"/home/p/.local/share/session-bridge/venv/bin/python"}]}'
            ),
            stderr=b"",
        )

    monkeypatch.setattr(wsl, "_wsl_exe", lambda: "wsl.exe")
    monkeypatch.setattr(wsl.subprocess, "run", run)
    result = wsl.install("Ubuntu", wheel)
    assert result["installed"] is True
    assert result["profiles"][0]["home"] == "/home/p/.codex"
    assert result["path_status"] == "added" and result["on_path"] is False
    argv, kwargs = calls[0]
    assert argv[:6] == ["wsl.exe", "--distribution", "Ubuntu", "--exec", "python3", "-c"]
    assert argv[-1] == "Ubuntu" and argv[-2] == f"/mnt/c/fixture with spaces/{wheel.name}"
    assert "shell" not in kwargs
    compile(argv[6], "wsl-install-bootstrap", "exec")


def test_wsl_call_reports_explicit_size_limit_before_running(monkeypatch):
    monkeypatch.setattr(wsl, "_wsl_exe", lambda: "wsl.exe")
    monkeypatch.setattr(
        wsl,
        "status",
        lambda: {"available": True, "distributions": [{"name": "Ubuntu", "running": True, "version": "2"}]},
    )
    calls = []
    monkeypatch.setattr(wsl.subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(wsl, "MAX_REQUEST_BYTES", 64)
    profile = Profile(
        id="p", tool="codex", environment="wsl", home="/home/me", distro="Ubuntu", helper="/home/me/python"
    )
    with pytest.raises(BridgeError) as error:
        wsl.call(profile, "discover", {"large": "x" * 100})
    assert error.value.code == "request_too_large"
    assert calls == []


def test_wsl_call_rejects_write_for_different_profile(monkeypatch):
    monkeypatch.setattr(wsl, "_wsl_exe", lambda: "wsl.exe")
    monkeypatch.setattr(
        wsl,
        "status",
        lambda: {"available": True, "distributions": [{"name": "Ubuntu", "running": True, "version": "2"}]},
    )
    calls = []
    monkeypatch.setattr(wsl.subprocess, "run", lambda *a, **k: calls.append(a))
    profile = Profile(
        id="p", tool="codex", environment="wsl", home="/home/me", distro="Ubuntu", helper="/home/me/python"
    )
    other = profile.model_copy(update={"id": "other"})
    request = WriteRequest(
        operation_id="tr_1",
        profile=other,
        native_id="n_1",
        cwd="/home/me/project",
        title="title",
        messages=[],
    )
    with pytest.raises(BridgeError) as error:
        wsl.call(profile, "write", {"request": request.model_dump(mode="json")})
    assert error.value.code == "profile_mismatch"
    assert calls == []


def test_wsl_transport_preserves_ten_thousand_messages(monkeypatch):
    monkeypatch.setattr(wsl, "_wsl_exe", lambda: "wsl.exe")
    monkeypatch.setattr(
        wsl,
        "status",
        lambda: {"available": True, "distributions": [{"name": "Ubuntu", "running": True, "version": "2"}]},
    )
    profile = Profile(
        id="p",
        tool="codex",
        environment="wsl",
        home="/home/me",
        distro="Ubuntu",
        helper="/home/me/.local/share/session-bridge/venv/bin/python",
    )
    request = WriteRequest(
        operation_id="tr_1",
        profile=profile,
        native_id="n_1",
        cwd="/home/me/project",
        title="title",
        messages=[ProjectedMessage(role="user", text="context") for _ in range(10_000)],
    )
    observed = {}

    def run(argv, **kwargs):
        observed["argv"] = argv
        packet = json.loads(kwargs["input"])
        observed["count"] = len(packet["args"]["request"]["messages"])
        observed["size"] = len(kwargs["input"])
        return SimpleNamespace(
            returncode=0, stdout=b'{"protocol_version":1,"ok":true,"data":{"native_id":"n_1"}}', stderr=b""
        )

    monkeypatch.setattr(wsl.subprocess, "run", run)
    assert wsl.call(profile, "write", {"request": request.model_dump(mode="json")}) == {"native_id": "n_1"}
    assert observed["count"] == 10_000
    assert observed["size"] > 100_000


def test_helper_dispatch_rejects_arbitrary_and_extra_arguments():
    with pytest.raises(BridgeError) as error:
        helper.dispatch("execute", {"command": "id"})
    assert error.value.code == "unsupported_helper_operation"
    with pytest.raises(BridgeError) as error:
        helper.dispatch("discover", {"profile": {"id": "p", "tool": "codex", "home": "/tmp", "x": "y"}})
    assert error.value.code == "invalid_helper_arguments"


def test_helper_validates_ten_thousand_write_messages(monkeypatch):
    from session_bridge.writers import registry

    profile = Profile(
        id="p",
        tool="codex",
        environment="wsl",
        home="/home/me",
        distro="Ubuntu",
        helper="/home/me/.local/share/session-bridge/venv/bin/python",
    )
    request = WriteRequest(
        operation_id="tr_1",
        profile=profile,
        native_id="n_1",
        cwd="/home/me/project",
        title="title",
        messages=[ProjectedMessage(role="assistant", text="reply") for _ in range(10_000)],
    )
    monkeypatch.setattr(registry, "write", lambda actual: {"received": len(actual.messages)})
    assert helper.dispatch("write", {"request": request.model_dump(mode="json")}) == {"received": 10_000}


def test_helper_main_always_writes_one_json_protocol_line(monkeypatch, capsys):
    class FakeStdin:
        buffer = SimpleNamespace(read=lambda size: b'{"protocol_version":1,"op":"not-allowed","args":{}}')

    monkeypatch.setattr(helper.sys, "stdin", FakeStdin())
    helper.main()
    output = capsys.readouterr()
    assert output.err == ""
    response = json.loads(output.out)
    assert response["protocol_version"] == 1 and response["ok"] is False
    assert response["error"]["code"] == "unsupported_helper_operation"


def test_wsl_install_requires_bridge_wheel_without_starting_distro(monkeypatch, tmp_path):
    wheel = tmp_path / "unrelated-1.0.whl"
    wheel.write_bytes(b"placeholder")
    calls = []
    monkeypatch.setattr(wsl, "_wsl_exe", lambda: "wsl.exe")
    monkeypatch.setattr(wsl.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(BridgeError) as error:
        wsl.install("Ubuntu", wheel)
    assert error.value.code == "invalid_wheel"
    assert calls == []
