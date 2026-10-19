"""Read-only capture primitives; no configuration or credential access."""

from __future__ import annotations

import io
import json
import ntpath
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from session_bridge.errors import BridgeError
from session_bridge.models import Attachment, Conversation, Event, digest


def root(profile, marker, nested):
    home = Path(profile.home).expanduser().resolve()
    return home if (home / marker).exists() or home.name == nested[-1] else home.joinpath(*nested)


def native_path(value):
    """Drop the Windows extended-length prefix that Codex records, so scope checks compare like forms."""
    value = str(value)
    if os.name != "nt":
        return value
    probe = value.replace("/", "\\")
    if probe[:8].upper() == "\\\\?\\UNC\\":
        plain = "\\\\" + probe[8:]
    elif probe.startswith("\\\\?\\"):
        plain = probe[4:]
    else:
        return value
    # Device namespaces and relative remainders keep their prefix, so the scope check refuses them.
    return plain if ntpath.isabs(plain) and ntpath.splitdrive(plain)[0] else value


def scoped(path, base):
    path = Path(native_path(path)).resolve()
    if not path.is_relative_to(Path(native_path(base)).resolve()):
        raise BridgeError("source_path_invalid", "La ruta no pertenece al perfil seleccionado.")
    return path


def guard(ref, snapshot):
    if ref.active is True and not snapshot:
        raise BridgeError("source_active", "La sesión de origen está activa; solicita una instantánea.")
    return (
        ["source_activity_unknown"]
        if ref.active is None
        else (["source_active_snapshot"] if ref.active else [])
    )


def timestamp(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000 if value > 100000000000 else value, UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            text(v)
            for v in value
            if isinstance(v, str)
            or (
                isinstance(v, dict) and v.get("type") in ("text", "input_text", "output_text", "summary_text")
            )
        )
    if isinstance(value, dict):
        return text(value.get("text", value.get("content", "")))
    return ""


MAX_SOURCE_BYTES = 1024 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_LINE_BYTES = 16 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024
MAX_ATTACHMENT_TOTAL_BYTES = 128 * 1024 * 1024


class _PrefixReader(io.RawIOBase):
    def __init__(self, source, size):
        self.source, self.remaining = source, size

    def readable(self):
        return True

    def readinto(self, target):
        data = self.source.read(min(len(target), self.remaining))
        self.remaining -= len(data)
        target[: len(data)] = data
        return len(data)


class _ZstdReader(io.RawIOBase):
    """Incremental complete-frame validation with bounded inflate chunks."""

    def __init__(self, source):
        import zstandard

        self.source, self.decoder = source, zstandard.ZstdDecompressor().decompressobj()
        self.pending, self.input_pending = bytearray(), b""
        self.total, self.finished, self.saw_frame = 0, False, False

    def readable(self):
        return True

    def readinto(self, target):
        import zstandard

        while not self.pending and not self.finished:
            chunk = self.input_pending or self.source.read(64)
            self.input_pending = b""
            if not chunk:
                if not self.decoder.eof or not self.saw_frame:
                    raise BridgeError("source_corrupt", "El archivo comprimido de origen está incompleto.")
                self.finished = True
                break
            if self.decoder.eof:
                self.decoder = zstandard.ZstdDecompressor().decompressobj()
            output = self.decoder.decompress(chunk)
            self.total += len(output)
            if self.total > MAX_DECOMPRESSED_BYTES:
                raise BridgeError(
                    "source_too_large",
                    "El archivo supera el límite de descompresión segura.",
                    limit_bytes=MAX_DECOMPRESSED_BYTES,
                )
            self.pending.extend(output)
            if self.decoder.eof:
                self.saw_frame = True
                self.input_pending = self.decoder.unused_data
        size = min(len(target), len(self.pending))
        target[:size] = self.pending[:size]
        del self.pending[:size]
        return size


