import hashlib
import json
import os
import random
import stat
import threading
import zlib
from pathlib import Path

import pytest

from session_bridge import sync_store as sync_store_module
from session_bridge.errors import BridgeError
from session_bridge.models import Attachment, Conversation, Event, SessionRef
from session_bridge.sync_store import SyncStore


def conversation(text="hello"):
    child = Conversation(
        source=SessionRef(profile_id="claude-win", native_id="child", tool="claude"),
        events=[Event(id="child-event", kind="unknown", native={"future": [1, 2]})],
        raw_records=[{"opaque": {"binary_hint": "retained"}}],
    )
    return Conversation(
        source=SessionRef(
            profile_id="codex-win", native_id="session", tool="codex", metadata={"future": True}
        ),
        captured_at="2026-10-19T12:00:00+00:00",
        events=[Event(id="same", kind="message", role="user", text=text, native={"vendor": {"x": 1}})],
        related=[SessionRef(profile_id="opencode-win", native_id="other", tool="opencode")],
        related_conversations=[child],
        attachments=[Attachment(id="file", name="payload.bin", data_base64="AAECAw==", sha256="fake")],
        warnings=["reader warning"],
        context_ambiguous=True,
        source_fingerprint="fingerprint",
        raw_records=[{"new_native_field": {"keep": True}}],
    )


def test_capture_round_trip_preserves_full_tree_and_deduplicates_payloads(tmp_path):
    store = SyncStore(tmp_path)
    original = conversation()
    first = store.put_capture(original.model_dump(mode="json"))
    with store.transaction() as db:
        blobs_after_first = db.execute("SELECT count(*) FROM blobs").fetchone()[0]
    second = store.put_capture(original.model_dump(mode="json"))

    assert first == second
    assert store.get_capture(first) == original.model_dump(mode="json")
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM blobs").fetchone()[0] == blobs_after_first
        assert db.execute("SELECT count(*) FROM captures").fetchone()[0] == 1


def test_appending_after_10000_events_reuses_reference_chunks_and_blobs(tmp_path):
    store = SyncStore(tmp_path)
    source = SessionRef(profile_id="codex-win", native_id="long", tool="codex")
    events = [
        Event(id=f"e{i}", kind="message", text="unchanged").model_dump(mode="json") for i in range(10_000)
    ]
    base = Conversation(
        source=source, captured_at="fixed", events=[Event.model_validate(item) for item in events]
    )
    base_id = store.put_capture(base.model_dump(mode="json"))
    with store.connect() as db:
        before = db.execute("SELECT count(*) FROM blobs").fetchone()[0]
        base_manifest = json.loads(
            db.execute("SELECT manifest FROM captures WHERE id=?", (base_id,)).fetchone()[0]
        )

    appended = base.model_copy(deep=True)
    appended.events.append(Event(id="e10000", kind="message", text="new"))
    appended_id = store.put_capture(appended.model_dump(mode="json"))
    with store.connect() as db:
        after = db.execute("SELECT count(*) FROM blobs").fetchone()[0]
        new_manifest = json.loads(
            db.execute("SELECT manifest FROM captures WHERE id=?", (appended_id,)).fetchone()[0]
        )
        new_chunk_count = after - before

    old_refs = base_manifest["_sync_collections"]["events"]
    new_refs = new_manifest["_sync_collections"]["events"]
    assert len(old_refs) == len(new_refs)
    assert old_refs[:-1] == new_refs[:-1]
    assert all(isinstance(ref, str) for ref in old_refs)
    assert new_chunk_count <= 2
    assert len(store.get_capture(appended_id)["events"]) == 10_001


