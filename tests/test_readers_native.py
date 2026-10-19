import json
import os
import sqlite3
import time
import tracemalloc

import pytest

from session_bridge.errors import BridgeError
from session_bridge.models import Profile, SessionRef
from session_bridge.readers import claude, codex, opencode


def profile(tmp_path, tool):
    return Profile(id=tool, tool=tool, home=str(tmp_path), version="fixture")


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Diseña un motor de búsqueda", "Diseña un motor de búsqueda"),
        (None, "Original first message"),
        ("", "Original first message"),
    ],
)
def test_codex_discovery_preserves_visible_name_before_original_prompt_title(tmp_path, name, expected):
    p = profile(tmp_path, "codex")
    database = tmp_path / "state_5.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE threads(id TEXT, name TEXT, title TEXT, cwd TEXT)")
        connection.execute(
            "INSERT INTO threads VALUES(?,?,?,?)", ("example", name, "Original first message", "/demo")
        )
    before = database.read_bytes()
    refs = codex.discover(p)
    assert len(refs) == 1 and refs[0].title == expected
    assert refs[0].cwd == "/demo"
    assert database.read_bytes() == before


def jsonl(path, records, tail=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + tail, encoding="utf-8")


def test_claude_selects_explicit_leaf_preserves_side_branch_and_truncated_tail(tmp_path):
    p = profile(tmp_path, "claude")
    path = tmp_path / ".claude/projects/encoded/session.jsonl"
    jsonl(
        path,
        [
            {
                "type": "user",
                "uuid": "a",
                "parentUuid": None,
                "cwd": "C:/test",
                "message": {"role": "user", "content": "first"},
            },
            {
                "type": "assistant",
                "uuid": "b",
                "parentUuid": "a",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "chosen"}]},
            },
            {
                "type": "assistant",
                "uuid": "c",
                "parentUuid": "a",
                "message": {"role": "assistant", "content": "other"},
                "unknown": {"preserve": True},
            },
            {"type": "last-prompt", "leafUuid": "b"},
        ],
        '{"type":',
    )
    refs = claude.discover(p)
    assert len(refs) == 1 and refs[0].cwd == "C:/test"
    assert "chosen" not in json.dumps(refs[0].model_dump())
    conv = claude.read(p, refs[0])
    assert [e.text for e in conv.events if e.active and e.kind == "message"] == ["first", "chosen"]
    assert any(e.id == "c" and not e.active for e in conv.events)
    assert any("trunc" in w for w in conv.warnings)
    assert conv.raw_records[2]["unknown"]["preserve"]
    assert conv.source_fingerprint == claude.read(p, refs[0]).source_fingerprint


def test_claude_ambiguous_branch_and_active_guard(tmp_path):
    p = profile(tmp_path, "claude")
    path = tmp_path / ".claude/projects/p/s.jsonl"
    jsonl(
        path,
        [
            {"type": "user", "uuid": "a", "parentUuid": None, "message": {"content": "x"}},
            {"type": "assistant", "uuid": "b", "parentUuid": "a", "message": {"content": "b"}},
            {"type": "assistant", "uuid": "c", "parentUuid": "a", "message": {"content": "c"}},
        ],
    )
    ref = claude.discover(p)[0]
    assert claude.read(p, ref).context_ambiguous
    ref.active = True
    with pytest.raises(BridgeError, match="activa") as err:
        claude.read(p, ref)
    assert err.value.code == "source_active"
    assert claude.read(p, ref, snapshot=True).events


def oc_db(tmp_path):
    path = tmp_path / ".local/share/opencode/opencode.db"
    path.parent.mkdir(parents=True)
    c = sqlite3.connect(path)
    for table in ["session", "session_v2"]:
        c.execute(
            "create table "
            + table
            + "(id text primary key,title text,directory text,parent_id text,time_created int,time_updated int,time_archived int,revert text)"
        )
    c.execute("create table message(id text,session_id text,time_created int,time_updated int,data text)")
    c.execute(
        "create table part(id text,message_id text,session_id text,time_created int,time_updated int,data text)"
    )
    c.execute(
        "create table session_message(id text,session_id text,type text,seq int,time_created int,time_updated int,data text)"
    )
    c.execute("create table account(secret text)")
    return c, path


def test_opencode_mixed_dedup_and_revert(tmp_path):
    c, _path = oc_db(tmp_path)
    for t, title in [("session", "old"), ("session_v2", "current")]:
        c.execute(
            "insert into " + t + " values(?,?,?,?,?,?,?,?)",
            ("s", title, "/repo", None, 1, 2, None, json.dumps({"messageID": "m2"})),
        )
    c.execute("insert into message values(?,?,?,?,?)", ("m1", "s", 1, 1, json.dumps({"role": "user"})))
    c.execute(
        "insert into part values(?,?,?,?,?,?)",
        ("p1", "m1", "s", 1, 1, json.dumps({"type": "text", "text": "legacy"})),
    )
    for seq, id, text in [(1, "m1", "new"), (2, "m2", "reverted")]:
        c.execute(
            "insert into session_message values(?,?,?,?,?,?,?)",
            (id, "s", "user", seq, seq, seq, json.dumps({"text": text, "unknown": True})),
        )
    c.commit()
    c.close()
    p = profile(tmp_path, "opencode")
    refs = opencode.discover(p)
    assert len(refs) == 1 and refs[0].title == "current"
    assert "reverted" not in json.dumps(refs[0].model_dump())
    conv = opencode.read(p, refs[0])
    assert [e.text for e in conv.events if e.active and e.kind == "message"] == ["new"]
    assert any(e.text == "reverted" and not e.active for e in conv.events)
    assert any(r.get("table") == "part" for r in conv.raw_records)
    assert len([e for e in conv.events if e.id == "m1"]) == 1


