"""Portable archive boundary. Members are validated before any extraction."""

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path

from pydantic import ValidationError

from . import __version__
from .errors import BridgeError
from .models import Attachment, Conversation, canonical, digest, file_digest

MAX_ENTRIES = 4096
MAX_MEMBER = 1024 * 1024 * 1024
MAX_TOTAL = 2 * 1024 * 1024 * 1024
MAX_RATIO = 5000


def atomic_write(path: Path, data: bytes) -> None:
    """Publish a complete file exclusively, never replacing a concurrent creator."""
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise BridgeError("already_exists", path=str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sbridge-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise BridgeError("already_exists", path=str(path)) from exc
    finally:
        Path(temporary).unlink(missing_ok=True)


def _json(data: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(data, object_pairs_hook=unique)


def _conversations(conversation):
    pending = [("", conversation, 0)]
    seen = set()
    while pending:
        scope, item, depth = pending.pop()
        if depth > 16 or len(seen) >= 512 or id(item) in seen:
            raise BridgeError("package_limit", "error.package_limit.tree")
        seen.add(id(item))
        yield scope, item
        for index, child in reversed(list(enumerate(item.related_conversations))):
            pending.append((f"{scope}related/{index}/", child, depth + 1))


def _attachments(conversation):
    for scope, item in _conversations(conversation):
        for attachment in item.attachments:
            yield scope + attachment.id, attachment, item


def write_package(conversation: Conversation, output: Path, *, include_files=None) -> dict:
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise BridgeError("already_exists", path=str(output))
    explicit = {Path(p).resolve(strict=True) for p in (include_files or [])}
    copied = conversation.model_copy(deep=True)
    referenced = {Path(a.path).resolve() for _, a, _ in _attachments(copied) if a.path}
    for file in sorted(explicit - referenced):
        if not file.is_file():
            raise BridgeError("invalid_attachment", "error.invalid_attachment.regular_files_only")
        copied.attachments.append(
            Attachment(id="included-" + digest(str(file))[:24], name=file.name, path=str(file), managed=False)
        )
    attachment_keys = [key for key, _, _ in _attachments(copied)]
    if len(set(attachment_keys)) != len(attachment_keys):
        raise BridgeError("attachment_identity")
    members: dict[str, bytes] = {}
    attachment_members = {}
    # A managed source file must remain under the native transcript's directory. Caller-supplied
    # external paths are included only by an exact explicit --include-file choice.
    for attachment_key, attachment, item in _attachments(copied):
        locator = Path(item.source.locator) if item.source.locator else None
        managed_root = locator.parent.resolve() if locator and locator.is_file() else None
        data = None
        if attachment.data_base64 is not None:
            if len(attachment.data_base64) > MAX_MEMBER * 4 // 3 + 4:
                raise BridgeError("package_limit", "error.package_limit.attachment")
            try:
                data = base64.b64decode(attachment.data_base64, validate=True)
            except ValueError as exc:
                raise BridgeError("invalid_attachment", "error.invalid_attachment.embedded") from exc
        elif attachment.path:
            file = Path(attachment.path)
            if file.is_file():
                resolved = file.resolve()
                allowed = resolved in explicit or (
                    attachment.managed
                    and managed_root is not None
                    and resolved.is_relative_to(managed_root)
                    and not file.is_symlink()
                )
                if allowed:
                    if file.stat().st_size > MAX_MEMBER:
                        raise BridgeError("package_limit", "error.package_limit.attachment")
                    data = file.read_bytes()
        attachment.path = None
        attachment.managed = False
        if data is None:
            attachment.status = "remote_reference" if attachment.url else "not_included"
            continue
        if len(data) > MAX_MEMBER:
            raise BridgeError("package_limit", "error.package_limit.attachment")
        sha = hashlib.sha256(data).hexdigest()
        member = "attachments/" + sha
        members[member] = data
        attachment_members[attachment_key] = member
        attachment.sha256 = sha
        attachment.data_base64 = None
        attachment.status = "available"
    members["conversation.json"] = canonical(copied.model_dump(mode="json"))
    if (
        len(members) >= MAX_ENTRIES
        or sum(map(len, members.values())) > MAX_TOTAL
        or any(len(data) > MAX_MEMBER for data in members.values())
    ):
        raise BridgeError("package_limit", "error.package_limit.resources")
    manifest = {
        "format": "session-bridge",
        "schema_version": 1,
        "created_by": __version__,
        "source": conversation.source.key,
        "captured_at": conversation.captured_at,
        "files": {
            name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            for name, data in members.items()
        },
        "attachments": attachment_members,
    }
    # Build privately in the destination filesystem, then publish without replacing a file.
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".sbridge-", dir=output.parent)
    os.close(fd)
    temporary = Path(temp_name)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("manifest.json", canonical(manifest))
            for name, data in members.items():
                archive.writestr(name, data)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        try:
            os.link(temporary, output)
        except FileExistsError as exc:
            raise BridgeError("already_exists", path=str(output)) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "path": str(output),
        "schema_version": 1,
        "sha256": file_digest(output),
        "attachments": len(attachment_members),
        "events": len(copied.events),
    }


