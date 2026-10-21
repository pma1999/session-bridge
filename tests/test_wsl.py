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


# The helper in WSL and the CLI in Windows may be different versions and speak different languages --

WSL_PROFILE = Profile(
    id="p", tool="codex", environment="wsl", home="/home/me", distro="Ubuntu", helper="/usr/bin/python3"
)


def _helper_answers(monkeypatch, response):
    """Make the helper answer ``response``; returns the environment each call was given."""
    environments = []
    monkeypatch.setattr(wsl, "_wsl_exe", lambda: "wsl.exe")
    monkeypatch.setattr(
        wsl,
        "status",
        lambda: {"available": True, "distributions": [{"name": "Ubuntu", "running": True, "version": "2"}]},
    )

    def run(argv, **kwargs):
        environments.append(kwargs["env"])
        body = json.dumps({"protocol_version": 1, **response}, ensure_ascii=False)
        return SimpleNamespace(returncode=0, stdout=body.encode("utf-8"), stderr=b"")

    monkeypatch.setattr(wsl.subprocess, "run", run)
    return environments


def test_the_helper_is_told_the_language_through_wslenv(monkeypatch, lang):
    lang("en")
    monkeypatch.setenv("WSLENV", "USERPROFILE/p")
    environments = _helper_answers(monkeypatch, {"ok": True, "data": None})
    wsl.call(WSL_PROFILE, "discover", {})
    assert environments[0]["SESSIONBRIDGE_LANG"] == "en"
    assert environments[0]["WSLENV"] == "USERPROFILE/p:SESSIONBRIDGE_LANG/u"
    monkeypatch.delenv("WSLENV")
    wsl.call(WSL_PROFILE, "discover", {})
    assert environments[1]["WSLENV"] == "SESSIONBRIDGE_LANG/u"


def test_an_older_helper_that_answers_in_spanish_reads_in_english(monkeypatch, lang):
    lang("en")
    # Before 0.7 the helper sent no key, and its message was the Spanish text of the catalog.
    error = {"code": "native_unsupported", "message": "Versión nativa no verificada; selecciona explícitamente el modo handoff.", "details": {"tool": "codex"}}  # noqa: E501
    _helper_answers(monkeypatch, {"ok": False, "error": error})
    with pytest.raises(BridgeError) as raised:
        wsl.call(WSL_PROFILE, "discover", {})
    assert raised.value.code == "native_unsupported" and raised.value.details == {"tool": "codex"}
    assert raised.value.message == "This native version is not verified; choose handoff mode explicitly."


def test_a_helper_key_is_rendered_here_and_an_unknown_one_keeps_its_text(monkeypatch, lang):
    lang("en")
    error = {"code": "cwd_missing", "key": "error.cwd_missing.target", "message": "La carpeta de destino no existe."}
    _helper_answers(monkeypatch, {"ok": False, "error": error})
    with pytest.raises(BridgeError) as raised:
        wsl.call(WSL_PROFILE, "discover", {})
    assert raised.value.message == "The target folder does not exist."
    # A newer helper may have a key this version lacks: its own wording is shown, not the bare key.
    newer = {"code": "cwd_missing", "key": "error.cwd_missing.from_the_future", "message": "Folder gone."}
    _helper_answers(monkeypatch, {"ok": False, "error": newer})
    with pytest.raises(BridgeError) as raised:
        wsl.call(WSL_PROFILE, "discover", {})
    assert raised.value.message == "Folder gone." and raised.value.code == "cwd_missing"


def test_a_helper_error_without_a_valid_code_or_text_still_reads_as_an_error(monkeypatch, lang):
    lang("en")
    _helper_answers(monkeypatch, {"ok": False, "error": {"code": "Not A Code!", "message": ""}})
    with pytest.raises(BridgeError) as raised:
        wsl.call(WSL_PROFILE, "discover", {})
    assert raised.value.code == "wsl_helper_error"
    assert raised.value.message == wsl.i18n.t("error.wsl_helper_error")


@pytest.mark.parametrize(("sent", "expected"), [(None, "es"), ("en", "en"), ("es", "es"), ("fr", "es")])
def test_the_helper_writes_in_the_language_it_is_sent_and_spanish_otherwise(monkeypatch, capsys, sent, expected):
    class FakeStdin:
        buffer = SimpleNamespace(read=lambda size: b'{"protocol_version":1,"op":"not-allowed","args":{}}')

    if sent is None:
        monkeypatch.delenv("SESSIONBRIDGE_LANG", raising=False)
    else:
        monkeypatch.setenv("SESSIONBRIDGE_LANG", sent)
    monkeypatch.setattr(helper.sys, "stdin", FakeStdin())
    helper.main()
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["key"] == "error.unsupported_helper_operation"
    assert error["message"] == wsl.i18n.t("error.unsupported_helper_operation", lang=expected)


def test_an_older_helper_text_with_a_value_reads_in_english(monkeypatch, lang):
    lang("en")
    # 0.6 wrote the limit into the sentence and also sent it in the details.
    sent = (
        "Esta versión de OpenCode admite como máximo 9362 mensajes por importación nativa. "
        "Conserva el archivo completo y selecciona handoff o un punto de contexto explícito; "
        "no se ha truncado nada."
    )
    details = {"max_messages": 9362, "message_count": 12000}
    _helper_answers(monkeypatch, {"ok": False, "error": {"code": "native_limit", "message": sent, "details": details}})
    with pytest.raises(BridgeError) as raised:
        wsl.call(WSL_PROFILE, "discover", {})
    assert raised.value.key == "error.native_limit.opencode_messages"
    assert raised.value.message.startswith("This OpenCode version accepts at most 9362 messages")
