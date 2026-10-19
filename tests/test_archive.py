import hashlib
import json
import stat
import zipfile
from pathlib import Path

import pytest

from session_bridge.archive import read_package, write_package
from session_bridge.errors import BridgeError
from session_bridge.models import Attachment, Conversation, Event, SessionRef


def sample():
    return Conversation(
        source=SessionRef(profile_id="codex-win", native_id="uuid", tool="codex"),
        events=[Event(id="u", kind="message", role="user", text="你好 á 🚀")],
        raw_records=[{"future_field": {"original": True}}],
    )


def test_archive_has_version_integrity_and_preserves_unrecognized_native_fields(tmp_path):
    path = tmp_path / "test.sbridge"
    c = sample()
    write_package(c, path)
    assert read_package(path).model_dump() == c.model_dump()
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["schema_version"] == 1
        assert (
            manifest["files"]["conversation.json"]["sha256"]
            == hashlib.sha256(z.read("conversation.json")).hexdigest()
        )


@pytest.mark.parametrize(
    "member",
    [
        "../escape",
        "/absolute",
        "C:/escape",
        "attachments/../../x",
        "attachments\\escape",
        "conversation.json",
    ],
)
def test_reject_unsafe_extra_or_duplicate_members_before_extract(tmp_path, member):
    path = tmp_path / "bad.sbridge"
    write_package(sample(), path)
    with zipfile.ZipFile(path, "a") as z:
        z.writestr(member, "malicious")
    with pytest.raises(BridgeError):
        read_package(path, attachment_dir=tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_reject_symlink_and_modified_content(tmp_path):
    path = tmp_path / "bad.sbridge"
    write_package(sample(), path)
    with zipfile.ZipFile(path, "a") as z:
        info = zipfile.ZipInfo("attachments/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "outside")
    with pytest.raises(BridgeError):
        read_package(path)


def test_no_overwrite_and_explicit_attachment_inclusion(tmp_path):
    outside = tmp_path / "external.txt"
    outside.write_text("private", encoding="utf-8")
    c = sample()
    c.attachments = [Attachment(id="ext", name="external.txt", path=str(outside), managed=False)]
    path = tmp_path / "full.sbridge"
    write_package(c, path)
    imported = read_package(path)
    assert imported.attachments[0].path is None
    assert imported.attachments[0].status == "not_included"
    with pytest.raises(BridgeError):
        write_package(c, path)
    full = tmp_path / "explicit.sbridge"
    write_package(c, full, include_files=[outside])
    extracted = read_package(full, attachment_dir=tmp_path / "assets")
    assert Path(extracted.attachments[0].path).read_text(encoding="utf-8") == "private"


def test_related_conversations_preserve_assets_and_scrub_foreign_paths_recursively(tmp_path):
    import base64

    root = sample()
    child = sample()
    child.source.native_id = "child"
    child.attachments = [
        Attachment(id="asset", name="file", data_base64=base64.b64encode(b"child-data").decode()),
        Attachment(id="reference", name="external", path="C:/sensitive/auth.json", managed=True),
    ]
    root.related_conversations = [child]
    path = tmp_path / "family.sbridge"
    write_package(root, path)
    result = read_package(path)
    assert result.related_conversations[0].events[0].text == root.events[0].text
    assert base64.b64decode(result.related_conversations[0].attachments[0].data_base64) == b"child-data"
    assert result.related_conversations[0].attachments[1].path is None
    assert not result.related_conversations[0].attachments[1].managed


@pytest.mark.parametrize("related", [False, True])
def test_duplicate_attachment_ids_are_rejected_even_without_available_payloads(tmp_path, related):
    root = sample()
    owner = root
    if related:
        owner = sample()
        root.related_conversations = [owner]
    owner.attachments = [
        Attachment(id="duplicate", name="remote", url="https://example.invalid/file"),
        Attachment(id="duplicate", name="missing", path=str(tmp_path / "absent-file")),
    ]
    output = tmp_path / "invalid.sbridge"
    with pytest.raises(BridgeError) as exc:
        write_package(root, output)
    assert exc.value.code == "attachment_identity"
    assert not output.exists()