def test_codex_paginated_plus_rollout_no_duplicate_bodies(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    rollout = home / "sessions/2026/s.jsonl"
    jsonl(
        rollout,
        [
            {"type": "session_meta", "payload": {"id": "s", "cwd": "/repo"}},
            {
                "ordinal": 1,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "i1",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            },
            {"ordinal": 2, "type": "event_msg", "payload": {"type": "user_message", "message": "hello"}},
            {
                "ordinal": 3,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "i2",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "answer"}],
                },
            },
        ],
    )
    c = sqlite3.connect(home / "state_5.sqlite")
    c.execute(
        "create table threads(id text,rollout_path text,title text,cwd text,history_mode text,archived int)"
    )
    c.execute(
        "insert into threads values(?,?,?,?,?,?)", ("s", str(rollout), "title", "/repo", "paginated", 0)
    )
    c.commit()
    c.close()
    c = sqlite3.connect(home / "thread_history_1.sqlite")
    c.execute(
        "create table thread_items(thread_id text,turn_id text,item_id text,rollout_ordinal int,item_json text,item_type text,updated_at_ordinal int)"
    )
    c.execute("create table thread_turns(thread_id text,turn_id text,rollout_ordinal int,status text)")
    c.execute(
        "create table thread_history_projection_state(thread_id text,next_rollout_byte_offset int,next_rollout_ordinal int)"
    )
    for seq, id, kind, text in [(1, "i1", "userMessage", "hello"), (3, "i2", "agentMessage", "answer")]:
        c.execute(
            "insert into thread_items values(?,?,?,?,?,?,?)",
            (
                "s",
                "t",
                id,
                seq,
                json.dumps({"type": kind, "id": id, "content": [{"type": "text", "text": text}]}),
                kind,
                seq,
            ),
        )
    c.execute("insert into thread_turns values(?,?,?,?)", ("s", "t", 0, "completed"))
    c.commit()
    c.close()
    p = profile(tmp_path, "codex")
    ref = codex.discover(p)[0]
    conv = codex.read(p, ref)
    assert [e.text for e in conv.events if e.active and e.kind == "message"] == ["hello", "answer"]
    assert len(conv.raw_records) >= 8
    assert not conv.context_ambiguous


def test_corrupt_zstd_and_unknown_database_schema(tmp_path):
    p = profile(tmp_path, "codex")
    path = tmp_path / ".codex/sessions/bad.jsonl.zst"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"bad")
    ref = SessionRef(profile_id=p.id, native_id="bad", tool="codex", locator=str(path))
    with pytest.raises(BridgeError) as err:
        codex.read(p, ref)
    assert err.value.code == "source_corrupt"
    c, _path = oc_db(tmp_path)
    c.execute("drop table session")
    c.execute("drop table session_v2")
    c.commit()
    c.close()
    with pytest.raises(BridgeError) as err:
        opencode.discover(profile(tmp_path, "opencode"))
    assert err.value.code == "source_schema_unsupported"


def test_ten_thousand_message_capture(tmp_path):
    p = profile(tmp_path, "claude")
    path = tmp_path / ".claude/projects/p/large.jsonl"
    jsonl(
        path,
        [
            {
                "type": "user" if i % 2 == 0 else "assistant",
                "uuid": str(i),
                "parentUuid": str(i - 1) if i else None,
                "message": {"content": "text " + str(i)},
            }
            for i in range(10000)
        ],
    )
    tracemalloc.start()
    start = time.perf_counter()
    conv = claude.read(p, SessionRef(profile_id=p.id, native_id="large", tool="claude", locator=str(path)))
    elapsed = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len([e for e in conv.events if e.kind == "message"]) == 10000
    print(f"10k capture: {elapsed:.3f}s peak={peak / 1024 / 1024:.1f}MiB")


def test_opencode_completed_compaction_retains_recent_and_summary(tmp_path):
    c, _path = oc_db(tmp_path)
    c.execute("insert into session_v2 values(?,?,?,?,?,?,?,?)", ("s", "t", "/repo", None, 1, 2, None, None))
    for seq, id, kind, data in [
        (1, "m1", "user", {"text": "old"}),
        (2, "m2", "user", {"text": "recent"}),
        (
            3,
            "m3",
            "compaction",
            {"status": "completed", "summary": "summary", "recent": "serialized recent conversation"},
        ),
        (4, "m4", "assistant", {"content": [{"type": "text", "text": "after"}]}),
    ]:
        c.execute(
            "insert into session_message values(?,?,?,?,?,?,?)",
            (id, "s", kind, seq, seq, seq, json.dumps(data)),
        )
    c.commit()
    c.close()
    p = profile(tmp_path, "opencode")
    conv = opencode.read(p, opencode.discover(p)[0])
    assert [(e.kind, e.text) for e in conv.events if e.active] == [
        ("summary", "summary\n\n[Retained recent conversation]\nserialized recent conversation"),
        ("message", "after"),
    ]
    assert not conv.context_ambiguous
    from session_bridge.projection import project

    assert [m.text for m in project(conv).messages] == [
        "summary\n\n[Retained recent conversation]\nserialized recent conversation",
        "after",
    ]


def test_codex_compressed_orphan_and_replacement_history(tmp_path):
    import zstandard

    p = profile(tmp_path, "codex")
    base = tmp_path / ".codex"
    path = base / "archived_sessions/s.jsonl.zst"
    path.parent.mkdir(parents=True)
    records = [
        {"type": "session_meta", "payload": {"id": "s", "cwd": "/repo"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": "old"}},
        {
            "type": "compacted",
            "payload": {"replacement_history": [{"type": "message", "role": "user", "content": "retained"}]},
        },
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "next"}},
    ]
    data = "".join(json.dumps(r) + "\n" for r in records).encode()
    path.write_bytes(zstandard.ZstdCompressor().compress(data))
    refs = codex.discover(p)
    assert len(refs) == 1 and refs[0].archived
    conv = codex.read(p, refs[0])
    assert [e.text for e in conv.events if e.active and e.kind == "message"] == ["retained", "next"]
    assert not conv.context_ambiguous


