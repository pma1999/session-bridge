import json
import sqlite3
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from session_bridge.errors import BridgeError
from session_bridge.models import Profile, ProjectedMessage, WriteRequest
from session_bridge.writers import claude, codex, common, opencode, registry


@pytest.fixture
def verified_windows_platform(monkeypatch):
    """Exercise the verified Windows gate without changing the host filesystem API."""
    monkeypatch.setattr(registry, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(registry, "platform", SimpleNamespace(machine=lambda: "AMD64"))


def request(tmp_path, tool="claude", count=4):
    profile = Profile(
        id=tool, tool=tool, home=str(tmp_path / tool), executable="client", version=registry.VERSIONS[tool]
    )
    return WriteRequest(
        operation_id=str(uuid.uuid4()),
        profile=profile,
        native_id=registry.allocate_id(profile),
        cwd=str(tmp_path),
        title="Importación",
        messages=[
            ProjectedMessage(role="user" if i % 2 == 0 else "assistant", text=f"Texto {i} áé 中文\nline 2")
            for i in range(count)
        ],
    )


def test_claude_roundtrip_preserves_unicode_all_messages_and_parent_graph(tmp_path):
    req = request(tmp_path)
    receipt = claude.write(req)
    assert claude.messages(req.profile, req.native_id) == [(m.role, m.text) for m in req.messages]
    assert registry.verify(req.profile, receipt, req.messages).evidence["verified_messages"] == 4
    rows = claude.read_rows(req.profile, req.native_id)[1][:-1]
    assert rows[0]["parentUuid"] is None
    assert [r["parentUuid"] for r in rows[1:]] == [r["uuid"] for r in rows[:-1]]
    assert all(r["sessionId"] == req.native_id for r in rows)


def test_claude_exclusive_write_and_changed_subtree_refuse_undo(tmp_path):
    req = request(tmp_path)
    receipt = claude.write(req)
    with pytest.raises(BridgeError, match="ya existe"):
        claude.write(req)
    subtree = Path(receipt.locator).with_suffix("") / "subagents"
    subtree.mkdir(parents=True)
    (subtree / "foreign.jsonl").write_text("foreign", encoding="utf-8")
    with pytest.raises(BridgeError) as exc:
        registry.undo(req.profile, receipt)
    assert exc.value.code == "undo_changed"
    assert Path(receipt.locator).exists()


def test_claude_interrupted_publish_never_exposes_partial_transcript(tmp_path, monkeypatch):
    from session_bridge import archive

    req = request(tmp_path)
    monkeypatch.setattr(archive.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError):
        claude.write(req)
    assert claude.locate(req.profile, req.native_id) is None
    assert not list(Path(req.profile.home).rglob(".sbridge-*"))


def test_claude_long_project_encoding_refuses_before_any_write(tmp_path):
    req = request(tmp_path)
    req.cwd = "C:/" + "x" * 220
    with pytest.raises(BridgeError) as exc:
        claude.write(req)
    assert exc.value.code == "native_path_unsupported"
    assert not Path(req.profile.home).exists()


def test_unchanged_native_undo_refuses_unknown_activity(tmp_path):
    req = request(tmp_path)
    receipt = claude.write(req)
    with pytest.raises(BridgeError) as exc:
        registry.undo(req.profile, receipt)
    assert exc.value.code == "undo_manual_required"
    assert Path(receipt.locator).exists()


def test_verify_does_not_accept_truncated_readback(tmp_path):
    req = request(tmp_path)
    receipt = claude.write(req)
    with pytest.raises(BridgeError) as exc:
        registry.verify(req.profile, receipt, req.messages[:-1])
    assert exc.value.code == "native_verification_failed"


@pytest.mark.parametrize("tool", ["claude", "opencode", "codex"])
def test_native_unknown_version_never_writes_or_silently_falls_back(tmp_path, tool):
    req = request(tmp_path, tool)
    req.profile.version = "unknown"
    with pytest.raises(BridgeError) as exc:
        registry.write(req)
    assert exc.value.code == "native_unsupported"
    assert not Path(req.profile.home).exists()


def test_declared_verified_claude_version_cannot_bypass_actual_binary_check(
    tmp_path, monkeypatch, verified_windows_platform
):
    req = request(tmp_path)
    monkeypatch.setattr(common, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, "2.1.999", ""))
    with pytest.raises(BridgeError) as exc:
        registry.write(req)
    assert exc.value.code == "native_version_changed"
    assert not Path(req.profile.home).exists()


