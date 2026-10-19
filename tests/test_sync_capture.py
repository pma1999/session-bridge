from copy import deepcopy

import pytest

from session_bridge.errors import BridgeError
from session_bridge.models import Conversation, Event, SessionRef
from session_bridge.projection import project
from session_bridge.sync_capture import checkpoint


def claude_capture(*, pending=True):
    records = [
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "question"}},
        {
            "type": "assistant",
            "uuid": "a1",
            "message": {"role": "assistant", "content": "answer", "stop_reason": "end_turn"},
        },
    ]
    if pending:
        records += [
            {"type": "user", "uuid": "u2", "message": {"role": "user", "content": "next"}},
            {"type": "assistant", "uuid": "a2", "message": {"role": "assistant", "content": "partial"}},
        ]
    return Conversation(
        source=SessionRef(profile_id="claude-test", native_id="one", tool="claude"),
        events=[
            Event(
                id=r["uuid"],
                role=r["message"]["role"],
                kind="message",
                text=r["message"]["content"],
                native=r,
            )
            for r in records
        ],
        raw_records=deepcopy(records),
        source_fingerprint="native-capture",
    )


def test_capture_only_completed_prefix_preserves_full_raw_archive_and_source():
    original = claude_capture()
    before = original.model_dump()
    result = checkpoint(original)
    assert [m.text for m in project(result.conversation).messages] == ["question", "answer"]
    assert result.pending
    assert result.conversation.raw_records == before["raw_records"]
    assert len(result.conversation.events) == 4
    assert original.model_dump() == before


def test_no_completion_evidence_never_becomes_completed_after_quiet_poll():
    original = claude_capture()
    original.events = original.events[2:]
    original.raw_records = original.raw_records[2:]
    for _ in range(2):
        with pytest.raises(BridgeError) as error:
            checkpoint(original)
        assert error.value.code == "sync_pending_turn"


def test_completed_turn_is_transferable_even_when_client_activity_is_unknown():
    result = checkpoint(claude_capture(pending=False))
    assert not result.pending
    assert [m.text for m in project(result.conversation).messages] == ["question", "answer"]


def test_sync_root_command_is_registered():
    from typer.testing import CliRunner

    from session_bridge.cli import app

    result = CliRunner().invoke(app, ["sync", "--help"])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("tool", ["opencode", "codex"])
def test_other_native_completion_markers_bound_pending_suffix(tool):
    c = claude_capture()
    c.source.tool = tool
    c.raw_records = []
    for index, event in enumerate(c.events):
        if tool == "opencode":
            event.native = {"data": {"time": {"completed": 12} if index == 1 else {}}}
        else:
            event.native = {"type": "response_item", "payload": {"type": "message", "id": event.id}}
        c.raw_records.append(event.native)
        if tool == "codex" and index == 1:
            c.raw_records.append({"type": "event_msg", "payload": {"type": "task_complete"}})
            c.raw_records.append({"type": "event_msg", "payload": {"type": "task_started"}})
    result = checkpoint(c)
    assert [m.text for m in project(result.conversation).messages] == ["question", "answer"]
    assert result.pending


@pytest.mark.parametrize("warning", ["source_changed_during_snapshot", "source_truncated_or_invalid_jsonl:5"])
def test_unstable_capture_keeps_the_last_valid_revision(warning):
    c = claude_capture(pending=False)
    c.warnings.append(warning)
    with pytest.raises(BridgeError) as error:
        checkpoint(c)
    assert error.value.code == "sync_unstable_source"


def test_malformed_future_native_fields_are_not_completion_evidence():
    c = claude_capture(pending=False)
    for event in c.events:
        event.native["message"] = ["future shape"]
    with pytest.raises(BridgeError) as error:
        checkpoint(c)
    assert error.value.code == "sync_pending_turn"