def test_reader_ref_cannot_escape_profile(tmp_path):
    p = profile(tmp_path, "claude")
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}", encoding="utf-8")
    ref = SessionRef(profile_id=p.id, native_id="x", tool="claude", locator=str(outside))
    with pytest.raises(BridgeError) as err:
        claude.read(p, ref)
    assert err.value.code == "source_path_invalid"


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length paths")
def test_codex_reads_extended_length_rollout_and_attachment_paths(tmp_path):
    p = profile(tmp_path, "codex")
    path = tmp_path / "sessions/2026/10/13/rollout-x.jsonl"
    image = tmp_path / "attachments/image.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    jsonl(
        path,
        [
            {"type": "session_meta", "payload": {"id": "x"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "local_image", "path": "\\\\?\\" + str(image)}],
                },
            },
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "hi"}},
        ],
    )
    with sqlite3.connect(tmp_path / "state_5.sqlite") as connection:
        connection.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT)")
        connection.execute("INSERT INTO threads VALUES(?,?)", ("x", "\\\\?\\" + str(path)))
    (ref,) = codex.discover(p)
    conv = codex.read(p, ref)
    assert [e.text for e in conv.events if e.active and e.kind == "message"][-1] == "hi"
    assert [a.managed for a in conv.attachments] == [True]
    outside = tmp_path.parent / (tmp_path.name + "-outside.jsonl")
    outside.write_text("{}", encoding="utf-8")
    unc = "localhost\\" + str(outside).replace(":", "$", 1)
    for locator in (
        "\\\\?\\" + str(outside),
        "\\\\?\\unc\\" + unc,
        "\\\\?\\GLOBALROOT\\x",
        "\\\\?\\sessions\\2026\\10\\13\\rollout-x.jsonl",
    ):
        escaped = SessionRef(profile_id=p.id, native_id="x", tool="codex", locator=locator)
        with pytest.raises(BridgeError) as err:
            codex.read(p, escaped)
        assert err.value.code == "source_path_invalid"


def test_codex_retained_items_reference_compaction_once_and_stay_checkpointable(tmp_path):
    from session_bridge.models import canonical, digest
    from session_bridge.projection import project
    from session_bridge.sync_capture import checkpoint

    p = profile(tmp_path, "codex")
    items = [
        {
            "type": "message",
            "role": "user" if n % 2 == 0 else "assistant",
            "content": f"kept {n} " + "x" * 4096,
        }
        for n in range(60)
    ]
    compacted = {"type": "compacted", "payload": {"message": "", "replacement_history": items}}
    path = tmp_path / "sessions/rollout-x.jsonl"
    jsonl(
        path,
        [
            {"type": "session_meta", "payload": {"id": "x"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": "old"}},
            {"type": "event_msg", "payload": {"type": "task_complete"}},
            compacted,
        ],
    )
    (ref,) = codex.discover(p)
    conv = codex.read(p, ref)
    retained = [e for e in conv.events if e.id.startswith("retained-")]
    record = next(r for r in conv.raw_records if r.get("type") == "compacted")
    assert len(retained) == 60
    assert {e.native["source_record"] for e in retained} == {digest(record)}
    # Each item carries itself plus a reference, never another copy of the whole record.
    assert sum(len(canonical(e.native)) for e in retained) < 2 * len(canonical(record))
    closed = checkpoint(conv)
    assert [m.text for m in project(closed.conversation).messages] == [i["content"] for i in items]
    # Captures stored before the reference existed embed the whole record and still checkpoint.
    legacy = conv.model_copy(deep=True)
    for event in legacy.events:
        if event.id.startswith("retained-"):
            event.native = record
    closed = checkpoint(legacy)
    assert [m.text for m in project(closed.conversation).messages] == [i["content"] for i in items]


def test_opencode_reads_uncheckpointed_wal_without_writing(tmp_path):
    c, path = oc_db(tmp_path)
    c.execute("pragma journal_mode=wal")
    c.execute("pragma wal_autocheckpoint=0")
    c.execute(
        "insert into session_v2 values(?,?,?,?,?,?,?,?)",
        ("s", "WAL metadata", "/repo", None, 1, 2, None, None),
    )
    c.commit()
    before = (path.stat().st_size, path.stat().st_mtime_ns)
    refs = opencode.discover(profile(tmp_path, "opencode"))
    assert len(refs) == 1 and refs[0].title == "WAL metadata"
    assert (path.stat().st_size, path.stat().st_mtime_ns) == before
    c.close()


def test_truncated_zstd_frame_is_rejected(tmp_path):
    import zstandard

    p = profile(tmp_path, "codex")
    path = tmp_path / ".codex/sessions/s.jsonl.zst"
    path.parent.mkdir(parents=True)
    data = (json.dumps({"type": "session_meta", "payload": {"id": "s"}}) + "\n").encode()
    path.write_bytes(zstandard.ZstdCompressor(write_checksum=True).compress(data)[:-2])
    ref = SessionRef(profile_id=p.id, native_id="s", tool="codex", locator=str(path))
    with pytest.raises(BridgeError) as err:
        codex.read(p, ref)
    assert err.value.code == "source_corrupt"


def test_claude_leaf_marker_followed_by_unique_descendant(tmp_path):
    p = profile(tmp_path, "claude")
    path = tmp_path / ".claude/projects/p/s.jsonl"
    jsonl(
        path,
        [
            {"type": "user", "uuid": "a", "parentUuid": None, "message": {"content": "question"}},
            {"type": "last-prompt", "leafUuid": "a"},
            {"type": "assistant", "uuid": "b", "parentUuid": "a", "message": {"content": "answer"}},
        ],
    )
    conv = claude.read(p, claude.discover(p)[0])
    assert [e.text for e in conv.events if e.active and e.kind == "message"] == ["question", "answer"]


def test_changing_source_refuses_without_snapshot(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from session_bridge.readers.common import capture

    path = tmp_path / "s.jsonl"
    jsonl(path, [{"type": "user"}])
    real_stat = type(path).stat
    calls = []

    def changing_stat(current, *args, **kwargs):
        value = real_stat(current, *args, **kwargs)
        if current == path:
            calls.append(1)
            return SimpleNamespace(
                st_size=value.st_size, st_mtime_ns=value.st_mtime_ns + len(calls), st_ino=value.st_ino
            )
        return value

    monkeypatch.setattr(type(path), "stat", changing_stat)
    with pytest.raises(BridgeError) as err:
        capture(path)
    assert err.value.code == "source_changed"
    records, warnings = capture(path, snapshot=True)
    assert records and "source_changed_during_snapshot" in warnings


def test_codex_event_only_user_not_hidden_by_assistant_response(tmp_path):
    p = profile(tmp_path, "codex")
    path = tmp_path / ".codex/sessions/s.jsonl"
    jsonl(
        path,
        [
            {"type": "session_meta", "payload": {"id": "s"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "question"}},
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": "answer"},
            },
        ],
    )
    conv = codex.read(p, codex.discover(p)[0])
    assert [e.text for e in conv.events if e.active and e.kind == "message"] == ["question", "answer"]


def test_claude_preserved_messages_replayed_after_summary_and_related_subagent(tmp_path):
    from session_bridge.projection import project

    p = profile(tmp_path, "claude")
    path = tmp_path / ".claude/projects/p/s.jsonl"
    jsonl(
        path,
        [
            {"type": "user", "uuid": "old", "parentUuid": None, "message": {"content": "discarded"}},
            {"type": "assistant", "uuid": "kept", "parentUuid": "old", "message": {"content": "retained"}},
            {
                "type": "system",
                "uuid": "boundary",
                "parentUuid": None,
                "compactMetadata": {
                    "preservedMessages": {"anchorUuid": "summary", "uuids": ["kept"]},
                    "preservedSegment": {"anchorUuid": "summary", "headUuid": "kept", "tailUuid": "kept"},
                },
            },
            {
                "type": "user",
                "uuid": "summary",
                "parentUuid": "boundary",
                "isCompactSummary": True,
                "message": {"content": "summary text"},
            },
            {
                "type": "assistant",
                "uuid": "next",
                "parentUuid": "summary",
                "message": {"content": "next answer"},
            },
            {"type": "last-prompt", "leafUuid": "next"},
        ],
    )
    jsonl(
        path.parent / "s/subagents/agent-x.jsonl",
        [{"type": "user", "uuid": "child", "parentUuid": None, "message": {"content": "child prompt"}}],
    )
    conv = claude.read(p, claude.discover(p)[0])
    assert not conv.context_ambiguous
    assert [m.text for m in project(conv).messages] == ["summary text", "retained", "next answer"]
    assert len(conv.related) == 1 and conv.related[0].parent_id == "s"
    assert len(conv.raw_records) == 6


def test_embedded_attachments_and_external_reference_do_not_read_arbitrary_files(tmp_path):
    import base64

    p = profile(tmp_path, "claude")
    path = tmp_path / ".claude/projects/p/s.jsonl"
    encoded = base64.b64encode(b"image bytes").decode()
    jsonl(
        path,
        [
            {
                "type": "user",
                "uuid": "a",
                "parentUuid": None,
                "message": {
                    "content": [
                        {"type": "text", "text": "describe"},
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/png", "data": encoded},
                        },
                        {
                            "type": "image",
                            "source": {"type": "url", "url": "https://example.invalid/image.png"},
                        },
                    ]
                },
            }
        ],
    )
    conv = claude.read(p, claude.discover(p)[0])
    assert len(conv.attachments) == 2
    assert conv.attachments[0].data_base64 == encoded and conv.attachments[0].sha256
    assert conv.attachments[1].status == "remote_reference" and conv.attachments[1].url


def test_codex_retained_context_unknown_and_managed_attachment(tmp_path):
    p = profile(tmp_path, "codex")
    base = tmp_path / ".codex"
    path = base / "sessions/s.jsonl"
    asset = base / "attachments/image.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"image")
    jsonl(
        path,
        [
            {"type": "session_meta", "payload": {"id": "s"}},
            {"type": "retained_context", "payload": {"unsupported_shape": True}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "see image"},
                        {"type": "local_image", "path": str(asset)},
                    ],
                },
            },
        ],
    )
    conv = codex.read(p, codex.discover(p)[0])
    assert conv.context_ambiguous
    assert len(conv.attachments) == 1 and conv.attachments[0].managed
    assert conv.attachments[0].path == str(asset.resolve())