def iter_records(path, *, snapshot=False, warnings=None):
    """Yield fixed-prefix JSONL records; parsed history is the only full-size retained copy."""
    warnings = warnings if warnings is not None else []
    try:
        before = path.stat()
        if before.st_size > MAX_SOURCE_BYTES:
            raise BridgeError(
                "source_too_large",
                "El archivo supera el límite de captura segura.",
                limit_bytes=MAX_SOURCE_BYTES,
            )
        with path.open("rb") as source:
            prefix = io.BufferedReader(_PrefixReader(source, before.st_size))
            stream = io.BufferedReader(_ZstdReader(prefix)) if path.suffix == ".zst" else prefix
            number, total = 0, 0
            while True:
                encoded = stream.readline(MAX_LINE_BYTES + 1)
                if not encoded:
                    break
                number += 1
                total += len(encoded)
                if len(encoded) > MAX_LINE_BYTES or total > MAX_DECOMPRESSED_BYTES:
                    raise BridgeError(
                        "source_too_large",
                        "Un registro supera el límite de captura segura.",
                        limit_bytes=MAX_LINE_BYTES
                        if len(encoded) > MAX_LINE_BYTES
                        else MAX_DECOMPRESSED_BYTES,
                    )
                line = encoded.decode("utf-8")
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                    if not isinstance(obj, dict):
                        raise TypeError
                    yield obj
                except (TypeError, ValueError):
                    yield {"_bridge_invalid_jsonl": line.rstrip("\r\n"), "line": number}
                    warnings.append("source_truncated_or_invalid_jsonl:" + str(number))
        after = path.stat()
    except BridgeError:
        raise
    except (OSError, UnicodeError) as exc:
        raise BridgeError("source_unreadable", "No se puede leer la sesión de origen.") from exc
    except Exception as exc:
        raise BridgeError("source_corrupt", "El archivo comprimido de origen está dañado.") from exc
    changed = (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    )
    if changed:
        if not snapshot:
            raise BridgeError(
                "source_changed", "La sesión cambió durante la captura; solicita una instantánea."
            )
        warnings.append("source_changed_during_snapshot")


def capture(path, *, snapshot=False):
    warnings = []
    return list(iter_records(path, snapshot=snapshot, warnings=warnings)), warnings


@contextmanager
def database(path):
    try:
        con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        yield con
    except sqlite3.Error as exc:
        raise BridgeError(
            "source_schema_unsupported", "No se puede leer el esquema de sesiones de origen."
        ) from exc
    finally:
        if "con" in locals():
            con.close()


def columns(con, table):
    # Call sites supply only literal allowlisted session tables, never user identifiers.
    return {row[1] for row in con.execute('PRAGMA table_info("' + table + '")')}


def object_json(raw, warnings):
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
        if isinstance(value, dict):
            return value
    except (TypeError, ValueError):
        pass
    warnings.append("source_invalid_json_value")
    return {}


def finish(ref, events, raw, warnings, *, ambiguous=False, related=None, attachments=None):
    return Conversation(
        source=ref,
        events=events,
        raw_records=raw,
        warnings=list(dict.fromkeys(warnings)),
        related=related or [],
        attachments=attachments or [],
        context_ambiguous=ambiguous,
        source_fingerprint=digest(
            {
                "tool": ref.tool,
                "id": ref.native_id,
                "raw": raw,
                "attachments": [a.model_dump() for a in attachments or []],
            }
        ),
    )


def _capture_managed_attachment(target):
    import hashlib

    before = target.stat()
    if before.st_size > MAX_ATTACHMENT_BYTES:
        raise BridgeError(
            "source_too_large",
            "Un adjunto gestionado supera el límite de captura segura.",
            limit_bytes=MAX_ATTACHMENT_BYTES,
        )
    with target.open("rb") as handle:
        payload = handle.read(MAX_ATTACHMENT_BYTES + 1)
    if len(payload) > MAX_ATTACHMENT_BYTES:
        raise BridgeError(
            "source_too_large",
            "Un adjunto gestionado supera el límite de captura segura.",
            limit_bytes=MAX_ATTACHMENT_BYTES,
        )
    sha = hashlib.sha256(payload).hexdigest()
    current = hashlib.sha256()
    remaining = len(payload)
    with target.open("rb") as handle:
        while remaining:
            chunk = handle.read(min(65536, remaining))
            if not chunk:
                break
            current.update(chunk)
            remaining -= len(chunk)
        grew = bool(handle.read(1))
    after = target.stat()
    if (
        remaining
        or grew
        or current.hexdigest() != sha
        or (before.st_size, before.st_mtime_ns, before.st_ino)
        != (after.st_size, after.st_mtime_ns, after.st_ino)
    ):
        raise BridgeError("source_changed", "Un adjunto gestionado cambió durante su captura.")
    return payload, sha