def test_appending_after_10000_context_messages_reuses_reference_chunks(tmp_path):
    store = SyncStore(tmp_path)
    messages = [
        {"id": f"m{i}", "role": "user", "text": "unchanged", "origin": {"key": "a", "event_ids": [str(i)]}}
        for i in range(10_000)
    ]
    refs = store.put_context(messages)
    with store.connect() as db:
        before = db.execute("SELECT count(*) FROM blobs").fetchone()[0]
        old_manifest = json.loads(db.execute("SELECT manifest FROM contexts WHERE id=?", refs).fetchone()[0])
    appended_refs = store.put_context(
        messages
        + [{"id": "m10000", "role": "assistant", "text": "new", "origin": {"key": "a", "event_ids": []}}]
    )
    with store.connect() as db:
        after = db.execute("SELECT count(*) FROM blobs").fetchone()[0]
        new_manifest = json.loads(
            db.execute("SELECT manifest FROM contexts WHERE id=?", appended_refs).fetchone()[0]
        )
    assert old_manifest["chunks"][:-1] == new_manifest["chunks"][:-1]
    assert all(isinstance(ref, str) for ref in old_manifest["chunks"])
    assert after - before <= 2
    assert len(store.get_context(appended_refs)) == 10_001


def test_context_keeps_order_and_duplicate_messages(tmp_path):
    store = SyncStore(tmp_path)
    messages = [
        {"id": "one", "role": "user", "text": "repeat", "origin": {"key": "a", "event_ids": ["e"]}},
        {"id": "two", "role": "assistant", "text": "repeat", "origin": {"key": "a", "event_ids": ["f"]}},
    ]
    refs = store.put_context(messages)
    assert store.get_context(refs) == messages
    assert store.get_context(refs + refs) == messages + messages


def test_transactions_roll_back_and_revision_ids_are_immutable(tmp_path):
    store = SyncStore(tmp_path)
    capture_id = store.put_capture(conversation().model_dump(mode="json"))
    revision = {
        "id": "r1",
        "link_id": "l1",
        "parent_id": None,
        "source_key": "p:s",
        "capture": capture_id,
        "context": [],
        "created_at": "now",
        "reason": "initial",
    }
    store.put_revision(revision)
    store.put_revision(revision)
    with pytest.raises(BridgeError) as exc:
        store.put_revision({**revision, "reason": "different"})
    assert exc.value.code == "sync_revision_conflict"

    with pytest.raises(RuntimeError):
        with store.transaction() as db:
            store.save_link(
                {
                    "id": "l1",
                    "name": "name",
                    "endpoints": [],
                    "heads": [],
                    "selected_head": None,
                    "paused": False,
                    "unlinked": False,
                    "last_checked": None,
                    "last_error": None,
                },
                db=db,
            )
            raise RuntimeError("rollback")
    assert store.links() == []
    assert store.revisions("l1") == [revision]


def test_links_resolve_exact_id_name_or_unique_prefix_and_reject_ambiguity(tmp_path):
    store = SyncStore(tmp_path)
    for ident, name in [("alpha-1", "Alpha"), ("alpha-2", "Beta")]:
        store.save_link(
            {
                "id": ident,
                "name": name,
                "endpoints": [],
                "heads": [],
                "selected_head": None,
                "paused": False,
                "unlinked": False,
                "last_checked": None,
                "last_error": None,
            }
        )
    assert store.get_link("Alpha")["id"] == "alpha-1"
    with pytest.raises(BridgeError) as exc:
        store.get_link("alpha-")
    assert exc.value.code == "sync_ambiguous"
    with pytest.raises(BridgeError) as exc:
        store.save_link(
            {
                "id": "gamma",
                "name": "alpha",
                "endpoints": [],
                "heads": [],
                "selected_head": None,
                "paused": False,
                "unlinked": False,
                "last_checked": None,
                "last_error": None,
            }
        )
    assert exc.value.code == "sync_link_conflict"


def test_rejects_unknown_schema_version(tmp_path):
    store = SyncStore(tmp_path)
    with store.transaction() as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(BridgeError) as exc:
        SyncStore(tmp_path)
    assert exc.value.code == "sync_store_version"