def test_partial_record_refuses_current_context(tmp_path):
    from session_bridge.projection import project

    p = profile(tmp_path, "claude")
    path = tmp_path / ".claude/projects/p/s.jsonl"
    jsonl(
        path,
        [{"type": "user", "uuid": "a", "parentUuid": None, "message": {"content": "safe"}}],
        '{"type":"assistant"',
    )
    conv = claude.read(p, claude.discover(p)[0])
    assert conv.context_ambiguous
    with pytest.raises(BridgeError) as err:
        project(conv)
    assert err.value.code == "ambiguous_context"
    assert project(conv, context="all").messages[0].text == "safe"


def test_zstd_resource_caps_and_concatenated_frames(tmp_path, monkeypatch):
    import zstandard

    from session_bridge.readers import common

    path = tmp_path / "s.jsonl.zst"
    frame = zstandard.ZstdCompressor(write_checksum=True)
    path.write_bytes(frame.compress(b'{"a":1}\n') + frame.compress(b'{"b":2}\n'))
    assert common.capture(path)[0] == [{"a": 1}, {"b": 2}]
    path.write_bytes(frame.compress((json.dumps({"text": "x" * 10000}) + "\n").encode()))
    monkeypatch.setattr(common, "MAX_DECOMPRESSED_BYTES", 4096)
    with pytest.raises(BridgeError) as err:
        common.capture(path)
    assert err.value.code == "source_too_large" and not err.value.details.get("content")


def test_jsonl_line_cap(tmp_path, monkeypatch):
    from session_bridge.readers import common

    path = tmp_path / "s.jsonl"
    jsonl(path, [{"text": "x" * 200}])
    monkeypatch.setattr(common, "MAX_LINE_BYTES", 64)
    with pytest.raises(BridgeError) as err:
        common.capture(path)
    assert err.value.code == "source_too_large"