def read_package(path: Path, *, attachment_dir: Path | None = None) -> Conversation:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(infos) > MAX_ENTRIES or len(set(names)) != len(names):
                raise ValueError("duplicate or excessive members")
            total = 0
            for info in infos:
                name = info.filename
                mode = info.external_attr >> 16
                if name not in {"manifest.json", "conversation.json"} and not re.fullmatch(
                    r"attachments/[0-9a-f]{64}", name
                ):
                    raise ValueError("unsafe member")
                if (
                    stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                    or info.flag_bits & 1
                    or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or info.file_size > MAX_MEMBER
                    or info.file_size > max(1, info.compress_size) * MAX_RATIO
                ):
                    raise ValueError("unsupported member")
                total += info.file_size
            if total > MAX_TOTAL or "manifest.json" not in names:
                raise ValueError("package limit or missing manifest")
            if archive.getinfo("manifest.json").file_size > 2 * 1024 * 1024:
                raise ValueError("manifest limit")
            manifest = _json(archive.read("manifest.json"))
            if (
                manifest.get("format") != "session-bridge"
                or type(manifest.get("schema_version")) is not int
                or manifest.get("schema_version") != 1
            ):
                raise BridgeError("package_version", "error.package_version.package")
            files = manifest["files"]
            if set(files) != set(names) - {"manifest.json"}:
                raise ValueError("unlisted member")
            verified = {}
            for name, spec in files.items():
                data = archive.read(name)
                if spec["size"] != len(data) or spec["sha256"] != hashlib.sha256(data).hexdigest():
                    raise ValueError("integrity failure")
                verified[name] = data
            conversation = Conversation.model_validate(_json(verified["conversation.json"]))
            if conversation.schema_version != 1:
                raise BridgeError("package_version", "error.package_version.conversation")
            assets = manifest.get("attachments", {})
            entries = list(_attachments(conversation))
            for _, item in _conversations(conversation):
                if item.schema_version != 1:
                    raise BridgeError("package_version", "error.package_version.related")
            ids = [key for key, _, _ in entries]
            if len(set(ids)) != len(ids) or set(assets) - set(ids):
                raise ValueError("invalid attachment identity")
            for attachment_key, attachment, _ in entries:
                attachment.path = None  # A foreign package never authorizes reading a local path.
                attachment.managed = False
                if attachment_key not in assets:
                    continue
                member = assets[attachment_key]
                if member not in verified or not member.startswith("attachments/"):
                    raise ValueError("missing attachment")
                data = verified[member]
                if hashlib.sha256(data).hexdigest() != attachment.sha256:
                    raise ValueError("attachment integrity")
            # No filesystem extraction until every member and reference has passed validation.
            if attachment_dir is not None:
                root = Path(attachment_dir)
                if root.is_symlink():
                    raise ValueError("symlink extraction root")
                root.mkdir(parents=True, exist_ok=True)
                for attachment_key, attachment, _ in entries:
                    if attachment_key not in assets:
                        continue
                    member = assets[attachment_key]
                    target = root / member.split("/")[-1]
                    data = verified[member]
                    if target.is_symlink():
                        raise ValueError("symlink destination")
                    if target.exists():
                        if target.read_bytes() != data:
                            raise ValueError("changed destination")
                    else:
                        atomic_write(target, data)
                    attachment.path = str(target.resolve())
                    attachment.managed = True
                    attachment.status = "available"
            else:
                for attachment_key, attachment, _ in entries:
                    if attachment_key in assets:
                        attachment.data_base64 = base64.b64encode(verified[assets[attachment_key]]).decode(
                            "ascii"
                        )
            return conversation
    except BridgeError:
        raise
    except (
        ValueError,
        KeyError,
        TypeError,
        OSError,
        zipfile.BadZipFile,
        ValidationError,
        RecursionError,
        AttributeError,
    ) as exc:
        raise BridgeError("invalid_package") from exc