def attachments_in(records, base):
    """Extract native media references without fetching URLs or reading external files."""
    import base64
    import binascii
    import hashlib
    import mimetypes
    import os
    from urllib.parse import unquote, urlsplit

    attachments, seen = [], set()
    embedded_total = 0
    pending = list(reversed(records))
    managed_roots = [Path(base).resolve() / name for name in ("attachments", "files", "image-cache")]
    while pending:
        block = pending.pop()
        if isinstance(block, list):
            pending.extend(reversed(block))
            continue
        if not isinstance(block, dict):
            continue
        source = block.get("source") if isinstance(block.get("source"), dict) else {}
        kind = block.get("type")
        candidate = kind in (
            "image",
            "input_image",
            "image_url",
            "local_image",
            "input_file",
            "document",
            "file",
        )
        candidate |= bool(block.get("mime") and "data" in block and source.get("type") in ("inline", "uri"))
        if candidate:
            encoded = block.get("data") if isinstance(block.get("data"), str) else None
            encoded = source.get("data", encoded) if source.get("type") == "base64" else encoded
            media = source.get(
                "media_type", block.get("mime", block.get("media_type", "application/octet-stream"))
            )
            uri = source.get(
                "url", source.get("uri", block.get("uri", block.get("url", block.get("image_url"))))
            )
            if isinstance(uri, dict):
                uri = uri.get("url")
            local = block.get("path") if kind in ("local_image", "input_file", "file") else None
            if isinstance(uri, str) and uri.startswith("data:") and ";base64," in uri:
                header, encoded = uri.split(";base64,", 1)
                media = header[5:]
                uri = None
            if isinstance(uri, str) and uri.startswith("file:"):
                parts = urlsplit(uri)
                if not parts.netloc or parts.netloc == "localhost":
                    local = unquote(parts.path)
                    if len(local) > 2 and local[0] == "/" and local[2] == ":":
                        local = local[1:]
            identity = digest({"data": encoded, "uri": uri, "path": local, "mime": media})
            if identity not in seen and (encoded is not None or uri or local):
                seen.add(identity)
                name = block.get("name", block.get("filename")) or (
                    Path(local).name if local else "attachment"
                )
                attachment = Attachment(
                    id="attachment-" + identity[:24],
                    name=str(name),
                    media_type=media,
                    url=uri if isinstance(uri, str) else None,
                    status="remote_reference",
                )
                if encoded is not None:
                    try:
                        if len(encoded) > MAX_ATTACHMENT_BYTES * 4 // 3 + 4:
                            raise BridgeError(
                                "source_too_large",
                                "Un adjunto embebido supera el límite de captura segura.",
                                limit_bytes=MAX_ATTACHMENT_BYTES,
                            )
                        decoded = base64.b64decode(encoded, validate=True)
                        embedded_total += len(decoded)
                        if len(decoded) > MAX_ATTACHMENT_BYTES or embedded_total > MAX_ATTACHMENT_TOTAL_BYTES:
                            raise BridgeError(
                                "source_too_large",
                                "Los adjuntos superan el límite de captura segura.",
                                limit_bytes=MAX_ATTACHMENT_TOTAL_BYTES,
                            )
                        attachment.data_base64 = encoded
                        attachment.sha256 = hashlib.sha256(decoded).hexdigest()
                        attachment.status = "available"
                    except (ValueError, binascii.Error):
                        attachment.status = "invalid_embedded_data"
                elif local:
                    lexical = Path(os.path.abspath(native_path(local)))
                    target = lexical.resolve()
                    claimed_managed = any(lexical.is_relative_to(root) for root in managed_roots)
                    is_managed = any(target.is_relative_to(root) for root in managed_roots)
                    if claimed_managed and not is_managed:
                        raise BridgeError(
                            "source_path_invalid", "Un adjunto gestionado sale de las rutas permitidas."
                        )
                    if is_managed:
                        if target.is_file():
                            attachment.path = str(target)
                            attachment.managed = True
                            attachment.media_type = mimetypes.guess_type(target.name)[0] or media
                            try:
                                payload, attachment.sha256 = _capture_managed_attachment(target)
                                embedded_total += len(payload)
                                if embedded_total > MAX_ATTACHMENT_TOTAL_BYTES:
                                    raise BridgeError(
                                        "source_too_large",
                                        "Los adjuntos superan el límite de captura segura.",
                                        limit_bytes=MAX_ATTACHMENT_TOTAL_BYTES,
                                    )
                                attachment.data_base64 = base64.b64encode(payload).decode("ascii")
                                attachment.status = "available"
                            except OSError:
                                attachment.status = "unreadable"
                        else:
                            attachment.status = "missing"
                    else:
                        attachment.url = target.as_uri()
                        attachment.status = "external_reference"
                attachments.append(attachment)
        # Native serialized SQLite body columns are parsed locally, never evaluated.
        for key, value in reversed(list(block.items())):
            if isinstance(value, (dict, list)):
                pending.append(value)
            elif key in ("data", "item_json") and isinstance(value, str) and value.startswith(("{", "[")):
                try:
                    pending.append(json.loads(value))
                except ValueError:
                    pass
    return attachments


