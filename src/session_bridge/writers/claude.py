"""Claude's private JSONL format, enabled by the version gates in ``registry``."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from session_bridge.archive import atomic_write
from session_bridge.errors import BridgeError
from session_bridge.models import NativeReceipt, digest, utc_now

from .common import validate_id

VERSION = "2.1.304"


def transcript(request):
    validate_id("claude", request.native_id)
    parent, rows, stamp = None, [], utc_now()
    for index, message in enumerate(request.messages):
        item_id = str(uuid.uuid5(uuid.UUID(request.native_id), str(index)))
        payload = {"role": message.role, "content": [{"type": "text", "text": message.text}]}
        if message.role == "assistant":
            payload.update(
                {
                    "id": f"msg_{item_id.replace('-', '')}",
                    "type": "message",
                    "model": request.model or "imported",
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            )
        rows.append(
            {
                "type": message.role,
                "message": payload,
                "uuid": item_id,
                "parentUuid": parent,
                "sessionId": request.native_id,
                "isSidechain": False,
                "cwd": request.cwd,
                "version": VERSION,
                "timestamp": stamp,
                "sessionBridge": {"operation_id": request.operation_id, "imported": True},
            }
        )
        parent = item_id
    if request.title:
        rows.append({"type": "custom-title", "customTitle": request.title, "sessionId": request.native_id})
    return rows


# How many recent sessions, and how much of each, the format check reads.
_SAMPLE_FILES = 50
_SAMPLE_BYTES = 4 * 1024 * 1024


def _message_row(row):
    """Whether ``row`` is a message with every field ``transcript`` writes, in the same shape."""
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    blocks_ok = isinstance(content, list) and all(
        isinstance(block, dict) and isinstance(block.get("type"), str) for block in content
    )
    return (
        row.get("type") in {"user", "assistant"}
        and isinstance(message, dict)
        and message.get("role") == row["type"]
        and (isinstance(content, str) or blocks_ok)
        and all(isinstance(row.get(name), str) for name in ("uuid", "sessionId", "cwd", "timestamp"))
        and "parentUuid" in row
        and (row["parentUuid"] is None or isinstance(row["parentUuid"], str))
        and isinstance(row.get("isSidechain"), bool)
    )


def _recent_sessions(home):
    """The most recently changed session files, skipping links and files that vanish meanwhile."""
    root = Path(home) / "projects"
    found = []
    try:
        candidates = list(root.glob("*/*.jsonl")) if root.is_dir() else []
    except OSError:
        return []
    for path in candidates:
        try:
            if path.is_symlink() or path.parent.is_symlink():
                continue
            found.append((path.stat().st_mtime, path))
        except OSError:
            continue
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found[:_SAMPLE_FILES]]


def check_format(profile, installed):
    """Refuse when the sessions this Claude Code version wrote no longer have the shape ``transcript`` writes.

    It reads the start and the end of the most recent sessions, keeps the rows the installed version wrote and
    needs one message row in our shape among them. When that version has not written a session yet
    there is nothing to compare, and the write goes ahead.
    """
    rows = []
    for path in _recent_sessions(profile.home):
        try:
            with path.open("rb") as handle:
                data = handle.read(_SAMPLE_BYTES)
                # A session continued in the new version has its newest rows at the end.
                size = handle.seek(0, 2)
                if size > 2 * _SAMPLE_BYTES:
                    handle.seek(size - _SAMPLE_BYTES)
                    data += b"\n" + handle.read(_SAMPLE_BYTES)
                elif size > _SAMPLE_BYTES:
                    handle.seek(_SAMPLE_BYTES)
                    data += handle.read()
        except OSError:
            continue
        text = data.decode("utf-8", errors="replace")
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue  # Also the last line, when the read stopped inside it.
            if isinstance(row, dict) and "sessionBridge" not in row and row.get("version") == installed:
                rows.append(row)
    if rows and not any(_message_row(row) for row in rows):
        raise BridgeError("native_format_changed", version=installed)


def locate(profile, native_id):
    validate_id("claude", native_id)
    root = Path(profile.home) / "projects"
    candidates = list(root.glob(f"*/{native_id}.jsonl")) if root.exists() else []
    if len(candidates) > 1:
        raise BridgeError("native_ambiguous", "error.native_ambiguous.same_identifier")
    if not candidates:
        return None
    target = candidates[0]
    if target.is_symlink() or not target.resolve().is_relative_to(Path(profile.home).resolve()):
        raise BridgeError("unsafe_native_path", "error.unsafe_native_path.path")
    return target


def read_rows(profile, native_id):
    path = locate(profile, native_id)
    if path is None:
        return None
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("invalid native row")
    except (ValueError, OSError):
        raise BridgeError("native_corrupt", "error.native_corrupt.record") from None
    return path, rows


def inspect(profile, native_id):
    value = read_rows(profile, native_id)
    if value is None:
        return None
    path, rows = value
    subtree = {path.name: path.read_bytes().hex()}
    children = path.with_suffix("")
    if children.exists():
        if children.is_symlink() or not children.resolve().is_relative_to(Path(profile.home).resolve()):
            raise BridgeError("unsafe_native_path", "error.unsafe_native_path.subtree")
        for item in sorted(children.rglob("*")):
            if item.is_symlink() or not item.resolve().is_relative_to(Path(profile.home).resolve()):
                raise BridgeError("unsafe_native_path", "error.unsafe_native_path.symlink")
            if item.is_file():
                subtree[str(item.relative_to(path.parent))] = item.read_bytes().hex()
    provenance = [row.get("sessionBridge") for row in rows if row.get("type") in {"user", "assistant"}]
    owned = bool(provenance) and all(isinstance(p, dict) and p.get("imported") is True for p in provenance)
    cwd = next((row.get("cwd", "") for row in rows if row.get("cwd")), "")
    return NativeReceipt(
        native_id=native_id,
        profile_id=profile.id,
        cwd=cwd,
        fingerprint=digest(subtree),
        locator=str(path),
        created_ids=[native_id],
        evidence={
            "owned": owned,
            "activity": "unknown",
            "subtree_files": len(subtree),
            "operation_ids": sorted({p.get("operation_id", "") for p in provenance if p}),
        },
    )


def write(request):
    if locate(request.profile, request.native_id):
        raise BridgeError("native_conflict", "error.native_conflict.exists")
    project_name = os.environ.get("CLAUDE_CODE_PROJECT_DIR_NAME") or re.sub(r"[^a-zA-Z0-9]", "-", request.cwd)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,200}", project_name):
        raise BridgeError(
            "native_path_unsupported",
        )
    directory = Path(request.profile.home) / "projects" / project_name
    root = Path(request.profile.home).resolve()
    if not directory.resolve().is_relative_to(root):
        raise BridgeError("unsafe_native_path", "error.unsafe_native_path.path")
    path = directory / f"{request.native_id}.jsonl"
    try:
        data = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in transcript(request)).encode(
            "utf-8"
        )
        atomic_write(path, data)
    except BridgeError as exc:
        if exc.code == "already_exists":
            raise BridgeError("native_conflict", "error.native_conflict.exists") from None
        raise
    result = inspect(request.profile, request.native_id)
    if result is None:
        raise BridgeError("native_missing", "error.native_missing.vanished")
    return result


def messages(profile, native_id):
    value = read_rows(profile, native_id)
    if value is None:
        raise BridgeError("native_missing", "error.native_missing.conversation")
    result = []
    for row in value[1]:
        if row.get("type") not in {"user", "assistant"}:
            continue
        payload = row.get("message", {})
        if not isinstance(payload, dict):
            raise BridgeError("native_corrupt", "error.native_corrupt.messages")
        content = payload.get("content", [])
        if not isinstance(content, (str, list)):
            raise BridgeError("native_corrupt", "error.native_corrupt.messages")
        if isinstance(content, list) and any(not isinstance(part, dict) for part in content):
            raise BridgeError("native_corrupt", "error.native_corrupt.blocks")
        text = (
            content
            if isinstance(content, str)
            else "".join(part.get("text", "") for part in content if part.get("type") == "text")
        )
        result.append((payload.get("role"), text))
    return result
