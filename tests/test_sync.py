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
        if ":" in key and not key.startswith("tr_"):
            # A linked native session reopened by its own key, as Bridge.resume resolves it.
            profile, native_id = key.split(":", 1)
            return {
                "mode": "native",
                "native_id": native_id,
                "profile": profile,
                "cwd": conversations[key].source.cwd,
                "argv": ["fake.exe", "resume", native_id],
                "env": {},
                "launched": launch,
            }
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


def _roles(world, link_id):
    endpoints = world.service.status(link_id)["endpoints"]
    return {endpoint["key"]: (endpoint["role"], endpoint["origin"]) for endpoint in endpoints}


def _current(world, link_id):
    endpoints = world.service.status(link_id)["endpoints"]
    return {
        endpoint["profile_id"]: endpoint["key"] for endpoint in endpoints if endpoint["role"] == "current"
    }


def test_round_trip_keeps_one_current_session_per_assistant(world):
    original = world.seed()
    link = world.service.link(original)
    codex_a = "codex:" + world.service.open(link["id"], "codex", launch=False)["native_id"]
    world.append(codex_a)
    world.service.poll(force=True)
    claude_b = "claude:" + world.service.open(link["id"], "claude", launch=False)["native_id"]
    world.append(claude_b)
    world.service.poll(force=True)
    codex_c = "codex:" + world.service.open(link["id"], "codex", launch=False)["native_id"]
    roles = _roles(world, link["id"])
    assert roles == {
        original: ("superseded", "original"),
        codex_a: ("superseded", "copy"),
        claude_b: ("current", "copy"),
        codex_c: ("current", "copy"),
    }
    assert _current(world, link["id"]) == {"claude": claude_b, "codex": codex_c}


def test_returning_without_new_turns_reopens_the_original_session(world):
    original = world.seed()
    link = world.service.link(original)
    world.service.open(link["id"], "codex", launch=False)
    back = world.service.open(link["id"], "claude", launch=False)
    assert back["native_id"] == "source" and back["reused"] is True
    assert len(world.writes) == 1
    assert world.resumes[-1] == (original, False)
    assert _current(world, link["id"])["claude"] == original


def test_a_new_session_is_reported_as_not_reused(world):
    link = world.service.link(world.seed())
    assert world.service.open(link["id"], "codex", launch=False)["reused"] is False


def test_turn_typed_in_an_earlier_copy_is_still_captured_as_its_own_continuation(world):
    original = world.seed()
    link = world.service.link(original)
    codex_a = "codex:" + world.service.open(link["id"], "codex", launch=False)["native_id"]
    world.append(codex_a)
    world.service.poll(force=True)
    world.service.open(link["id"], "claude", launch=False)
    world.append(original, "stale tab", "stale answer")
    world.service.poll(force=True)
    status = world.service.status(link["id"])
    assert status["state"] == "conflict"
    assert {head["source_key"] for head in status["heads"]} == {original, codex_a}
    assert _roles(world, link["id"])[original] == ("superseded", "original")


def test_choose_a_continuation_by_assistant(world):
    original = world.seed()
    link = world.service.link(original)
    opened = world.service.open(link["id"], "opencode", launch=False)
    world.append("opencode:" + opened["native_id"], "approach A", "A done")
    world.append(original, "approach B", "B done")
    world.service.poll(force=True)
    chosen = world.service.select(link["id"], "opencode")["selected_head"]
    assert world.service.store.get_revision(chosen)["source_key"] == "opencode:" + opened["native_id"]


def test_choosing_by_assistant_is_refused_when_it_has_two_continuations(world):
    original = world.seed()
    link = world.service.link(original)
    codex_a = "codex:" + world.service.open(link["id"], "codex", launch=False)["native_id"]
    world.append(codex_a)
    world.service.poll(force=True)
    claude_b = "claude:" + world.service.open(link["id"], "claude", launch=False)["native_id"]
    world.append(original, "old tab", "old answer")
    world.append(claude_b, "new tab", "new answer")
    world.service.poll(force=True)
    with pytest.raises(BridgeError) as error:
        world.service.select(link["id"], "claude")
    assert error.value.code == "sync_branch_required"


def test_plan_open_says_whether_a_session_is_reused_without_writing(world):
    original = world.seed()
    link = world.service.link(original)
    before = world.service.store.get_link(link["id"])
    plan = world.service.plan_open(link["id"], "codex")
    assert plan["reuse"] is False and plan["profile_id"] == "codex"
    assert world.service.store.get_link(link["id"]) == before and world.writes == []
    world.service.open(link["id"], "codex", launch=False)
    assert world.service.plan_open(link["id"], "codex")["reuse"] is True
    assert world.service.plan_open(link["id"], "claude")["reuse"] is True


def test_head_previews_show_the_last_request_and_status_stays_content_free(world):
    original = world.seed()
    link = world.service.link(original)
    opened = world.service.open(link["id"], "opencode", launch=False)
    world.append("opencode:" + opened["native_id"], "approach A \x1b[31m", "A done")
    world.append(original, "approach B", "B done")
    world.service.poll(force=True)
    previews = world.service.head_previews(link["id"])
    assert sorted(previews.values()) == ["approach A \\x1b[31m", "approach B"]
    assert "approach" not in repr(world.service.status(link["id"]))