def test_imported_opencode_data_contains_no_foreign_authority(tmp_path):
    req = request(tmp_path, "opencode")
    data = opencode.transfer_data(req)
    assert set(data) == {"info", "messages"}
    assert "permissions" not in data["info"]
    assert "parentID" not in data["info"]
    assert all("parts" not in m and "providerState" not in m for m in data["messages"])
    assistant = data["messages"][1]
    assert assistant["time"]["completed"] >= assistant["time"]["created"]
    assert assistant["content"] == [{"type": "text", "text": req.messages[1].text}]


def test_opencode_10000_message_encoder_is_complete_not_truncated(tmp_path):
    req = request(tmp_path, "opencode", count=10_000)
    data = opencode.transfer_data(req)
    assert len(data["messages"]) == 10_000
    assert len({m["id"] for m in data["messages"]}) == 10_000
    assert data["messages"][-1]["content"][0]["text"] == req.messages[-1].text


def test_observed_native_opencode_limit_refuses_before_launch_or_write(
    tmp_path, monkeypatch, verified_windows_platform
):
    req = request(tmp_path, "opencode", count=9363)
    monkeypatch.setattr(common, "run", lambda *a, **k: pytest.fail("oversized import launched client"))
    assert registry.capability(req.profile).max_messages == 9362
    with pytest.raises(BridgeError) as exc:
        registry.write(req)
    assert exc.value.code == "native_limit"
    assert exc.value.details == {"max_messages": 9362, "message_count": 9363}
    assert not Path(req.profile.home).exists()


def test_opencode_zero_exit_with_api_error_is_not_success(tmp_path, monkeypatch):
    req = request(tmp_path, "opencode")
    monkeypatch.setattr(
        opencode, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, "API Error: 409", "")
    )
    with pytest.raises(BridgeError) as exc:
        opencode.write(req)
    assert exc.value.code == "native_read_failed"


def test_opencode_preexisting_other_operation_is_not_owned(tmp_path, monkeypatch):
    req = request(tmp_path, "opencode")
    foreign = opencode.transfer_data(req)
    for item in foreign["messages"]:
        item["metadata"]["session_bridge"]["operation_id"] = "other"
    receipt = opencode.receipt_from_export(req.profile, foreign)
    monkeypatch.setattr(
        opencode, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, json.dumps(foreign), "")
    )
    monkeypatch.setattr(opencode, "inspect", lambda *a, **k: receipt)
    with pytest.raises(BridgeError) as exc:
        opencode.write(req)
    assert exc.value.code == "native_conflict"


def test_opencode_readonly_inspection_captures_descendants_and_never_launches_client(tmp_path, monkeypatch):
    req = request(tmp_path, "opencode")
    data = opencode.transfer_data(req)
    home = Path(req.profile.home)
    home.mkdir()
    path = home / "opencode.db"
    with sqlite3.connect(path) as con:
        con.execute(
            "CREATE TABLE session_v2(id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, time_suspended INTEGER)"
        )
        con.execute(
            "CREATE TABLE session_message(id TEXT, session_id TEXT, type TEXT, seq INTEGER, data TEXT)"
        )
        con.execute("INSERT INTO session_v2 VALUES(?,NULL,?,NULL)", (req.native_id, req.cwd))
        for seq, item in enumerate(data["messages"]):
            con.execute(
                "INSERT INTO session_message VALUES(?,?,?,?,?)",
                (item["id"], req.native_id, item["type"], seq, json.dumps(item)),
            )
        con.execute("INSERT INTO session_v2 VALUES('ses_foreign',?,?,1)", (req.native_id, req.cwd))
    before = path.read_bytes()
    monkeypatch.setattr(opencode, "run", lambda *a, **k: pytest.fail("inspect launched a native client"))
    receipt, observed = opencode.readback(req.profile, req.native_id)
    assert observed == [(m.role, m.text) for m in req.messages]
    assert receipt.evidence["descendants"] == 1
    assert receipt.evidence["activity"] == "active"
    assert receipt.created_ids == sorted([req.native_id, "ses_foreign"])
    assert path.read_bytes() == before
    assert "readback" not in receipt.evidence


def test_resume_requires_existing_exact_native_target(tmp_path):
    req = request(tmp_path)
    with pytest.raises(BridgeError) as exc:
        registry.resume_argv(req.profile, req.native_id, req.cwd)
    assert exc.value.code == "native_missing"
    claude.write(req)
    assert registry.resume_argv(req.profile, req.native_id, req.cwd) == ["client", "--resume", req.native_id]


def test_path_like_native_id_rejected_before_io(tmp_path):
    req = request(tmp_path)
    with pytest.raises(BridgeError) as exc:
        registry.inspect(req.profile, "../../other")
    assert exc.value.code == "invalid_native_id"


def test_codex_responses_items_do_not_inject_system_authority(tmp_path):
    req = request(tmp_path, "codex")
    items = codex.response_items(req.messages)
    assert [i["role"] for i in items] == [m.role for m in req.messages]
    assert items[0]["content"][0]["type"] == "input_text"
    assert items[1]["content"][0]["type"] == "output_text"