def test_codex_paginated_unknown_schema_preserves_rollout_and_refuses_guess(tmp_path):
    p = profile(tmp_path, "codex")
    home = tmp_path / ".codex"
    home.mkdir()
    path = home / "sessions/s.jsonl"
    jsonl(
        path,
        [
            {"type": "session_meta", "payload": {"id": "s"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": "hello"}},
        ],
    )
    c = sqlite3.connect(home / "thread_history_1.sqlite")
    c.execute("create table future_history(unknown text)")
    c.close()
    ref = SessionRef(
        profile_id=p.id,
        native_id="s",
        tool="codex",
        locator=str(path),
        metadata={"history_mode": "paginated"},
    )
    conv = codex.read(p, ref)
    assert conv.context_ambiguous and conv.raw_records


def test_codex_paginated_transcript_not_revived_after_compaction(tmp_path):
    p = profile(tmp_path, "codex")
    home = tmp_path / ".codex"
    home.mkdir()
    path = home / "sessions/s.jsonl"
    jsonl(
        path,
        [
            {"type": "session_meta", "payload": {"id": "s"}},
            {
                "type": "response_item",
                "payload": {"type": "message", "id": "old", "role": "user", "content": "old"},
            },
            {
                "type": "compacted",
                "payload": {
                    "replacement_history": [{"type": "message", "role": "user", "content": "summary"}]
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "message", "id": "new", "role": "assistant", "content": "new"},
            },
        ],
    )
    c = sqlite3.connect(home / "thread_history_1.sqlite")
    c.execute("create table thread_items(thread_id text,item_id text,item_json text,rollout_ordinal int)")
    for id, text in [("old", "old"), ("new", "new")]:
        c.execute(
            "insert into thread_items values(?,?,?,?)",
            ("s", id, json.dumps({"type": "userMessage", "content": [{"type": "text", "text": text}]}), 1),
        )
    c.commit()
    c.close()
    conv = codex.read(
        p,
        SessionRef(
            profile_id=p.id,
            native_id="s",
            tool="codex",
            locator=str(path),
            metadata={"history_mode": "paginated"},
        ),
    )
    assert [e.text for e in conv.events if e.active and e.kind == "message"] == ["summary", "new"]
    assert not conv.context_ambiguous


def test_opencode_embedded_attachment_and_related_sessions(tmp_path):
    import base64

    c, _ = oc_db(tmp_path)
    for id, parent in [("s", None), ("child", "s")]:
        c.execute(
            "insert into session_v2 values(?,?,?,?,?,?,?,?)", (id, id, "/repo", parent, 1, 2, None, None)
        )
    c.execute(
        "insert into session_message values(?,?,?,?,?,?,?)",
        (
            "m1",
            "s",
            "user",
            1,
            1,
            1,
            json.dumps(
                {
                    "text": "hi",
                    "files": [
                        {
                            "data": base64.b64encode(b"document").decode(),
                            "mime": "application/pdf",
                            "source": {"type": "inline"},
                            "name": "document.pdf",
                        }
                    ],
                }
            ),
        ),
    )
    c.commit()
    c.close()
    p = profile(tmp_path, "opencode")
    ref = next(r for r in opencode.discover(p) if r.native_id == "s")
    conv = opencode.read(p, ref)
    assert len(conv.attachments) == 1 and conv.attachments[0].status == "available"
    assert [r.native_id for r in conv.related] == ["child"]


def test_codex_ten_thousand_message_zstd_capture(tmp_path):
    import zstandard

    p = profile(tmp_path, "codex")
    path = tmp_path / ".codex/sessions/large.jsonl.zst"
    path.parent.mkdir(parents=True)
    records = [{"type": "session_meta", "payload": {"id": "large"}}] + [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": str(i),
                "role": "user" if i % 2 == 0 else "assistant",
                "content": [
                    {"type": "input_text" if i % 2 == 0 else "output_text", "text": "message " + str(i)}
                ],
            },
        }
        for i in range(10000)
    ]
    serialized = "".join(json.dumps(r) + "\n" for r in records).encode()
    path.write_bytes(zstandard.ZstdCompressor(write_checksum=True).compress(serialized))
    tracemalloc.start()
    start = time.perf_counter()
    conv = codex.read(p, SessionRef(profile_id=p.id, native_id="large", tool="codex", locator=str(path)))
    elapsed = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len([e for e in conv.events if e.active and e.kind == "message"]) == 10000
    assert not conv.context_ambiguous
    print(
        f"10k zstd capture: {elapsed:.3f}s peak={peak / 1024 / 1024:.1f}MiB compressed={path.stat().st_size}B plain={len(serialized)}B"
    )