def test_corrupt_or_oversized_decompressed_blob_fails_content_free(tmp_path, monkeypatch):
    store = SyncStore(tmp_path)
    capture_id = store.put_capture(conversation().model_dump(mode="json"))
    monkeypatch.setattr(SyncStore, "MAX_BLOB_BYTES", 32)
    with store.transaction() as db:
        row = db.execute("SELECT hash, data FROM blobs LIMIT 1").fetchone()
        db.execute("UPDATE blobs SET data=? WHERE hash=?", (zlib.compress(b"x" * 1000), row[0]))
    with pytest.raises(BridgeError) as exc:
        store.get_capture(capture_id)
    assert exc.value.code == "sync_blob_invalid"
    assert "hello" not in str(exc.value)


def test_oversized_packed_blob_is_rejected_before_payload_fetch_or_decompression(tmp_path, monkeypatch):
    store = SyncStore(tmp_path)
    monkeypatch.setattr(SyncStore, "MAX_BLOB_BYTES", 128)
    packed = b"x" * 1024
    key = hashlib.sha256(b"expected").hexdigest()
    statements = []
    decompression_calls = []
    real_decompressobj = sync_store_module.zlib.decompressobj

    def track_decompressobj(*args, **kwargs):
        decompression_calls.append(True)
        return real_decompressobj(*args, **kwargs)

    monkeypatch.setattr(sync_store_module.zlib, "decompressobj", track_decompressobj)
    with store.transaction() as db:
        db.execute("INSERT INTO blobs(hash,raw_size,data) VALUES(?,?,?)", (key, 8, packed))
        db.set_trace_callback(statements.append)
        with pytest.raises(BridgeError) as exc:
            store._get_blob(key, db)

    assert exc.value.code == "sync_blob_invalid"
    assert not decompression_calls
    assert not any("SELECT raw_size,data FROM blobs" in statement for statement in statements)


def test_near_limit_incompressible_zlib_blob_is_accepted(tmp_path, monkeypatch):
    store = SyncStore(tmp_path)
    max_blob_bytes = 1024 * 1024
    monkeypatch.setattr(SyncStore, "MAX_BLOB_BYTES", max_blob_bytes)
    raw = random.Random(9281).randbytes(max_blob_bytes)
    with store.transaction() as db:
        identifier = store._put_blob(raw, db)
        assert store._get_blob(identifier, db) == raw


def test_trailing_compressed_stream_is_rejected(tmp_path):
    store = SyncStore(tmp_path)
    raw = b"valid body"
    key = hashlib.sha256(raw).hexdigest()
    packed = zlib.compress(raw) + zlib.compress(b"second stream")
    with store.transaction() as db:
        db.execute("INSERT INTO blobs(hash,raw_size,data) VALUES(?,?,?)", (key, len(raw), packed))
        with pytest.raises(BridgeError) as exc:
            store._get_blob(key, db)
    assert exc.value.code == "sync_blob_invalid"


def test_attachment_payload_counts_toward_aggregate_capture_read_limit(tmp_path, monkeypatch):
    store = SyncStore(tmp_path)
    value = {
        "schema_version": 1,
        "source": {"profile_id": "codex-win", "native_id": "minimal", "tool": "codex"},
        "captured_at": "fixed",
        "attachments": [{"id": "a", "name": "a.bin", "data_base64": "A" * 512}],
    }
    capture_id = store.put_capture(value)
    monkeypatch.setattr(SyncStore, "MAX_CAPTURE_BYTES", 300)
    with pytest.raises(BridgeError) as exc:
        store.get_capture(capture_id)
    assert exc.value.code == "sync_blob_invalid"


def test_backup_is_exclusive_validated_and_restores_only_to_empty_root(tmp_path):
    store = SyncStore(tmp_path / "source")
    original = conversation()
    capture_id = store.put_capture(original.model_dump(mode="json"))
    backup = tmp_path / "sync # ü.sqlite3"
    result = store.backup(backup)
    assert result["path"] == str(backup)
    assert result["sha256"] == hashlib.sha256(backup.read_bytes()).hexdigest()
    assert SyncStore.validate_backup(backup)["schema_version"] == 1
    restored = SyncStore.restore(backup, tmp_path / "restored")
    assert restored.get_capture(capture_id) == original.model_dump(mode="json")
    with pytest.raises(BridgeError):
        store.backup(backup)
    with pytest.raises(BridgeError):
        SyncStore.restore(backup, tmp_path / "source")


