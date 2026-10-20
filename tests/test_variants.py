"""Edited messages leave versions behind; subagents reach the parent as a request and its result."""

import json
import sqlite3

import pytest

from session_bridge import history
from session_bridge.errors import BridgeError
from session_bridge.models import Profile, SessionRef
from session_bridge.projection import project
from session_bridge.readers import claude, codex, opencode
from session_bridge.sync_capture import checkpoint


def profile(tmp_path, tool):
    return Profile(id=tool, tool=tool, home=str(tmp_path), version="fixture")


def jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def user(uuid, parent, content, **extra):
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": extra.pop("at", None),
        "message": {"role": "user", "content": content},
        **extra,
    }


def assistant(uuid, parent, text, **extra):
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": extra.pop("at", None),
        "message": {
            "role": "assistant",
            "id": "msg-" + uuid,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
        },
        **extra,
    }


def texts(conversation, **options):
    return [m.text for m in project(conversation, **options).messages if not m.adapted]


def edited_claude(tmp_path, *, pointer=True):
    records = [
        user("a", None, "start the report", at="2026-10-01T10:00:00Z"),
        assistant("b", "a", "which format?", at="2026-10-01T10:00:05Z"),
        user("old", "b", "write it as a table please", at="2026-10-01T10:01:00Z"),
        assistant("old-reply", "old", "here is the table", at="2026-10-01T10:01:10Z"),
        user("new", "b", "write it as a list please", at="2026-10-01T10:05:00Z"),
        assistant("new-reply", "new", "here is the list", at="2026-10-01T10:05:10Z"),
    ]
    if pointer:
        records.append({"type": "last-prompt", "leafUuid": "new"})
    jsonl(tmp_path / ".claude/projects/p/s.jsonl", records)
    p = profile(tmp_path, "claude")
    return p, claude.read(p, claude.discover(p)[0])


def test_claude_edited_prompt_is_a_version_and_the_resumed_one_is_the_default(tmp_path):
    _, conversation = edited_claude(tmp_path)
    assert not conversation.context_ambiguous
    assert texts(conversation) == [
        "start the report",
        "which format?",
        "write it as a list please",
        "here is the list",
    ]
    variants = history.describe(conversation)
    assert [v.current for v in variants] == [True, False]
    assert [v.prompt for v in variants] == ["…it as a list please", "…it as a table please"]
    assert variants[1].updated_at == "2026-10-01T10:01:10Z" and variants[1].messages == 2
    assert texts(conversation, variant=variants[1].id) == [
        "start the report",
        "which format?",
        "write it as a table please",
        "here is the table",
    ]
    # A prefix chooses it too; an unknown one is refused.
    assert texts(conversation, variant=variants[1].id[:6]) == texts(conversation, variant=variants[1].id)
    with pytest.raises(BridgeError) as error:
        project(conversation, variant="v_nothing")
    assert error.value.code == "variant_not_found"


def test_claude_versions_without_a_pointer_default_to_the_newest_and_stay_publishable(tmp_path):
    _, conversation = edited_claude(tmp_path, pointer=False)
    assert not conversation.context_ambiguous
    assert texts(conversation)[-1] == "here is the list"
    assert len(history.describe(conversation)) == 2
    assert not checkpoint(conversation).pending


def test_claude_competing_answers_to_one_prompt_still_need_a_choice(tmp_path):
    jsonl(
        tmp_path / ".claude/projects/p/s.jsonl",
        [
            user("a", None, "question"),
            assistant("b", "a", "first answer"),
            assistant("c", "a", "second answer"),
        ],
    )
    p = profile(tmp_path, "claude")
    assert claude.read(p, claude.discover(p)[0]).context_ambiguous


def test_claude_history_detached_from_the_conversation_is_not_offered_as_a_version(tmp_path):
    p, _ = edited_claude(tmp_path)
    path = tmp_path / ".claude/projects/p/s.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    # A prompt whose parent is not in the file never left this conversation at an edit.
    records.insert(-1, user("orphan", "missing", "orphan prompt", at="2026-10-01T09:00:00Z"))
    records.insert(-1, assistant("orphan-reply", "orphan", "orphan reply", at="2026-10-01T09:00:05Z"))
    jsonl(path, records)
    conversation = claude.read(p, claude.discover(p)[0])
    variants = history.describe(conversation)
    assert len(variants) == 2
    assert all("orphan" not in texts(conversation, variant=v.id)[-1] for v in variants)
    assert variants[0].prompt == "…it as a list please"
    # Without a pointer, a detached tip is a competing history again, as before.
    jsonl(path, [r for r in records if r.get("type") != "last-prompt"])
    assert claude.read(p, claude.discover(p)[0]).context_ambiguous