def test_codex_related_spawn_and_active_guard_from_real_history_table(tmp_path):
    p = profile(tmp_path, "codex")
    home = tmp_path / ".codex"
    home.mkdir()
    path = home / "sessions/s.jsonl"
    jsonl(
        path,
        [
            {"type": "session_meta", "payload": {"id": "s"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": "hello"}},
        ],
    )
    c = sqlite3.connect(home / "state_5.sqlite")
    c.execute("create table threads(id text)")
    c.execute("insert into threads values(?)", ("s",))
    c.execute("create table thread_spawn_edges(parent_thread_id text,child_thread_id text,status text)")
    c.execute("insert into thread_spawn_edges values(?,?,?)", ("s", "child", "completed"))
    c.commit()
    c.close()
    c = sqlite3.connect(home / "thread_history_1.sqlite")
    c.execute("create table thread_turns(thread_id text,turn_id text,status text,rollout_ordinal int)")
    c.execute("insert into thread_turns values(?,?,?,?)", ("s", "turn", "inProgress", 0))
    c.commit()
    c.close()
    ref = SessionRef(profile_id=p.id, native_id="s", tool="codex", locator=str(path))
    with pytest.raises(BridgeError) as err:
        codex.read(p, ref)
    assert err.value.code == "source_active"
    conv = codex.read(p, ref, snapshot=True)
    assert [r.native_id for r in conv.related] == ["child"]
    assert "source_active_snapshot" in conv.warnings


def test_claude_segment_without_explicit_list_and_missing_node_ambiguous(tmp_path):
    p = profile(tmp_path, "claude")
    path = tmp_path / ".claude/projects/p/s.jsonl"
    records = [
        {"type": "user", "uuid": "old", "parentUuid": None, "message": {"content": "discard"}},
        {"type": "user", "uuid": "head", "parentUuid": "old", "message": {"content": "head"}},
        {"type": "assistant", "uuid": "tail", "parentUuid": "head", "message": {"content": "tail"}},
        {
            "type": "system",
            "uuid": "boundary",
            "parentUuid": None,
            "compactMetadata": {
                "preservedSegment": {"anchorUuid": "summary", "headUuid": "head", "tailUuid": "tail"}
            },
        },
        {
            "type": "user",
            "uuid": "summary",
            "parentUuid": "boundary",
            "isCompactSummary": True,
            "message": {"content": "summary"},
        },
        {"type": "last-prompt", "leafUuid": "summary"},
    ]
    jsonl(path, records)
    conv = claude.read(p, claude.discover(p)[0])
    assert not conv.context_ambiguous
    assert [e.text for e in conv.events if e.active and e.kind in ("message", "summary")] == [
        "summary",
        "head",
        "tail",
    ]
    records[3]["compactMetadata"]["preservedSegment"]["headUuid"] = "missing"
    jsonl(path, records)
    assert claude.read(p, claude.discover(p)[0]).context_ambiguous


def test_sqlite_readers_never_query_account_table(tmp_path, monkeypatch):
    from session_bridge.readers import common

    c, _ = oc_db(tmp_path)
    c.execute(
        "insert into session_v2 values(?,?,?,?,?,?,?,?)", ("s", "title", "/repo", None, 1, 2, None, None)
    )
    c.commit()
    c.close()
    original = common.sqlite3.connect

    def connect(*args, **kwargs):
        con = original(*args, **kwargs)

        def authorizer(action, arg1, arg2, dbname, source):
            if action == sqlite3.SQLITE_READ and arg1 == "account":
                raise AssertionError("account table must not be read")
            return sqlite3.SQLITE_OK

        con.set_authorizer(authorizer)
        return con

    monkeypatch.setattr(common.sqlite3, "connect", connect)
    p = profile(tmp_path, "opencode")
    refs = opencode.discover(p)
    assert opencode.read(p, refs[0]).source.native_id == "s"


def test_claude_related_subagent_discoverable_and_current_context_transferable(tmp_path):
    from session_bridge.projection import project

    p = profile(tmp_path, "claude")
    path = tmp_path / ".claude/projects/p/parent.jsonl"
    jsonl(
        path,
        [
            {
                "type": "user",
                "sessionId": "parent",
                "cwd": "/repo",
                "uuid": "main",
                "parentUuid": None,
                "message": {"content": "main"},
            }
        ],
    )
    child = path.parent / "parent/subagents/agent-a.jsonl"
    jsonl(
        child,
        [
            {
                "type": "user",
                "sessionId": "parent",
                "agentId": "a",
                "isSidechain": True,
                "uuid": "c1",
                "parentUuid": None,
                "message": {"content": "child question"},
            },
            {
                "type": "assistant",
                "sessionId": "parent",
                "agentId": "a",
                "isSidechain": True,
                "uuid": "c2",
                "parentUuid": "c1",
                "message": {"content": "child answer"},
            },
        ],
    )
    refs = claude.discover(p)
    main = next(r for r in refs if r.native_id == "parent")
    relation = claude.read(p, main).related[0]
    selected = next(r for r in refs if r.key == relation.key)
    assert selected.parent_id == "parent" and selected.native_id != "parent"
    assert selected.cwd == "/repo"
    conv = claude.read(p, selected)
    assert not conv.context_ambiguous
    assert [m.text for m in project(conv).messages] == ["child question", "child answer"]
    assert all(e.branch == "sidechain" for e in conv.events if e.kind == "message")


def test_claude_subagent_keys_do_not_collide_when_agent_names_repeat(tmp_path):
    p = profile(tmp_path, "claude")
    for parent in ["parent1", "parent2"]:
        path = tmp_path / f".claude/projects/p/{parent}.jsonl"
        jsonl(
            path,
            [
                {
                    "type": "user",
                    "sessionId": parent,
                    "uuid": "main",
                    "parentUuid": None,
                    "message": {"content": "main"},
                }
            ],
        )
        jsonl(
            path.parent / f"{parent}/subagents/agent-a.jsonl",
            [
                {
                    "type": "user",
                    "sessionId": parent,
                    "agentId": "a",
                    "isSidechain": True,
                    "uuid": "child",
                    "parentUuid": None,
                    "message": {"content": "child"},
                }
            ],
        )
    refs = claude.discover(p)
    assert len(refs) == 4 and len({r.key for r in refs}) == 4
    assert {r.parent_id for r in refs if r.parent_id} == {"parent1", "parent2"}


def test_codex_spawn_edges_populate_discovery_parent_filter_and_transferable_child(tmp_path):
    from session_bridge.projection import project

    p = profile(tmp_path, "codex")
    home = tmp_path / ".codex"
    home.mkdir()
    c = sqlite3.connect(home / "state_5.sqlite")
    c.execute("create table threads(id text,rollout_path text,cwd text)")
    c.execute("create table thread_spawn_edges(parent_thread_id text,child_thread_id text,status text)")
    c.execute("insert into thread_spawn_edges values(?,?,?)", ("parent", "child", "completed"))
    for id in ["parent", "child"]:
        path = home / f"sessions/{id}.jsonl"
        jsonl(
            path,
            [
                {"type": "session_meta", "payload": {"id": id, "cwd": "/repo"}},
                {
                    "type": "response_item",
                    "payload": {"type": "message", "role": "user", "content": id + " prompt"},
                },
            ],
        )
        c.execute("insert into threads values(?,?,?)", (id, str(path), "/repo"))
    c.commit()
    c.close()
    refs = codex.discover(p)
    parent = next(r for r in refs if r.native_id == "parent")
    relation = codex.read(p, parent).related[0]
    selected = next(r for r in refs if r.key == relation.key)
    assert selected.parent_id == "parent" and selected.cwd == "/repo"
    assert [m.text for m in project(codex.read(p, selected)).messages] == ["child prompt"]
    # Related-tree capture reads the edge ref directly; it carries no locator of its own.
    assert relation.locator == ""
    assert [m.text for m in project(codex.read(p, relation)).messages] == ["child prompt"]


def test_managed_attachment_embeds_stable_bytes_before_source_changes(tmp_path):
    import base64
    import hashlib

    from session_bridge.readers.common import attachments_in

    root = tmp_path / ".codex"
    asset = root / "attachments/a.bin"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"captured bytes")
    captured = attachments_in([{"type": "local_image", "path": str(asset)}], root)[0]
    asset.write_bytes(b"later bytes")
    assert base64.b64decode(captured.data_base64) == b"captured bytes"
    assert captured.sha256 == hashlib.sha256(b"captured bytes").hexdigest()
    assert captured.managed and captured.status == "available"


def test_managed_attachment_limit_explicit_and_external_reference_untouched(tmp_path, monkeypatch):
    from session_bridge.readers import common

    root = tmp_path / ".codex"
    asset = root / "attachments/a.bin"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"x" * 100)
    monkeypatch.setattr(common, "MAX_ATTACHMENT_BYTES", 32, raising=False)
    with pytest.raises(BridgeError) as err:
        common.attachments_in([{"type": "local_image", "path": str(asset)}], root)
    assert err.value.code == "source_too_large"
    external = tmp_path / "missing-external.bin"
    attachment = common.attachments_in([{"type": "local_image", "path": str(external)}], root)[0]
    assert attachment.status == "external_reference" and attachment.data_base64 is None
    missing = common.attachments_in(
        [{"type": "local_image", "path": str(root / "attachments/missing.bin")}], root
    )[0]
    assert missing.status == "missing" and missing.data_base64 is None