def historical_tool_text(source_format, source_kind, **values):
    """Selected native fields rendered as inert historical data, without provider replay state."""
    return json.dumps(
        {
            "source_format": source_format,
            "source_kind": source_kind,
            "historical_observation": True,
            **values,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def block_events(
    key, content, *, role, kind, active, native, source_format, parent_id=None, timestamp=None, branch=None
):
    """Keep native content-block order; every emitted block shares its record graph activity."""
    base = {
        "role": role,
        "kind": kind,
        "active": active,
        "native": native,
        "parent_id": parent_id,
        "timestamp": timestamp,
        "branch": branch,
    }
    if not isinstance(content, list) or not content:
        return [Event(id=key, text=text(content), **base)]
    result, summary_seen = [], False
    for index, block in enumerate(content):
        values = dict(base)
        block_kind = (
            block.get("type") if isinstance(block, dict) else "text" if isinstance(block, str) else None
        )
        if block_kind in ("tool_use", "tool_result", "tool", "tool-invocation"):
            values["kind"], values["role"] = "tool", "tool"
            if block_kind == "tool_use":
                description = historical_tool_text(
                    source_format,
                    block_kind,
                    tool=block.get("name"),
                    call_id=block.get("id"),
                    arguments=block.get("input"),
                )
            elif block_kind == "tool_result":
                description = historical_tool_text(
                    source_format,
                    block_kind,
                    call_id=block.get("tool_use_id"),
                    result=block.get("content"),
                    is_error=block.get("is_error", False),
                )
            else:
                state = block.get("state") if isinstance(block.get("state"), dict) else {}
                description = historical_tool_text(
                    source_format,
                    block_kind,
                    tool=block.get("name", block.get("tool")),
                    call_id=block.get("id", block.get("callID")),
                    arguments=state.get("input", block.get("args")),
                    result=state.get("content", state.get("output", block.get("result"))),
                    error=state.get("error"),
                )
        elif block_kind in ("text", "input_text", "output_text", "summary_text"):
            description = text(block)
            if kind == "summary":
                values["kind"] = "message" if summary_seen else "summary"
                summary_seen = True
        elif block_kind in ("thinking", "reasoning", "redacted_thinking"):
            values["kind"] = "reasoning"
            description = ""  # Native thoughts/signatures remain in the original archive record only.
        else:
            values["kind"] = "metadata"
            description = ""
        result.append(
            Event(id=key if index == 0 else key + ":block:" + str(index), text=description, **values)
        )
    return result
