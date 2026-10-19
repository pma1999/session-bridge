import uuid
from itertools import permutations
from types import SimpleNamespace

import pytest

from session_bridge.errors import BridgeError
from session_bridge.models import Capability, Conversation, Event, NativeReceipt, Profile, SessionRef, digest
from session_bridge.service import Bridge
from session_bridge.sync import SyncService


def native(tool, profile, identifier, messages, cwd, locator=""):
    events, records = [], []
    for index, (role, text) in enumerate(messages):
        key = f"{identifier}-{index}"
        if tool == "claude":
            record = {"type": role, "uuid": key, "message": {"role": role, "content": text}}
            if role == "assistant":
                record["message"]["stop_reason"] = "end_turn"
        elif tool == "opencode":
            record = {"row": {"id": key}, "data": {"time": {"completed": 100} if role == "assistant" else {}}}
        else:
            record = {
                "type": "response_item",
                "payload": {"id": key, "type": "message", "role": role, "content": [{"text": text}]},
            }
        records.append(record)
        events.append(Event(id=key, kind="message", role=role, text=text, native=record))
    if tool == "codex":
        records.append({"type": "event_msg", "payload": {"type": "task_complete"}})
    return Conversation(
        source=SessionRef(
            profile_id=profile, native_id=identifier, tool=tool, cwd=str(cwd), title="test", locator=locator
        ),
        events=events,
        raw_records=records,
        source_fingerprint=digest(records),
    )


@pytest.fixture
def world(tmp_path, monkeypatch):
    project = tmp_path / "project with spaces á"
    project.mkdir()
    bridge = Bridge(tmp_path / "state")
    profiles = {
        tool: Profile(id=tool, tool=tool, home=str(tmp_path / tool), executable="fake.exe")
        for tool in ("claude", "opencode", "codex")
    }
    conversations, receipts, writes, reads, resumes = {}, {}, [], [], []
    monkeypatch.setattr(bridge, "profiles", lambda: list(profiles.values()))

    def show(key, snapshot=False):
        reads.append(key)
        if key not in conversations:
            raise BridgeError("session_not_found", "No existe.")
        return conversations[key].model_copy(deep=True)

    def write(request):
        writes.append(request)
        key = request.profile.id + ":" + request.native_id
        conversations[key] = native(
            request.profile.tool,
            request.profile.id,
            request.native_id,
            [(m.role, m.text) for m in request.messages],
            request.cwd,
        )
        receipt = NativeReceipt(
            profile_id=request.profile.id,
            native_id=request.native_id,
            cwd=request.cwd,
            fingerprint=conversations[key].source_fingerprint,
            created_ids=[request.native_id],
            evidence={"owned": True, "operation_ids": [request.operation_id]},
        )
        receipts[key] = receipt
        return receipt

    def verify(profile, receipt, messages):
        assert receipt.evidence["owned"]
        observed = conversations[profile.id + ":" + receipt.native_id]
        assert [(e.role, e.text) for e in observed.events] == [(m.role, m.text) for m in messages]
        return receipt

    writer = SimpleNamespace(
        capability=lambda p: Capability(
            tool=p.tool, version="test", native=True, reason="fake local evidence"
        ),
        allocate_id=lambda p: ("ses_" if p.tool == "opencode" else "") + uuid.uuid4().hex,
        write=write,
        verify=verify,
        inspect=lambda p, n: receipts.get(p.id + ":" + n),
    )
    monkeypatch.setattr(bridge, "show", show)
    monkeypatch.setattr(bridge, "_writer", lambda p: writer)

    def resume(key, launch=True):
        resumes.append((key, launch))
        transfer = bridge.store.get(key)
        return {
            "mode": "native",
            "native_id": transfer.native_id,
            "profile": transfer.target_profile,
            "cwd": transfer.cwd,
            "argv": ["fake.exe", "resume", transfer.native_id],
            "env": {},
            "launched": launch,
        }

    monkeypatch.setattr(bridge, "resume", resume)
    service = SyncService(bridge=bridge)

    def seed(tool="claude", identifier="source", messages=None):
        key = tool + ":" + identifier
        conversations[key] = native(
            tool, tool, identifier, messages or [("user", "question"), ("assistant", "answer")], project
        )
        return key

    def append(key, user="next", answer="done"):
        c = conversations[key]
        messages = [(event.role, event.text) for event in c.events] + [("user", user), ("assistant", answer)]
        conversations[key] = native(c.source.tool, c.source.profile_id, c.source.native_id, messages, project)

    return SimpleNamespace(
        service=service,
        bridge=bridge,
        profiles=profiles,
        conversations=conversations,
        receipts=receipts,
        writes=writes,
        reads=reads,
        resumes=resumes,
        writer=writer,
        seed=seed,
        append=append,
        project=project,
    )