def test_managed_attachment_symlink_escape_refused(tmp_path):
    from session_bridge.readers.common import attachments_in

    root = tmp_path / ".codex"
    folder = root / "attachments"
    folder.mkdir(parents=True)
    outside = tmp_path / "external.bin"
    outside.write_bytes(b"outside")
    link = folder / "link.bin"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Windows symlink creation privilege unavailable")
    with pytest.raises(BridgeError) as err:
        attachments_in([{"type": "local_image", "path": str(link)}], root)
    assert err.value.code == "source_path_invalid"


def test_managed_attachment_change_during_capture_refused(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from session_bridge.readers.common import attachments_in

    root = tmp_path / ".codex"
    asset = root / "attachments/a.bin"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"before")
    original = type(asset).open
    reads = []

    @contextmanager
    def changing_open(current, *args, **kwargs):
        with original(current, *args, **kwargs) as handle:
            yield handle
        if current == asset and args and args[0] == "rb":
            reads.append(1)
            if len(reads) == 1:
                with original(asset, "wb") as changed:
                    changed.write(b"after!")

    monkeypatch.setattr(type(asset), "open", changing_open)
    with pytest.raises(BridgeError) as err:
        attachments_in([{"type": "local_image", "path": str(asset)}], root)
    assert err.value.code == "source_changed"


def test_subagent_external_parent_must_match_known_parent_transcript(tmp_path):
    p = profile(tmp_path, "claude")
    path = tmp_path / ".claude/projects/p/parent.jsonl"
    jsonl(
        path,
        [
            {
                "type": "user",
                "sessionId": "parent",
                "uuid": "main",
                "parentUuid": None,
                "message": {"content": "parent"},
            }
        ],
    )
    child = path.parent / "parent/subagents/agent-a.jsonl"
    jsonl(
        child,
        [
            {
                "type": "user",
                "sessionId": "parent",
                "isSidechain": True,
                "uuid": "c1",
                "parentUuid": "main",
                "message": {"content": "child"},
            }
        ],
    )
    ref = next(r for r in claude.discover(p) if r.parent_id)
    assert not claude.read(p, ref).context_ambiguous
    jsonl(
        child,
        [
            {
                "type": "user",
                "sessionId": "parent",
                "isSidechain": True,
                "uuid": "c1",
                "parentUuid": "unknown",
                "message": {"content": "child"},
            }
        ],
    )
    assert claude.read(p, ref).context_ambiguous


def test_claude_tools_preserve_block_order_graph_and_closed_projection(tmp_path):
    from session_bridge.projection import project

    p = profile(tmp_path, "claude")
    path = tmp_path / ".claude/projects/p/s.jsonl"
    old_tool = {"type": "tool_use", "id": "old-call", "name": "old_tool", "input": {"old": True}}
    call = {
        "type": "tool_use",
        "id": "call-1",
        "name": "Run",
        "input": {"command": "echo data"},
        "signature": "do-not-project-signature",
    }
    records = [
        {"type": "user", "uuid": "start", "parentUuid": None, "message": {"content": "old question"}},
        {"type": "assistant", "uuid": "old", "parentUuid": "start", "message": {"content": [old_tool]}},
        {
            "type": "user",
            "uuid": "summary",
            "parentUuid": "old",
            "isCompactSummary": True,
            "message": {"content": "current summary"},
        },
        {
            "type": "assistant",
            "uuid": "a",
            "parentUuid": "summary",
            "message": {
                "content": [
                    {"type": "text", "text": "A"},
                    call,
                    {"type": "text", "text": "B"},
                    {
                        "type": "thinking",
                        "thinking": "pure private thought",
                        "signature": "private-signature",
                    },
                ]
            },
        },
        {
            "type": "user",
            "uuid": "b",
            "parentUuid": "a",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "call-1", "content": "result one"},
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-2",
                        "content": [{"type": "text", "text": "result two"}],
                    },
                    {"type": "text", "text": "C"},
                ]
            },
        },
        {
            "type": "assistant",
            "uuid": "fork",
            "parentUuid": "a",
            "message": {
                "content": [{"type": "tool_use", "id": "fork-call", "name": "fork_tool", "input": {}}]
            },
        },
        {"type": "last-prompt", "leafUuid": "b"},
    ]
    jsonl(path, records)
    conv = claude.read(p, claude.discover(p)[0])
    active = [e for e in conv.events if e.active and e.kind in ("message", "summary", "tool")]
    assert [e.kind for e in active] == ["summary", "message", "tool", "message", "tool", "tool", "message"]
    assert [e.text for e in active if e.kind != "tool"] == ["current summary", "A", "B", "C"]
    tools = [e for e in active if e.kind == "tool"]
    assert all(e.role == "tool" for e in tools)
    assert "echo data" in tools[0].text and "Run" in tools[0].text and "call-1" in tools[0].text
    assert "result one" in tools[1].text and "result two" in tools[2].text
    assert len({e.id for e in conv.events}) == len(conv.events)
    assert tools[0].id == "a:block:1"
    assert all(not e.active for e in conv.events if e.id.startswith(("old", "fork")))
    projected = project(conv)
    assert len(projected.messages) == 7
    assert all("Closed historical tool observation" in m.text for m in projected.messages if m.adapted)
    joined = "\n".join(m.text for m in projected.messages)
    assert "old_tool" not in joined and "fork_tool" not in joined
    assert "pure private thought" not in joined and "do-not-project-signature" not in joined
    assert conv.raw_records == records