def test_codex_native_history_preserves_leading_summary_roles_and_exact_text(
    tmp_path, monkeypatch, verified_windows_platform
):
    req = request(tmp_path, "codex")
    req.messages = [
        ProjectedMessage(role="assistant", text="Original summary: España 🚀"),
        ProjectedMessage(role="user", text="<user_query>  preserve exact wrapper  </user_query>"),
        ProjectedMessage(role="user", text="next user"),
        ProjectedMessage(role="assistant", text="   "),
    ]
    expected = [(m.role, m.text) for m in req.messages]
    monkeypatch.setattr(
        common, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, "codex-cli 0.159.0", "")
    )
    monkeypatch.setattr(codex, "native_history", lambda *a, **k: expected, raising=False)
    receipt = registry.write(req)
    assert receipt.native_id == req.native_id
    assert codex.messages(req.profile, req.native_id) == expected
    rows = [json.loads(line) for line in Path(receipt.locator).read_text(encoding="utf-8").splitlines()]
    assert receipt.evidence["owned"] and receipt.evidence["operation_ids"] == [req.operation_id]
    assert all(r["type"] != "turn_context" for r in rows)
    assert all(r.get("payload", {}).get("role") not in {"system", "developer"} for r in rows)
    assert len([r for r in rows if r.get("payload", {}).get("type") == "task_complete"]) == len(expected)
    before = Path(receipt.locator).read_bytes()
    with pytest.raises(BridgeError) as exc:
        registry.write(req)
    assert exc.value.code == "native_conflict"
    assert Path(receipt.locator).read_bytes() == before
    assert registry.resume_argv(req.profile, req.native_id, req.cwd) == [
        "client",
        "--cd",
        req.cwd,
        "resume",
        req.native_id,
    ]


def test_codex_native_ui_mismatch_leaves_recoverable_complete_file(
    tmp_path, monkeypatch, verified_windows_platform
):
    req = request(tmp_path, "codex")
    monkeypatch.setattr(
        common, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, "codex-cli 0.159.0", "")
    )
    monkeypatch.setattr(codex, "native_history", lambda *a, **k: [], raising=False)
    with pytest.raises(BridgeError) as exc:
        registry.write(req)
    assert exc.value.code == "native_verification_failed"
    receipt = codex.inspect(req.profile, req.native_id)
    assert receipt.evidence["operation_ids"] == [req.operation_id]
    assert codex.messages(req.profile, req.native_id) == [(m.role, m.text) for m in req.messages]


def test_codex_native_interrupted_publish_has_no_partial_history(
    tmp_path, monkeypatch, verified_windows_platform
):
    from session_bridge import archive

    req = request(tmp_path, "codex")
    monkeypatch.setattr(
        common, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, "codex-cli 0.159.0", "")
    )
    monkeypatch.setattr(archive.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError):
        registry.write(req)
    assert codex.inspect(req.profile, req.native_id) is None
    assert not list(Path(req.profile.home).rglob(".sbridge-*"))


def test_codex_verify_requires_native_presentation_and_detects_later_changes(
    tmp_path, monkeypatch, verified_windows_platform
):
    req = request(tmp_path, "codex")
    expected = [(m.role, m.text) for m in req.messages]
    monkeypatch.setattr(
        common, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, "codex-cli 0.159.0", "")
    )
    monkeypatch.setattr(codex, "native_history", lambda *a, **k: expected, raising=False)
    receipt = registry.write(req)
    monkeypatch.setattr(codex, "native_history", lambda *a, **k: expected[:-1])
    with pytest.raises(BridgeError) as exc:
        registry.verify(req.profile, receipt, req.messages)
    assert exc.value.code == "native_verification_failed"
    with Path(receipt.locator).open("ab") as stream:
        stream.write(b'{"type":"event_msg","payload":{"type":"agent_message","message":"continued"}}\n')
    with pytest.raises(BridgeError) as exc:
        registry.undo(req.profile, receipt)
    assert exc.value.code == "undo_changed"