def test_claude_full_history_is_the_chosen_version_without_other_branches_or_subagent_steps(tmp_path):
    p, _ = edited_claude(tmp_path)
    path = tmp_path / ".claude/projects/p/s.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    # Older clients wrote subagent steps into the parent file as sidechain records.
    records.insert(2, user("side", "b", "subagent step", isSidechain=True))
    records.insert(3, assistant("side-reply", "side", "subagent internals", isSidechain=True))
    jsonl(path, records)
    conversation = claude.read(p, claude.discover(p)[0])
    full = texts(conversation, context="all")
    assert full == ["start the report", "which format?", "write it as a list please", "here is the list"]
    other = history.describe(conversation)[1].id
    assert texts(conversation, context="all", variant=other)[-1] == "here is the table"


def test_claude_edit_sent_before_any_answer_is_a_version(tmp_path):
    p = profile(tmp_path, "claude")
    jsonl(
        tmp_path / ".claude/projects/p/s.jsonl",
        [
            user("first", None, "review the pull request comment", at="2026-10-01T10:00:00Z"),
            {
                "type": "attachment",
                "uuid": "env",
                "parentUuid": "first",
                "attachment": {"type": "environment"},
            },
            user("second", None, "review the pull request comment and reply", at="2026-10-01T10:00:13Z"),
            assistant("reply", "second", "done"),
            {"type": "last-prompt", "leafUuid": "second"},
        ],
    )
    conversation = claude.read(p, claude.discover(p)[0])
    variants = history.describe(conversation)
    assert len(variants) == 2 and variants[1].messages == 1
    assert texts(conversation, variant=variants[1].id) == ["review the pull request comment"]


def test_claude_partial_compaction_keeps_the_summarized_turns_in_the_full_history(tmp_path):
    # A partial compaction rewrites the kept tail after its boundary and names it as the logical
    # parent; the turns it summarized hang beside the kept head, off any chain from the tip.
    p = profile(tmp_path, "claude")
    jsonl(
        tmp_path / ".claude/projects/p/s.jsonl",
        [
            user("u1", None, "first request"),
            assistant("a1", "u1", "first answer"),
            assistant("a2", "a1", "summarized answer"),
            user("u2", "a2", "summarized request"),
            assistant("a3", "u2", "summarized reply"),
            assistant("kept", "a1", "kept answer"),
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "boundary",
                "parentUuid": None,
                "logicalParentUuid": "tail",
                "compactMetadata": {
                    "preservedSegment": {"headUuid": "kept", "anchorUuid": "summary", "tailUuid": "tail"}
                },
            },
            user("summary", "boundary", "summary of the work", isCompactSummary=True),
            user("tail", "summary", "continue"),
            assistant("reply", "tail", "continuing"),
            {"type": "last-prompt", "leafUuid": "tail"},
        ],
    )
    conversation = claude.read(p, claude.discover(p)[0])
    full = texts(conversation, context="all")
    assert "summarized request" in full and "summarized answer" in full and full[-1] == "continuing"
    assert history.describe(conversation) == []


# ---------------------------------------------------------------------------
# History index


def test_history_index_round_trips_through_the_interchange_format_and_detects_damage(tmp_path):
    from session_bridge.models import Conversation

    _, conversation = edited_claude(tmp_path)
    restored = Conversation.model_validate(json.loads(conversation.model_dump_json()))
    assert [v.id for v in history.describe(restored)] == [v.id for v in history.describe(conversation)]
    restored.events[0].id = "changed"
    assert history.describe(restored) == []
    assert history.lineage(restored) == [True] * len(restored.events)
    assert "source_history_index_invalid" in restored.warnings