@pytest.mark.parametrize("source,target", list(permutations(("claude", "opencode", "codex"), 2)))
def test_all_six_linked_directions_reuse_unchanged_materialization(world, source, target):
    key = world.seed(source)
    linked = world.service.link(key, name="my conversation")
    first = world.service.open(linked["id"], target, launch=False)
    again = world.service.open(linked["id"], target, launch=False)
    assert first["native_id"] == again["native_id"]
    assert first["launched"] is False
    assert len(world.writes) == 1
    assert world.resumes == [(first["transfer_id"], False), (first["transfer_id"], False)]
    assert world.service.status(linked["id"])["state"] == "synced"


def test_continuation_round_trip_has_one_head_no_echo_and_preserves_old_source(world):
    key = world.seed()
    before = world.conversations[key].model_dump()
    link = world.service.link(key)
    opened = world.service.open(link["id"], "opencode", launch=False)
    target = "opencode:" + opened["native_id"]
    world.append(target)
    assert world.service.poll(force=True)["changed"] == 1
    updated = world.service.open(link["id"], "codex", launch=False)
    assert len(world.writes[-1].messages) == 4
    assert len(world.service.status(link["id"])["heads"]) == 1
    assert world.service.poll(force=True)["changed"] == 0
    assert world.conversations[key].model_dump() == before
    assert updated["revision_id"] != opened["revision_id"]


def test_concurrent_continuations_preserve_two_branches_and_require_selection(world):
    original = world.seed()
    link = world.service.link(original)
    opened = world.service.open(link["id"], "opencode", launch=False)
    world.append("opencode:" + opened["native_id"], "approach A", "A done")
    world.append(original, "approach B", "B done")
    world.service.poll(force=True)
    status = world.service.status(link["id"])
    assert status["state"] == "conflict" and len(status["heads"]) == 2
    with pytest.raises(BridgeError) as error:
        world.service.open(link["id"], "codex", launch=False)
    assert error.value.code == "sync_conflict"
    chosen = status["heads"][0]["id"]
    world.service.select(link["id"], chosen)
    result = world.service.open(link["id"], "codex", launch=False)
    assert result["revision_id"] == chosen
    assert len(world.service.status(link["id"])["heads"]) == 2


def test_repeated_legitimate_text_is_not_deduplicated(world):
    key = world.seed(messages=[("user", "same"), ("assistant", "same")])
    link = world.service.link(key)
    world.append(key, "same", "same")
    assert world.service.poll(force=True)["changed"] == 1
    revision = world.service.store.get_revision(world.service.status(link["id"])["heads"][0]["id"])
    context = world.service.store.get_context(revision["context"])
    assert [m["text"] for m in context] == ["same"] * 4
    assert len({m["id"] for m in context}) == 4


def test_pending_turn_does_not_publish_and_original_data_remains_archival(world):
    key = world.seed()
    link = world.service.link(key)
    c = world.conversations[key]
    record = {"type": "user", "uuid": "pending", "message": {"role": "user", "content": "unfinished"}}
    c.events.append(Event(id="pending", kind="message", role="user", text="unfinished", native=record))
    c.raw_records.append(record)
    assert world.service.poll(force=True)["changed"] == 0
    with pytest.raises(BridgeError) as error:
        world.service.open(link["id"], "codex", launch=False)
    assert error.value.code == "sync_not_current"
    internal = world.service.store.get_link(link["id"])
    archived = world.service.store.get_capture(internal["endpoints"][0]["capture"])
    assert archived["raw_records"][-1] == record
    assert archived["events"][-1]["active"]  # Capture and projected context are separate objects.


def test_compaction_changes_context_without_reviving_prior_messages(world):
    key = world.seed()
    link = world.service.link(key)
    c = world.conversations[key]
    for event in c.events:
        event.active = False
    summary = {
        "type": "user",
        "uuid": "summary",
        "isCompactSummary": True,
        "message": {"role": "user", "content": "current summary"},
    }
    c.events.append(Event(id="summary", kind="summary", role="user", text="current summary", native=summary))
    c.raw_records.append(summary)
    assert world.service.poll(force=True)["changed"] == 1
    world.service.open(link["id"], "codex", launch=False)
    assert [m.text for m in world.writes[-1].messages] == ["current summary"]
    assert len(world.service.store.revisions(link["id"])) == 2


def test_pause_unlink_and_profile_drift_never_write_native_sources(world):
    key = world.seed()
    link = world.service.link(key)
    world.service.pause(link["id"])
    world.append(key)
    assert world.service.poll(force=True)["changed"] == 0
    world.service.unpause(link["id"])
    world.profiles["claude"].home = str(world.project / "different")
    result = world.service.poll(force=True)
    assert result["errors"][0]["code"] == "sync_profile_changed"
    assert len(world.service.status(link["id"])["heads"]) == 1
    world.service.unlink(link["id"])
    assert world.service.list() == []
    assert world.service.status(link["id"])["state"] == "unlinked"
    assert world.writes == []


def test_wsl_is_rejected_before_discovery_or_read(world):
    world.profiles["claude"].environment = "wsl"
    with pytest.raises(BridgeError) as error:
        world.service.link("claude:source")
    assert error.value.code == "sync_windows_only"
    assert world.reads == []