def test_claude_preserved_uuid_restores_all_text_tool_blocks(tmp_path):
    from session_bridge.projection import project

    p = profile(tmp_path, "claude")
    path = tmp_path / ".claude/projects/p/s.jsonl"
    jsonl(
        path,
        [
            {
                "type": "assistant",
                "uuid": "kept",
                "parentUuid": None,
                "message": {
                    "content": [
                        {"type": "text", "text": "kept before"},
                        {"type": "tool_use", "id": "call", "name": "kept_tool", "input": {}},
                        {"type": "text", "text": "kept after"},
                    ]
                },
            },
            {
                "type": "system",
                "uuid": "boundary",
                "parentUuid": None,
                "compactMetadata": {"preservedMessages": {"anchorUuid": "summary", "uuids": ["kept"]}},
            },
            {
                "type": "user",
                "uuid": "summary",
                "parentUuid": "boundary",
                "isCompactSummary": True,
                "message": {"content": "summary"},
            },
            {"type": "last-prompt", "leafUuid": "summary"},
        ],
    )
    conv = claude.read(p, claude.discover(p)[0])
    messages = project(conv).messages
    assert len(messages) == 4
    assert messages[0].text == "summary" and messages[1].text == "kept before"
    assert "kept_tool" in messages[2].text and messages[3].text == "kept after"


def test_opencode_tools_preserve_content_order_and_args(tmp_path):
    from session_bridge.projection import project

    c, _ = oc_db(tmp_path)
    c.execute(
        "insert into session_v2 values(?,?,?,?,?,?,?,?)", ("s", "title", "/repo", None, 1, 2, None, None)
    )
    data = {
        "content": [
            {"type": "text", "text": "A"},
            {
                "type": "tool",
                "id": "call",
                "name": "Run",
                "providerState": {"signature": "private"},
                "state": {
                    "status": "completed",
                    "input": {"command": "echo data"},
                    "content": [{"type": "text", "text": "result"}],
                },
            },
            {"type": "text", "text": "B"},
        ]
    }
    c.execute(
        "insert into session_message values(?,?,?,?,?,?,?)",
        ("m", "s", "assistant", 1, 1, 1, json.dumps(data)),
    )
    c.commit()
    c.close()
    p = profile(tmp_path, "opencode")
    conv = opencode.read(p, opencode.discover(p)[0])
    events = [e for e in conv.events if e.active and e.kind in ("message", "tool")]
    assert [e.kind for e in events] == ["message", "tool", "message"]
    assert events[0].text == "A" and events[2].text == "B"
    assert "echo data" in events[1].text and "result" in events[1].text
    assert "private" not in "\n".join(m.text for m in project(conv).messages)


def test_codex_tool_call_arguments_and_results_project_as_closed_data(tmp_path):
    from session_bridge.projection import project

    p = profile(tmp_path, "codex")
    path = tmp_path / ".codex/sessions/s.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "s"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "A"}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "id": "f",
                "call_id": "call",
                "name": "Run",
                "arguments": '{"command":"echo data"}',
                "signature": "private",
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "call", "output": "result"},
        },
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "B"}},
    ]
    jsonl(path, records)
    conv = codex.read(p, codex.discover(p)[0])
    events = [e for e in conv.events if e.active and e.kind in ("message", "tool")]
    assert [e.kind for e in events] == ["message", "tool", "tool", "message"]
    assert "echo data" in events[1].text and "Run" in events[1].text and "call" in events[1].text
    assert "result" in events[2].text and "call" in events[2].text
    assert all(e.role == "tool" for e in events if e.kind == "tool")
    assert "private" not in "\n".join(m.text for m in project(conv).messages)
    assert conv.raw_records == records


def test_opencode_legacy_tools_preserve_part_order_and_arguments(tmp_path):
    from session_bridge.projection import project

    c, _ = oc_db(tmp_path)
    c.execute("insert into session values(?,?,?,?,?,?,?,?)", ("s", "title", "/repo", None, 1, 2, None, None))
    c.execute("insert into message values(?,?,?,?,?)", ("m", "s", 1, 1, json.dumps({"role": "assistant"})))
    for i, data in enumerate(
        [
            {"type": "text", "text": "A"},
            {
                "type": "tool",
                "tool": "legacy_run",
                "callID": "call",
                "state": {
                    "status": "completed",
                    "input": {"command": "legacy cmd"},
                    "output": "legacy result",
                },
            },
            {"type": "text", "text": "B"},
        ]
    ):
        c.execute("insert into part values(?,?,?,?,?,?)", (str(i), "m", "s", i, i, json.dumps(data)))
    c.commit()
    c.close()
    p = profile(tmp_path, "opencode")
    conv = opencode.read(p, opencode.discover(p)[0])
    messages = project(conv).messages
    assert len(messages) == 3 and messages[0].text == "A" and messages[2].text == "B"
    assert messages[1].adapted and "legacy_run" in messages[1].text
    assert "legacy cmd" in messages[1].text and "legacy result" in messages[1].text


def test_opencode_shell_history_is_closed_observation(tmp_path):
    from session_bridge.projection import project

    c, _ = oc_db(tmp_path)
    c.execute(
        "insert into session_v2 values(?,?,?,?,?,?,?,?)", ("s", "title", "/repo", None, 1, 2, None, None)
    )
    c.execute(
        "insert into session_message values(?,?,?,?,?,?,?)",
        (
            "m",
            "s",
            "shell",
            1,
            1,
            1,
            json.dumps(
                {
                    "shellID": "sh",
                    "command": "echo historical",
                    "status": "completed",
                    "output": {"output": "shell result"},
                }
            ),
        ),
    )
    c.commit()
    c.close()
    p = profile(tmp_path, "opencode")
    conv = opencode.read(p, opencode.discover(p)[0])
    projected = project(conv)
    assert len(projected.messages) == 1 and projected.messages[0].adapted
    assert "echo historical" in projected.messages[0].text and "shell result" in projected.messages[0].text