def test_conversation_without_an_index_projects_as_before(tmp_path):
    _, conversation = edited_claude(tmp_path)
    history.strip(conversation)
    assert history.describe(conversation) == []
    # The full history then keeps every event, as archives written before versions existed did.
    assert "here is the table" in texts(conversation, context="all")
    with pytest.raises(BridgeError) as error:
        project(conversation, variant="v_")
    assert error.value.code == "variant_not_found"


def test_selected_version_keeps_the_original_selectable(tmp_path):
    _, conversation = edited_claude(tmp_path)
    current, other = (v.id for v in history.describe(conversation))
    chosen = history.select(conversation, other)
    assert history.current(chosen) == other and history.original(chosen) == current
    assert [v.current for v in history.describe(chosen)] == [True, False]
    assert history.describe(chosen)[0].id == current
    assert texts(chosen, variant=current)[-1] == "here is the list"


# ---------------------------------------------------------------------------
# Codex


THREAD = "01a14b28-c6f4-7881-b911-c75bc1def0dd"


def message(ordinal, role, text, at=None):
    record = {
        "ordinal": ordinal,
        "type": "response_item",
        "payload": {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]},
    }
    if at:
        record["timestamp"] = at
    return record


def event(ordinal, kind, **payload):
    return {"ordinal": ordinal, "type": "event_msg", "payload": {"type": kind, **payload}}


def edited_codex(tmp_path, *, current_tail=None):
    """The shape Codex 0.159 writes when a prompt is edited: a new rollout keeps the records
    before that turn through its history base, and the old rollout keeps the rest."""
    day = tmp_path / ".codex/sessions/2026/10/17"
    jsonl(
        day / f"rollout-2026-10-17T20-38-46-{THREAD}.jsonl",
        [
            {"ordinal": 0, "type": "session_meta", "payload": {"id": THREAD, "history_mode": "paginated"}},
            event(1, "task_started", turn_id="t1"),
            message(2, "user", "set up the headset", at="2026-10-17T19:00:00Z"),
            message(3, "assistant", "headset ready", at="2026-10-17T19:00:10Z"),
            event(4, "task_complete", turn_id="t1"),
            event(5, "task_started", turn_id="t2"),
            message(6, "user", "prepare it for discord", at="2026-10-17T21:46:42Z"),
            message(7, "assistant", "checking discord", at="2026-10-17T21:46:50Z"),
            event(8, "task_complete", turn_id="t2"),
        ],
    )
    segment = day / f"rollout-2026-10-17T23-46-56-{THREAD}_01a14bd5-0cc4-7bf0-b274-6bdb793fa68f.jsonl"
    jsonl(
        segment,
        [
            {
                "ordinal": 5,
                "type": "session_meta",
                "payload": {"id": THREAD, "history_base": {"thread_id": THREAD, "end_ordinal_exclusive": 5}},
            },
            event(6, "task_started", turn_id="t3"),
            message(7, "user", "prepare it for discord and give me instructions", at="2026-10-17T21:46:59Z"),
            *(
                current_tail
                if current_tail is not None
                else [message(8, "assistant", "here are the steps"), event(9, "task_complete", turn_id="t3")]
            ),
        ],
    )
    p = profile(tmp_path, "codex")
    return p, codex.read(p, SessionRef(profile_id=p.id, native_id=THREAD, tool="codex", locator=str(segment)))


def test_codex_prompt_edit_keeps_the_earlier_rollout_as_a_version(tmp_path):
    _, conversation = edited_codex(tmp_path)
    assert not conversation.context_ambiguous
    assert texts(conversation) == [
        "set up the headset",
        "headset ready",
        "prepare it for discord and give me instructions",
        "here are the steps",
    ]
    current, earlier = history.describe(conversation)
    assert current.current and current.prompt == "…it for discord and give me instructions"
    assert earlier.prompt == "prepare it for discord" and earlier.updated_at == "2026-10-17T21:46:50Z"
    assert texts(conversation, variant=earlier.id) == [
        "set up the headset",
        "headset ready",
        "prepare it for discord",
        "checking discord",
    ]
    # The earlier turns are kept verbatim and marked, so they never look like the live rollout.
    kept = [r for r in conversation.raw_records if r.get("table") == "discarded_rollout"]
    assert [r["row"]["ordinal"] for r in kept] == [5, 6, 7, 8]
    assert texts(conversation, context="all") == texts(conversation)


