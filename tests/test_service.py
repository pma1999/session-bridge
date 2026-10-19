import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from session_bridge.errors import BridgeError
from session_bridge.models import (
    Capability,
    Conversation,
    Event,
    NativeReceipt,
    Profile,
    SessionRef,
)
from session_bridge.service import Bridge


def test_catalog_directory_filter_is_exact_normalized_and_not_a_prefix(app, monkeypatch):
    bridge, ref, *_ = app
    refs = [
        ref.model_copy(update={"native_id": key, "cwd": cwd})
        for key, cwd in (
            ("same", "c:\\Work\\App\\"),
            ("prefix", "C:/Work/Application"),
            ("nested", "C:/Work/App/nested"),
            ("unknown", ""),
        )
    ]
    monkeypatch.setattr(bridge, "_discover", lambda _: refs)
    assert {item.native_id for item in bridge.list_sessions(cwd="C:/Work/App/./")} == {"same"}


def test_catalog_search_finds_accented_reordered_title_tokens(app, monkeypatch):
    bridge, ref, *_ = app
    ref.title = "Diseña un motor de búsqueda"
    monkeypatch.setattr(bridge, "_discover", lambda _: [ref])
    assert bridge.list_sessions(search="BUSQUEDA   motor disena")


@pytest.fixture
def app(tmp_path, monkeypatch):
    source = Profile(id="claude-test", tool="claude", home=str(tmp_path / "source"))
    target = Profile(id="opencode-test", tool="opencode", home=str(tmp_path / "target"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    ref = SessionRef(profile_id=source.id, native_id="source-id", tool="claude", cwd=str(cwd), title="Test")
    c = Conversation(
        source=ref,
        source_fingerprint="captured-state",
        events=[
            Event(id="old", kind="message", role="user", text="old discarded"),
            Event(id="s", kind="summary", role="assistant", text="original summary"),
            Event(id="u", kind="message", role="user", text="continue"),
        ],
    )
    bridge = Bridge(tmp_path / "data")
    monkeypatch.setattr(bridge, "profiles", lambda: [source, target], raising=False)
    monkeypatch.setattr(bridge, "show", lambda key, snapshot=False: c.model_copy(deep=True), raising=False)
    writer = SimpleNamespace(
        capability=lambda p: Capability(tool=p.tool, version="test", native=True, reason="mock only"),
        allocate_id=lambda p: "new-native-id",
        inspect=lambda p, i: None,
        write=lambda req: NativeReceipt(
            native_id=req.native_id,
            profile_id=req.profile.id,
            cwd=req.cwd,
            fingerprint="verified",
            created_ids=[req.native_id],
        ),
        verify=lambda p, receipt, messages: receipt,
    )
    monkeypatch.setattr(bridge, "_writer", lambda p: writer, raising=False)
    return bridge, ref, target, c, writer


def test_handoff_explicit_archive_context_separate_and_idempotent_across_restart(app):
    b, ref, target, c, writer = app
    first = b.convert(ref.key, target.id, mode="handoff")
    assert first.state == "prepared" and first.native_id is None
    context = Path(first.context_file).read_text(encoding="utf-8")
    assert "original summary" in context and "old discarded" not in context
    assert Path(first.archive).is_file() and len(c.events) == 3
    again = b.convert(ref.key, target.id, mode="handoff")
    assert again.id == first.id
    reopened = Bridge(b.data_dir)
    assert reopened.transfers()[0].id == first.id
    assert b.resume(first.id, launch=False)["mode"] == "handoff"


def test_native_requires_capability_never_silently_falls_back(app):
    b, ref, target, c, writer = app
    writer.capability = lambda p: Capability(tool=p.tool, version="new", native=False, reason="unverified")
    with pytest.raises(BridgeError) as exc:
        b.convert(ref.key, target.id)
    assert exc.value.code == "native_unavailable"
    assert b.transfers() == []


def test_uncertain_native_result_journaled_then_reconciled_without_second_write(app):
    b, ref, target, c, writer = app
    calls = []

    def failed(req):
        calls.append(req.native_id)
        raise BridgeError("import_timeout", "Tiempo de espera agotado.")

    writer.write = failed
    with pytest.raises(BridgeError):
        b.convert(ref.key, target.id)
    pending = b.transfers()[0]
    assert pending.state == "recovery" and pending.native_id == "new-native-id"
    receipt = NativeReceipt(
        native_id=pending.native_id,
        profile_id=target.id,
        cwd=ref.cwd,
        fingerprint="reconciled",
        created_ids=[pending.native_id],
        evidence={"owned": True, "operation_ids": [pending.id]},
    )
    writer.inspect = lambda p, i: receipt
    recovered = b.convert(ref.key, target.id)
    assert recovered.state == "completed" and calls == ["new-native-id"]


def test_unknown_after_timeout_does_not_blindly_retry(app):
    b, ref, target, c, writer = app
    writer.write = lambda r: (_ for _ in ()).throw(BridgeError("timeout", "Tiempo agotado."))
    with pytest.raises(BridgeError):
        b.convert(ref.key, target.id)
    with pytest.raises(BridgeError) as exc:
        b.convert(ref.key, target.id)
    assert exc.value.code == "recovery_required"


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length paths")
def test_native_target_cwd_drops_extended_length_prefix(app):
    b, ref, target, c, writer = app
    plain = c.source.cwd
    c.source.cwd = "\\\\?\\" + plain
    written = []
    write = writer.write
    writer.write = lambda req: written.append(req.cwd) or write(req)
    transfer = b.convert(ref.key, target.id)
    # Native clients key their project folders by the plain path they are launched from.
    assert written == [str(Path(plain).resolve())]
    assert transfer.state == "completed"


def test_dry_run_does_not_create_transfer_files_or_start_native_client(app):
    b, ref, target, c, writer = app
    writer.write = lambda r: pytest.fail("preview wrote native history")
    p = b.preview(ref.key, target.id)
    assert p["message_count"] == 2
    assert b.transfers() == []
    assert not (b.data_dir / "transfers").exists()


def test_related_capture_is_complete_but_not_injected_and_identity_ignores_capture_times(app, monkeypatch):
    from session_bridge.archive import read_package

    b, ref, target, c, writer = app
    child_ref = ref.model_copy(update={"native_id": "child", "parent_id": ref.native_id})
    c.related = [child_ref]
    child = Conversation(
        source=child_ref,
        source_fingerprint="child-capture",
        events=[
            Event(id="child-message", kind="message", role="assistant", text="Separate child conversation")
        ],
    )
    monkeypatch.setattr(
        b, "_reader", lambda p: SimpleNamespace(read=lambda p, r, snapshot=False: child.model_copy())
    )
    first = b.convert(ref.key, target.id, mode="handoff")
    captured = read_package(Path(first.archive))
    assert captured.related_conversations[0].events[0].text == "Separate child conversation"
    assert "Separate child conversation" not in Path(first.context_file).read_text(encoding="utf-8")
    child.captured_at = "2030-01-01T00:00:00+00:00"
    assert b.convert(ref.key, target.id, mode="handoff").id == first.id
    assert b.import_package(Path(first.archive), target.id, mode="handoff").id == first.id


def test_recovery_refuses_foreign_operation_and_changed_handoff(app):
    b, ref, target, c, writer = app
    writer.write = lambda r: (_ for _ in ()).throw(BridgeError("timeout", "Tiempo agotado."))
    with pytest.raises(BridgeError):
        b.convert(ref.key, target.id)
    t = b.transfers()[0]
    writer.inspect = lambda p, i: NativeReceipt(
        native_id=i,
        profile_id=p.id,
        cwd=ref.cwd,
        fingerprint="foreign",
        evidence={"owned": True, "operation_ids": ["other"]},
    )
    with pytest.raises(BridgeError) as exc:
        b.recover(t.id)
    assert exc.value.code == "recovery_ownership"
    handoff = b.convert(ref.key, target.id, mode="handoff")
    Path(handoff.context_file).write_text("changed", encoding="utf-8")
    with pytest.raises(BridgeError) as exc:
        b.resume(handoff.id, launch=False)
    assert exc.value.code == "handoff_changed"


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("retry", ["convert", "recover"])
def test_native_prepared_crash_can_resume_once_without_an_unjournaled_id(app, monkeypatch, legacy, retry):
    b, ref, target, c, writer = app
    original_save = b.store.save
    calls = []
    original_write = writer.write

    def interrupt(transfer):
        original_save(transfer)
        if transfer.state == "prepared":
            raise KeyboardInterrupt

    monkeypatch.setattr(b.store, "save", interrupt)
    with pytest.raises(KeyboardInterrupt):
        b.convert(ref.key, target.id)
    pending = b.transfers()[0]
    if not legacy:
        assert pending.native_id == "new-native-id"
    else:
        pending.native_id = None  # An older release could commit this state before allocation.
        original_save(pending)
    monkeypatch.setattr(b.store, "save", original_save)

    def inspect(profile, native_id):
        assert native_id == "new-native-id"
        return None

    def write(request):
        assert b.store.get(request.operation_id).state == "importing"
        calls.append(request.native_id)
        return original_write(request)

    writer.inspect = inspect
    writer.write = write
    restored = b.recover(pending.id) if retry == "recover" else b.convert(ref.key, target.id)
    assert restored.id == pending.id and restored.state == "completed"
    assert calls == ["new-native-id"]
    assert b.recover(pending.id).id == pending.id
    assert calls == ["new-native-id"]


@pytest.mark.parametrize("stage", ["journal", "context.md", "integrity.json"])
@pytest.mark.parametrize("retry", ["convert", "recover"])
def test_handoff_recovers_missing_owned_files_after_each_publication_boundary(app, monkeypatch, stage, retry):
    import session_bridge.service as service

    b, ref, target, c, writer = app
    original_save = b.store.save
    original_write = service.atomic_write

    def save(transfer):
        original_save(transfer)
        if stage == "journal":
            raise KeyboardInterrupt

    def write(path, data):
        original_write(path, data)
        if path.name == stage:
            raise KeyboardInterrupt

    monkeypatch.setattr(b.store, "save", save)
    monkeypatch.setattr(service, "atomic_write", write)
    with pytest.raises(KeyboardInterrupt):
        b.convert(ref.key, target.id, mode="handoff")
    pending = b.transfers()[0]
    context_file = Path(pending.archive).parent / "context.md"
    before = context_file.read_bytes() if context_file.exists() else None
    monkeypatch.setattr(b.store, "save", original_save)
    monkeypatch.setattr(service, "atomic_write", original_write)
    restored = b.recover(pending.id) if retry == "recover" else b.convert(ref.key, target.id, mode="handoff")
    assert restored.id == pending.id and restored.state == "prepared"
    assert Path(restored.context_file) == context_file
    assert "original summary" in context_file.read_text(encoding="utf-8")
    assert "old discarded" not in context_file.read_text(encoding="utf-8")
    if before is not None:
        assert context_file.read_bytes() == before
    assert (context_file.parent / "integrity.json").is_file()
    assert b.resume(restored.id, launch=False)["initial_message"] == restored.initial_message
    assert b.recover(restored.id).id == restored.id


def test_handoff_partial_recovery_never_overwrites_changed_context(app, monkeypatch):
    import session_bridge.service as service

    b, ref, target, c, writer = app
    original_write = service.atomic_write

    def interrupt(path, data):
        original_write(path, data)
        if path.name == "context.md":
            raise KeyboardInterrupt

    monkeypatch.setattr(service, "atomic_write", interrupt)
    with pytest.raises(KeyboardInterrupt):
        b.convert(ref.key, target.id, mode="handoff")
    pending = b.transfers()[0]
    context = Path(pending.archive).parent / "context.md"
    context.write_bytes(b"user changes after interruption")
    monkeypatch.setattr(service, "atomic_write", original_write)
    with pytest.raises(BridgeError) as exc:
        b.recover(pending.id)
    assert exc.value.code == "handoff_changed"
    assert context.read_bytes() == b"user changes after interruption"


def test_codex_resume_keeps_exact_profile_and_explicit_model_without_sending_input(app):
    b, ref, target, c, writer = app
    target.tool = "codex"
    writer.resume_argv = lambda p, i, d: ["codex", "--cd", d, "resume", i]
    transfer = b.convert(ref.key, target.id, model="gpt-6-luna")
    writer.inspect = lambda p, i: transfer.receipt
    result = b.resume(transfer.id, launch=False)
    assert result["argv"] == ["codex", "--model", "gpt-6-luna", "--cd", ref.cwd, "resume", transfer.native_id]
    assert result["env"] == {"CODEX_HOME": target.home, "CODEX_SQLITE_HOME": target.home}
    assert not result["launched"]