def test_codex_rpc_declines_actions_and_keeps_errors_content_free(tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace

    req = request(tmp_path, "codex")
    incoming = [
        {"id": "server-action", "method": "command/exec", "params": {"command": "NEVER RUN"}},
        {"id": 1, "result": {}},
        {"id": 2, "error": {"code": -1, "message": "PRIVATE_BODY_MUST_NOT_ESCAPE"}},
    ]
    stdin = io.BytesIO()
    process = SimpleNamespace(
        stdin=stdin,
        stdout=io.BytesIO(b"".join((json.dumps(v) + "\n").encode() for v in incoming)),
        wait=lambda **k: 0,
    )
    seen = {}

    def popen(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return process

    monkeypatch.setattr(codex.subprocess, "Popen", popen)
    monkeypatch.setenv("CODEX_SQLITE_HOME", "wrong-profile")
    rpc = codex._AppServer(req.profile, req.cwd)
    with pytest.raises(BridgeError) as exc:
        rpc.call("thread/read", {"threadId": req.native_id})
    assert "PRIVATE_BODY" not in str(exc.value) and "PRIVATE_BODY" not in str(exc.value.details)
    sent = [json.loads(line) for line in stdin.getvalue().splitlines()]
    assert any(value.get("id") == "server-action" and "error" in value for value in sent)
    assert not any(value.get("method") == "command/exec" for value in sent)
    assert seen["env"]["CODEX_HOME"] == req.profile.home
    assert seen["env"]["CODEX_SQLITE_HOME"] == req.profile.home
    rpc.close()


def test_codex_rpc_cannot_start_a_model_turn(tmp_path, monkeypatch):
    import io
    import queue
    from types import SimpleNamespace

    rpc = codex._AppServer.__new__(codex._AppServer)
    rpc.process = SimpleNamespace(stdin=io.BytesIO())
    rpc.sequence, rpc.failed, rpc.output = 0, False, queue.Queue()
    with pytest.raises(BridgeError) as exc:
        rpc.call("turn/start", {}, timeout=0.001)
    assert exc.value.code == "native_rpc_method"
    assert rpc.process.stdin.getvalue() == b""


def test_codex_corrupt_compressed_history_is_a_diagnostic_error(tmp_path):
    req = request(tmp_path, "codex")
    path = Path(req.profile.home) / "sessions" / (req.native_id + ".jsonl.zst")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"invalid zstd")
    with pytest.raises(BridgeError) as exc:
        codex.inspect(req.profile, req.native_id)
    assert exc.value.code == "native_corrupt"


@pytest.mark.parametrize(
    "turn",
    [None, {"items": [None]}, {"items": [{"type": "userMessage", "content": [None]}]}, {"items": False}],
)
def test_codex_malformed_native_history_is_rejected_as_verification_failure(tmp_path, monkeypatch, turn):
    from types import SimpleNamespace

    req = request(tmp_path, "codex")
    rpc = SimpleNamespace(
        call=lambda *a, **k: {"thread": {"id": req.native_id, "turns": [turn]}}, close=lambda: None
    )
    monkeypatch.setattr(codex, "_AppServer", lambda *a: rpc)
    with pytest.raises(BridgeError) as exc:
        codex.native_history(req.profile, req.native_id, req.cwd)
    assert exc.value.code == "native_verification_failed"


def test_codex_growing_rollout_cannot_bypass_read_limit(tmp_path, monkeypatch):
    import os

    req = request(tmp_path, "codex")
    path = Path(req.profile.home) / "sessions" / (req.native_id + ".jsonl")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"id": req.native_id, "cwd": req.cwd, "padding": "x" * 2048}}
        )
        + "\n",
        encoding="utf-8",
    )
    original_stat = Path.stat

    def stale_stat(candidate, *args, **kwargs):
        value = original_stat(candidate, *args, **kwargs)
        if candidate == path and kwargs.get("follow_symlinks", True):
            fields = list(value)
            fields[6] = 1
            return os.stat_result(fields)
        return value

    monkeypatch.setattr(Path, "stat", stale_stat)
    monkeypatch.setattr(codex, "MAX_NATIVE_BYTES", 256)
    with pytest.raises(BridgeError) as exc:
        codex.inspect(req.profile, req.native_id)
    assert exc.value.code == "native_limit"


@pytest.mark.parametrize("tool", ["claude", "opencode", "codex"])
@pytest.mark.parametrize(
    "system,machine,native",
    [
        ("win32", "AMD64", True),
        ("win32", "ARM64", False),
        ("linux", "x86_64", False),
        ("darwin", "arm64", False),
    ],
)
def test_native_platform_gate_refuses_unverified_platforms_before_client_or_write(
    tmp_path, monkeypatch, tool, system, machine, native
):
    req = request(tmp_path, tool)
    monkeypatch.setattr(registry, "sys", SimpleNamespace(platform=system))
    monkeypatch.setattr(registry, "platform", SimpleNamespace(machine=lambda: machine))
    assert registry.capability(req.profile).native is native
    if not native:
        monkeypatch.setattr(common, "run", lambda *a, **k: pytest.fail("unverified client launched"))
        with pytest.raises(BridgeError) as error:
            registry.write(req)
        assert error.value.code == "native_unsupported"
        assert not Path(req.profile.home).exists()