def test_codex_earlier_rollout_does_not_close_a_turn_still_running(tmp_path):
    _, conversation = edited_codex(tmp_path, current_tail=[message(8, "assistant", "working on it")])
    # The old rollout's completed turn comes later in the capture; the live turn is still open.
    assert checkpoint(conversation).pending


def test_codex_legacy_rollback_drops_the_newest_turns_and_keeps_them_as_a_version(tmp_path):
    rollout = tmp_path / f".codex/sessions/2026/10/01/rollout-2026-10-01T10-00-00-{THREAD}.jsonl"
    jsonl(
        rollout,
        [
            {"ordinal": 0, "type": "session_meta", "payload": {"id": THREAD}},
            message(1, "user", "first request"),
            message(2, "assistant", "first answer"),
            message(3, "user", "<environment_context>\n  <cwd>C:/repo</cwd>\n</environment_context>"),
            message(4, "user", "second request"),
            message(5, "assistant", "second answer"),
            event(6, "thread_rolled_back", num_turns=1),
            message(7, "user", "edited second request"),
            message(8, "assistant", "edited answer"),
        ],
    )
    p = profile(tmp_path, "codex")
    conversation = codex.read(
        p, SessionRef(profile_id=p.id, native_id=THREAD, tool="codex", locator=str(rollout))
    )
    assert not conversation.context_ambiguous
    assert texts(conversation) == ["first request", "first answer", "edited second request", "edited answer"]
    assert texts(conversation, context="all") == texts(conversation)
    earlier = history.describe(conversation)[1]
    assert texts(conversation, variant=earlier.id)[-2:] == ["second request", "second answer"]


def test_codex_rollback_across_a_compaction_needs_a_choice(tmp_path):
    rollout = tmp_path / f".codex/sessions/2026/10/01/rollout-2026-10-01T10-00-00-{THREAD}.jsonl"
    jsonl(
        rollout,
        [
            {"ordinal": 0, "type": "session_meta", "payload": {"id": THREAD}},
            message(1, "user", "first request"),
            message(2, "assistant", "first answer"),
            {"ordinal": 3, "type": "compacted", "payload": {"message": "summary of the first turn"}},
            message(4, "user", "second request"),
            event(5, "thread_rolled_back", num_turns=2),
        ],
    )
    p = profile(tmp_path, "codex")
    conversation = codex.read(
        p, SessionRef(profile_id=p.id, native_id=THREAD, tool="codex", locator=str(rollout))
    )
    assert conversation.context_ambiguous
    assert "source_rollback_across_compaction" in conversation.warnings


# ---------------------------------------------------------------------------
# OpenCode


def opencode_session(tmp_path, revert):
    path = tmp_path / ".local/share/opencode/opencode.db"
    path.parent.mkdir(parents=True)
    c = sqlite3.connect(path)
    c.execute(
        "create table session_v2(id text primary key,title text,directory text,parent_id text,"
        "time_created int,time_updated int,time_archived int,revert text)"
    )
    c.execute(
        "create table session_message(id text,session_id text,type text,seq int,time_created int,"
        "time_updated int,data text)"
    )
    c.execute(
        "insert into session_v2 values(?,?,?,?,?,?,?,?)",
        ("s", "demo", "/repo", None, 1, 2, None, json.dumps(revert) if revert else None),
    )
    for seq, (kind, text) in enumerate(
        [
            ("user", "rename the module"),
            ("assistant", "renamed"),
            ("user", "now delete it"),
            ("assistant", "deleted"),
        ],
        1,
    ):
        c.execute(
            "insert into session_message values(?,?,?,?,?,?,?)",
            ("m" + str(seq), "s", kind, seq, 1000 * seq, 1000 * seq, json.dumps({"text": text})),
        )
    c.commit()
    c.close()
    p = profile(tmp_path, "opencode")
    return opencode.read(p, opencode.discover(p)[0])


def test_opencode_staged_undo_is_honoured_and_what_it_undid_stays_selectable(tmp_path):
    conversation = opencode_session(tmp_path, {"messageID": "m3"})
    assert texts(conversation) == ["rename the module", "renamed"]
    assert texts(conversation, context="all") == ["rename the module", "renamed"]
    current, undone = history.describe(conversation)
    assert current.current and not undone.current
    assert texts(conversation, variant=undone.id) == [
        "rename the module",
        "renamed",
        "now delete it",
        "deleted",
    ]