def test_backup_failure_never_publishes_empty_or_partial_output(tmp_path, monkeypatch):
    store = SyncStore(tmp_path / "source")
    output = tmp_path / "backup # ? unicode-é.sqlite3"

    def fail_validation(path):
        raise BridgeError("test", "failure")

    monkeypatch.setattr(store, "validate_backup", fail_validation)
    with pytest.raises(BridgeError):
        store.backup(output)
    assert not output.exists()
    assert list(tmp_path.glob(".sync-backup-*.tmp")) == []


def test_backup_publication_does_not_replace_a_concurrent_file(tmp_path, monkeypatch):
    store = SyncStore(tmp_path / "source")
    output = tmp_path / "backup.sqlite3"
    primitive = "rename" if os.name == "nt" else "link"
    real_publish = getattr(os, primitive)

    def race_publish(source, destination, *args, **kwargs):
        Path(destination).write_bytes(b"other writer")
        return real_publish(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, primitive, race_publish)
    with pytest.raises(BridgeError):
        store.backup(output)
    assert output.read_bytes() == b"other writer"
    assert list(tmp_path.glob(".sync-backup-*.tmp")) == []


def test_restore_publication_does_not_replace_a_concurrent_database(tmp_path, monkeypatch):
    source = SyncStore(tmp_path / "source")
    backup = tmp_path / "backup.sqlite3"
    source.backup(backup)
    destination = tmp_path / "restored"
    destination.mkdir()
    primitive = "rename" if os.name == "nt" else "link"
    real_publish = getattr(os, primitive)

    def race_publish(temp, final, *args, **kwargs):
        Path(final).write_bytes(b"other writer")
        return real_publish(temp, final, *args, **kwargs)

    monkeypatch.setattr(os, primitive, race_publish)
    with pytest.raises(BridgeError):
        SyncStore.restore(backup, destination)
    assert (destination / "sync.sqlite3").read_bytes() == b"other writer"
    assert list(destination.glob(".sync.sqlite3.restore-*")) == []


def test_restore_refuses_symlink_root_before_creating_or_writing_it(tmp_path, monkeypatch):
    source = SyncStore(tmp_path / "source")
    backup = tmp_path / "backup.sqlite3"
    source.backup(backup)
    root = tmp_path / "symlink-root"
    root.mkdir()
    real_is_symlink = Path.is_symlink

    def report_root_as_symlink(path):
        return path == root or real_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", report_root_as_symlink)
    with pytest.raises(BridgeError) as exc:
        SyncStore.restore(backup, root)

    assert exc.value.code == "sync_restore_unsafe"
    assert list(root.iterdir()) == []


def test_restore_refuses_windows_reparse_point_root(tmp_path, monkeypatch):
    source = SyncStore(tmp_path / "source")
    backup = tmp_path / "backup.sqlite3"
    source.backup(backup)
    root = tmp_path / "junction-root"
    root.mkdir()
    real_lstat = Path.lstat

    class ReparseStat:
        st_mode = stat.S_IFDIR
        st_file_attributes = 0x400

    def report_root_as_junction(path):
        return ReparseStat() if path == root else real_lstat(path)

    monkeypatch.setattr(Path, "lstat", report_root_as_junction)
    with pytest.raises(BridgeError) as exc:
        SyncStore.restore(backup, root)

    assert exc.value.code == "sync_restore_unsafe"
    assert list(root.iterdir()) == []


def test_os_lock_reports_overlap_and_releases_after_exception(tmp_path):
    store = SyncStore(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def owner():
        with store.lock():
            entered.set()
            release.wait(3)

    thread = threading.Thread(target=owner)
    thread.start()
    assert entered.wait(3)
    try:
        with pytest.raises(BridgeError) as exc:
            with store.lock():
                pass
        assert exc.value.code == "sync_busy"
    finally:
        release.set()
        thread.join(3)
    with store.lock():
        pass
