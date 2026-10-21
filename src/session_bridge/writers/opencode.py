"""OpenCode SessionTransfer.Data encoder and CLI import/readback."""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from pathlib import Path

from session_bridge.errors import BridgeError
from session_bridge.models import NativeReceipt, digest
from session_bridge.readers.common import columns, database

from .common import run, validate_id

VERSION = "2.0.29"


def transfer_data(request):
    validate_id("opencode", request.native_id)
    now = int(time.time() * 1000)
    provider, _, model = (request.model or "imported/imported").partition("/")
    if not provider or not model:
        raise BridgeError("invalid_model")
    ref, messages = {"providerID": provider, "id": model}, []
    for index, message in enumerate(request.messages):
        item = {
            "id": "msg_" + uuid.uuid5(uuid.NAMESPACE_URL, request.native_id + ":" + str(index)).hex,
            "type": message.role,
            "time": {"created": now + index},
            "metadata": {"session_bridge": {"operation_id": request.operation_id, "imported": True}},
        }
        if message.role == "user":
            item["text"] = message.text
        else:
            item.update(
                {
                    "agent": "build",
                    "model": ref,
                    "content": [{"type": "text", "text": message.text}],
                    "finish": "stop",
                }
            )
            item["time"]["completed"] = now + index
        messages.append(item)
    return {
        "info": {
            "id": request.native_id,
            "projectID": "global",
            "location": {"directory": request.cwd},
            "title": request.title,
            "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            "time": {"created": now, "updated": now, "idle": now + len(messages)},
            "outcome": "succeeded",
        },
        "messages": messages,
    }


def export(profile, native_id, cwd):
    validate_id("opencode", native_id)
    result = run(profile, ["session", "export", native_id, "--standalone"], cwd=cwd)
    if result.returncode:
        raise BridgeError("native_read_failed", "error.native_read_failed.conversation")
    try:
        value = json.loads(result.stdout)
    except ValueError:
        raise BridgeError("native_read_failed", "error.native_read_failed.record") from None
    if not isinstance(value, dict) or not isinstance(value.get("info"), dict):
        raise BridgeError("native_read_failed", "error.native_read_failed.record")
    if value["info"].get("id") != native_id or not isinstance(value.get("messages"), list):
        raise BridgeError("native_read_failed", "error.native_read_failed.unexpected")
    return value


def inspect(profile, native_id, *, cwd=None):
    result = readback(profile, native_id)
    return result[0] if result else None


def readback(profile, native_id):
    """One read-only WAL-aware snapshot; never starts the native client."""
    validate_id("opencode", native_id)
    path = Path(profile.home) / "opencode.db"
    if not path.exists():
        return None
    if path.is_symlink() or not path.resolve().is_relative_to(Path(profile.home).resolve()):
        raise BridgeError("unsafe_native_path", "error.unsafe_native_path.base")
    raw, selected = {}, []
    with database(path) as con:
        if not {"id", "parent_id", "directory"} <= columns(con, "session_v2"):
            raise BridgeError("native_schema_unsupported", "error.native_schema_unsupported.schema")
        if not {"session_id", "seq", "type", "data"} <= columns(con, "session_message"):
            raise BridgeError("native_schema_unsupported", "error.native_schema_unsupported.history")
        rows = [
            dict(row)
            for row in con.execute(
                "WITH RECURSIVE subtree(id) AS (SELECT id FROM session_v2 WHERE id=? "
                "UNION SELECT s.id FROM session_v2 s JOIN subtree p ON s.parent_id=p.id) "
                "SELECT * FROM session_v2 WHERE id IN (SELECT id FROM subtree) ORDER BY id",
                (native_id,),
            )
        ]
        if not rows:
            return None
        raw["session_v2"] = rows
        ids = [row["id"] for row in rows]
        # Parameter batches avoid SQLite's host-variable limit for large subtrees.
        for table in (
            "session_message",
            "session_pending",
            "session_inbox",
            "instruction_entry",
            "instruction_state",
        ):
            if "session_id" not in columns(con, table):
                continue
            all_rows = []
            for start in range(0, len(ids), 500):
                batch = ids[start : start + 500]
                all_rows.extend(
                    dict(row)
                    for row in con.execute(
                        f"SELECT * FROM {table} WHERE session_id IN ({','.join('?' for _ in batch)})", batch
                    )
                )
            raw[table] = sorted(all_rows, key=lambda row: json.dumps(row, sort_keys=True))
        session = next(row for row in rows if row["id"] == native_id)
        for row in sorted(raw.get("session_message", []), key=lambda item: item["seq"]):
            if row["session_id"] == native_id:
                try:
                    data = json.loads(row["data"])
                except (ValueError, TypeError):
                    raise BridgeError("native_corrupt", "error.native_corrupt.messages") from None
                selected.append({**data, "id": row["id"], "type": row["type"]})
    value = {"info": {"id": native_id, "location": {"directory": session["directory"]}}, "messages": selected}
    receipt = receipt_from_export(profile, value)
    receipt.fingerprint = digest(raw)
    receipt.created_ids = ids
    receipt.evidence["descendants"] = len(ids) - 1
    receipt.evidence["activity"] = "active" if any(row.get("time_suspended") for row in rows) else "unknown"
    return receipt, projected_messages(value)


def receipt_from_export(profile, value):
    info = value["info"]
    provenance = [m.get("metadata", {}).get("session_bridge", {}) for m in value["messages"]]
    return NativeReceipt(
        native_id=info["id"],
        profile_id=profile.id,
        cwd=info.get("location", {}).get("directory", ""),
        fingerprint=digest(value),
        locator=str(Path(profile.home) / "opencode.db"),
        created_ids=[info["id"]],
        evidence={
            "owned": bool(provenance) and all(p.get("imported") is True for p in provenance),
            "activity": "unknown",
            "message_count": len(value["messages"]),
            "operation_ids": sorted({p.get("operation_id", "") for p in provenance}),
        },
    )


def write(request):
    payload = transfer_data(request)
    with tempfile.TemporaryDirectory(prefix="sessionbridge-") as directory:
        path = Path(directory) / "transfer.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = run(
            request.profile,
            ["session", "import", str(path), "--directory", request.cwd, "--standalone"],
            cwd=request.cwd,
        )
    if result.returncode:
        raise BridgeError("native_import_failed")
    receipt = inspect(request.profile, request.native_id, cwd=request.cwd)
    if receipt is None:
        raise BridgeError("native_read_failed", "error.native_read_failed.persisted_record")
    if receipt.evidence.get("operation_ids") != [request.operation_id]:
        raise BridgeError("native_conflict", "error.native_conflict.unattributed")
    return receipt


def projected_messages(value):
    result = []
    for item in value.get("messages", []):
        if item.get("type") == "user":
            result.append(("user", item["text"]))
        elif item.get("type") == "assistant":
            result.append(
                (
                    "assistant",
                    "".join(c.get("text", "") for c in item.get("content", []) if c.get("type") == "text"),
                )
            )
    return result