def test_opencode_without_an_undo_has_one_version(tmp_path):
    conversation = opencode_session(tmp_path, None)
    assert history.describe(conversation) == []
    assert texts(conversation, context="all") == texts(conversation)


# ---------------------------------------------------------------------------
# Subagents: the parent's model saw the request it delegated and the result, never the steps.


def everything(conversation):
    return [
        "\n".join(m.text for m in project(conversation, context=context).messages)
        for context in ("current", "all")
    ]


def test_claude_subagent_reaches_the_parent_as_its_request_and_result(tmp_path):
    call = {"type": "tool_use", "id": "agent-1", "name": "Agent", "input": {"prompt": "look for bugs"}}
    result = {"type": "tool_result", "tool_use_id": "agent-1", "content": "found 2 bugs"}
    jsonl(
        tmp_path / ".claude/projects/p/s.jsonl",
        [
            user("a", None, "audit the parser"),
            {
                "type": "assistant",
                "uuid": "b",
                "parentUuid": "a",
                "message": {"role": "assistant", "id": "m-b", "content": [call]},
            },
            # Older clients wrote the steps inline as sidechain records.
            user("s1", "b", "look for bugs", isSidechain=True),
            assistant("s2", "s1", "internal step of the subagent", isSidechain=True),
            user("c", "b", [result]),
            assistant("d", "c", "the audit is done"),
            {"type": "last-prompt", "leafUuid": "a"},
        ],
    )
    jsonl(
        tmp_path / ".claude/projects/p/s/subagents/agent-1.jsonl",
        [
            user("x", None, "look for bugs", isSidechain=True),
            assistant("y", "x", "child step grep", isSidechain=True),
        ],
    )
    p = profile(tmp_path, "claude")
    conversation = claude.read(p, next(r for r in claude.discover(p) if not r.parent_id))
    assert conversation.related
    for sent in everything(conversation):
        assert "look for bugs" in sent and "found 2 bugs" in sent and sent.endswith("the audit is done")
        assert "internal step" not in sent and "child step" not in sent


def test_codex_subagent_reaches_the_parent_as_its_request_and_result(tmp_path):
    parent, child = THREAD, "01a14b29-0000-7000-8000-000000000001"
    day = tmp_path / ".codex/sessions/2026/10/01"
    jsonl(
        day / f"rollout-2026-10-01T10-00-00-{parent}.jsonl",
        [
            {"ordinal": 0, "type": "session_meta", "payload": {"id": parent}},
            message(1, "user", "audit the parser"),
            {
                "ordinal": 2,
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "call_id": "c1",
                    "arguments": json.dumps({"message": "look for bugs"}),
                },
            },
            {
                "ordinal": 3,
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": "spawned /root/worker",
                },
            },
            {
                "ordinal": 4,
                "type": "response_item",
                "payload": {
                    "type": "agent_message",
                    "author": "/root/worker",
                    "content": [{"type": "input_text", "text": "found 2 bugs"}],
                },
            },
            message(5, "assistant", "the audit is done"),
        ],
    )
    jsonl(
        day / f"rollout-2026-10-01T10-00-01-{child}.jsonl",
        [
            {"ordinal": 0, "type": "session_meta", "payload": {"id": child, "agent_path": "/root/worker"}},
            message(1, "user", "look for bugs"),
            message(2, "assistant", "child step grep"),
        ],
    )
    p = profile(tmp_path, "codex")
    ref = next(r for r in codex.discover(p) if r.native_id == parent)
    conversation = codex.read(p, ref)
    for sent in everything(conversation):
        assert "look for bugs" in sent and "found 2 bugs" in sent and sent.endswith("the audit is done")
        assert "child step" not in sent