def _with_file(world, tool, identifier, messages):
    home = world.project.parent / tool
    rollout = home / "sessions" / f"rollout-{identifier}.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text("start\n", encoding="utf-8")
    key = tool + ":" + identifier
    world.conversations[key] = native(tool, tool, identifier, messages, world.project, str(rollout))
    return key, rollout, home


def test_an_earlier_copy_is_not_reread_when_only_the_shared_database_changes(world):
    original, rollout, home = _with_file(world, "codex", "thread", [("user", "q"), ("assistant", "a")])
    link = world.service.link(original)
    claude = "claude:" + world.service.open(link["id"], "claude", launch=False)["native_id"]
    world.append(claude)
    world.service.poll(force=True)
    world.service.open(link["id"], "codex", launch=False)
    world.service.poll(force=True)  # The earlier copy's own signature is now the baseline.
    world.reads.clear()
    (home / "state_5.sqlite").write_bytes(b"other thread activity")
    world.service.poll(force=True)
    assert original not in world.reads
    rollout.write_text("start\ncontinued\n", encoding="utf-8")
    world.service.poll(force=True)
    assert original in world.reads


def test_an_earlier_copy_without_its_own_file_is_reconciled_every_five_minutes(world):
    original = world.seed("opencode", "ses_original")
    link = world.service.link(original)
    claude = "claude:" + world.service.open(link["id"], "claude", launch=False)["native_id"]
    world.append(claude)
    world.service.poll(force=True)
    current = "opencode:" + world.service.open(link["id"], "opencode", launch=False)["native_id"]
    world.service.poll(force=True)
    world.reads.clear()
    home = world.project.parent / "opencode"
    home.mkdir(exist_ok=True)
    (home / "opencode.db").write_bytes(b"other session activity")
    world.service.poll()
    assert original not in world.reads
    assert current in world.reads
    stored = world.service.store.get_link(link["id"])
    next(e for e in stored["endpoints"] if e["key"] == original)["last_full_check"] -= 301
    world.service.store.save_link(stored)
    world.service.poll()
    assert original in world.reads


def test_new_turns_in_a_continuation_that_was_not_chosen_ask_for_a_choice_again(world):
    original = world.seed()
    link = world.service.link(original)
    opened = "opencode:" + world.service.open(link["id"], "opencode", launch=False)["native_id"]
    world.append(opened, "approach A", "A done")
    world.append(original, "approach B", "B done")
    world.service.poll(force=True)
    world.service.select(link["id"], "opencode")
    assert world.service.status(link["id"])["state"] == "synced"
    world.append(original, "still typing in the old tab", "answer")
    world.service.poll(force=True)
    status = world.service.status(link["id"])
    assert status["state"] == "conflict" and status["selected_head"] is None
    # Continuing the chosen one keeps the choice.
    world.service.select(link["id"], "opencode")
    world.append(opened, "more A", "A again")
    world.service.poll(force=True)
    assert world.service.status(link["id"])["state"] == "synced"


def _superseded_codex_with_file(world):
    original, rollout, home = _with_file(world, "codex", "thread", [("user", "q"), ("assistant", "a")])
    link = world.service.link(original)
    claude = "claude:" + world.service.open(link["id"], "claude", launch=False)["native_id"]
    world.append(claude)
    world.service.poll(force=True)
    world.service.open(link["id"], "codex", launch=False)
    world.service.poll(force=True)
    return original, rollout, link


def test_a_failed_read_of_an_earlier_session_is_retried_by_an_explicit_check(world):
    original, rollout, link = _superseded_codex_with_file(world)
    saved = world.conversations.pop(original)
    rollout.write_text("start\nchanged while unreadable\n", encoding="utf-8")
    world.service.poll(force=True)
    assert _roles(world, link["id"])[original][0] == "superseded"
    assert world.service.status(link["id"])["state"] == "error"
    world.conversations[original] = saved  # The source is readable again; its file did not change.
    world.service.poll(force=True)
    assert world.service.status(link["id"])["state"] == "synced"
    assert world.service.open(link["id"], "claude", launch=False)["reused"] is True


def test_an_earlier_session_with_its_own_file_is_still_fully_read_every_five_minutes(world):
    original, rollout, link = _superseded_codex_with_file(world)
    world.reads.clear()
    world.service.poll()
    assert original not in world.reads
    stored = world.service.store.get_link(link["id"])
    next(e for e in stored["endpoints"] if e["key"] == original)["last_full_check"] -= 301
    world.service.store.save_link(stored)
    world.service.poll()
    assert original in world.reads


def test_a_session_without_a_recorded_folder_is_not_reused_for_another_folder(world, tmp_path):
    original = world.seed()
    world.conversations[original].source.cwd = ""
    link = world.service.link(original)
    other = tmp_path / "elsewhere"
    other.mkdir()
    opened = world.service.open(link["id"], "claude", cwd=str(other), launch=False)
    assert opened["reused"] is False and opened["native_id"] != "source"


def test_an_assistant_update_does_not_break_a_link(world):
    key = world.seed()
    link = world.service.link(key)
    world.profiles["claude"].version = "9.9.9"
    world.append(key)
    result = world.service.poll(force=True)
    assert result["errors"] == []
    assert result["changed"] == 1
    assert world.service.status(link["id"])["state"] != "unlinked"