def test_materialization_records_intended_transfer_id_before_native_side_effect(world):
    key = world.seed()
    link = world.service.link(key)
    original_write = world.writer.write

    def inspect_intent(request):
        value = world.service.store.get_link(link["id"])
        planned = [intent["transfer_id"] for intent in value["materializations"].values()]
        assert request.operation_id in planned
        return original_write(request)

    world.writer.write = inspect_intent
    world.service.open(link["id"], "codex", launch=False)


def test_uncertain_native_publish_recovers_same_target_after_restart(world):
    link = world.service.link(world.seed())
    original_write = world.writer.write

    def interrupted(request):
        original_write(request)
        raise BridgeError("import_timeout", "Respuesta incierta.")

    world.writer.write = interrupted
    with pytest.raises(BridgeError):
        world.service.open(link["id"], "codex", launch=False)
    pending = world.bridge.transfers()[0]
    world.writer.write = original_write
    reopened = SyncService(bridge=world.bridge)
    completed = reopened.open(link["id"], "codex", launch=False)
    assert completed["transfer_id"] == pending.id and completed["native_id"] == pending.native_id
    assert len(world.writes) == 1


def test_link_existing_verified_conversion_uses_exact_original_revision(world):
    key = world.seed()
    transfer = world.bridge.convert(key, "opencode")
    link = world.service.link(key, transfer_id=transfer.id, name="linked conversion")
    opened = world.service.open(link["id"], "opencode", launch=False)
    assert opened["native_id"] == transfer.native_id and len(world.writes) == 1


def test_link_without_completion_waits_then_advances_automatically(world):
    key = world.seed(messages=[("user", "waiting")])
    link = world.service.link(key)
    assert link["state"] == "waiting" and link["heads"] == []
    world.append(key)
    assert world.service.poll(force=True)["changed"] == 1
    assert world.service.status(link["id"])["state"] == "synced"


def test_failed_checkpoint_transaction_does_not_publish_a_dangling_head(world, monkeypatch):
    key = world.seed()
    link = world.service.link(key)
    before = world.service.status(link["id"])["heads"]
    world.append(key)
    original_save = world.service.store.save_link

    def fail_once(value, *, db=None):
        if db is not None:
            raise BridgeError("sync_store_invalid", "Fallo controlado antes del commit.")
        return original_save(value, db=db)

    monkeypatch.setattr(world.service.store, "save_link", fail_once)
    result = world.service.poll(force=True)
    assert result["errors"][0]["code"] == "sync_store_invalid"
    assert world.service.status(link["id"])["heads"] == before
    monkeypatch.setattr(world.service.store, "save_link", original_save)
    assert world.service.poll(force=True)["changed"] == 1


def test_retire_missing_old_source_preserves_archive_and_restores_current_open(world):
    original = world.seed()
    link = world.service.link(original)
    target = world.service.open(link["id"], "opencode", launch=False)
    world.append("opencode:" + target["native_id"])
    del world.conversations[original]
    world.service.poll(force=True)
    world.service.detach(link["id"], original)
    opened = world.service.open(link["id"], "codex", launch=False)
    assert opened["native_id"]
    assert len(world.writes[-1].messages) == 4
    old = world.service.store.get_link(link["id"])["endpoints"][0]
    assert old["capture"] and not old["enabled"]
    assert len(world.service.store.get_capture(old["capture"])["events"]) == 2


def test_explicit_new_copy_keeps_previous_native_and_branch(world):
    link = world.service.link(world.seed())
    first = world.service.open(link["id"], "codex", launch=False)
    copied = world.service.open(link["id"], "codex", new_copy=True, launch=False)
    assert first["native_id"] != copied["native_id"]
    assert copied["revision_id"] == first["revision_id"]
    assert "codex:" + first["native_id"] in world.conversations
    assert len(world.writes) == 2


def test_incomplete_intent_cannot_be_misrepresented_as_a_portable_recovery_backup(world, tmp_path):
    link = world.service.link(world.seed())
    world.writer.write = lambda _: (_ for _ in ()).throw(BridgeError("native_timeout", "Fallo incierto."))
    with pytest.raises(BridgeError):
        world.service.open(link["id"], "codex", launch=False)
    with pytest.raises(BridgeError) as error:
        world.service.backup(tmp_path / "incomplete.sqlite3")
    assert error.value.code == "sync_recovery_required"
    assert not (tmp_path / "incomplete.sqlite3").exists()


def test_lookup_redirects_only_exact_linked_keys_and_owned_transfer_ids(world):
    key = world.seed()
    link = world.service.link(key)
    result = world.service.open(link["id"], "opencode", launch=False)
    assert world.service.lookup(key) == (link["id"], "claude")
    assert world.service.lookup(result["transfer_id"]) == (link["id"], "opencode")
    assert world.service.lookup("source") is None


def test_interactive_launch_does_not_hold_the_sync_operation_lock(world):
    link = world.service.link(world.seed())
    original = world.bridge.resume

    def resume(key, launch=True):
        if launch:
            with world.service.store.lock():
                pass
        return original(key, launch=launch)

    world.bridge.resume = resume
    assert world.service.open(link["id"], "codex", launch=True)["launched"]