def test_opencode_subagent_reaches_the_parent_as_its_request_and_result(tmp_path):
    path = tmp_path / ".local/share/opencode/opencode.db"
    path.parent.mkdir(parents=True)
    c = sqlite3.connect(path)
    c.execute(
        "create table session_v2(id text primary key,title text,directory text,parent_id text,"
        "time_created int,time_updated int,time_archived int,revert text)"
    )
    c.execute(
        "create table session_message(id text,session_id text,type text,seq int,time_created int,"
        "time_updated int,data text)"
    )
    for row in [("s", "demo", "/repo", None), ("child", "worker", "/repo", "s")]:
        c.execute("insert into session_v2 values(?,?,?,?,?,?,?,?)", (*row, 1, 2, None, None))
    block = {
        "type": "tool",
        "id": "call-1",
        "name": "subagent",
        "state": {
            "status": "completed",
            "input": {"agent": "general", "prompt": "look for bugs"},
            "content": [
                {
                    "type": "text",
                    "text": '<subagent sessionID="child" state="completed">\nfound 2 bugs\n</subagent>',
                }
            ],
        },
    }
    rows = [
        ("m1", "s", "user", {"text": "audit the parser"}),
        ("m2", "s", "assistant", {"content": [block]}),
        ("m3", "s", "assistant", {"text": "the audit is done"}),
        ("c1", "child", "user", {"text": "look for bugs"}),
        ("c2", "child", "assistant", {"text": "child step grep"}),
    ]
    for seq, (key, session, kind, data) in enumerate(rows, 1):
        c.execute(
            "insert into session_message values(?,?,?,?,?,?,?)",
            (key, session, kind, seq, seq, seq, json.dumps(data)),
        )
    c.commit()
    c.close()
    p = profile(tmp_path, "opencode")
    conversation = opencode.read(p, next(r for r in opencode.discover(p) if r.native_id == "s"))
    assert [r.native_id for r in conversation.related] == ["child"]
    for sent in everything(conversation):
        assert "look for bugs" in sent and "found 2 bugs" in sent and sent.endswith("the audit is done")
        assert "child step" not in sent


# ---------------------------------------------------------------------------
# Conversion


def test_conversion_carries_the_chosen_version_through_recovery_and_refuses_to_sync_it(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from session_bridge.models import Capability, NativeReceipt
    from session_bridge.service import Bridge
    from session_bridge.sync import SyncService

    _, conversation = edited_claude(tmp_path)
    (tmp_path / "project").mkdir()
    conversation.source.cwd = str(tmp_path / "project")
    target = Profile(id="opencode-test", tool="opencode", home=str(tmp_path / "target"))
    bridge = Bridge(tmp_path / "data")
    written = []
    writer = SimpleNamespace(
        capability=lambda p: Capability(tool=p.tool, version="test", native=True, reason="mock only"),
        allocate_id=lambda p: "new-native-id",
        inspect=lambda p, i: None,
        write=lambda req: (
            written.append([m.text for m in req.messages])
            or NativeReceipt(native_id=req.native_id, profile_id=req.profile.id, cwd=req.cwd, fingerprint="v")
        ),
        verify=lambda p, receipt, messages: receipt,
    )
    monkeypatch.setattr(bridge, "profiles", lambda: [profile(tmp_path, "claude"), target], raising=False)
    monkeypatch.setattr(
        bridge, "show", lambda key, snapshot=False: conversation.model_copy(deep=True), raising=False
    )
    monkeypatch.setattr(bridge, "_writer", lambda p: writer, raising=False)
    current, other = (v.id for v in history.describe(conversation))
    preview = bridge.preview(conversation.source.key, target.id, variant=other[:8])
    assert preview["variant"] == other and [v["id"] for v in preview["variants"]] == [current, other]
    transfer = bridge.convert(conversation.source.key, target.id, variant=other)
    assert transfer.context_policy["variant"] == other
    assert written[-1][-1] == "here is the table"
    # Recovery and verification rebuild the messages from the archive with the same version.
    assert [m.text for m in bridge._archived_context(transfer)[1].messages] == written[-1]
    # Naming the version the source resumes is the same as not choosing one.
    plain = bridge.convert(conversation.source.key, target.id, variant=current)
    assert "variant" not in plain.context_policy and written[-1][-1] == "here is the list"
    with pytest.raises(BridgeError) as error:
        SyncService(bridge=bridge).link(conversation.source.key, transfer_id=transfer.id)
    assert error.value.code == "sync_variant_unsupported"
