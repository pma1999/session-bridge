"""OpenCode legacy/v2 SQLite readers. Only session-owned tables are queried."""

from __future__ import annotations

from session_bridge.errors import BridgeError
from session_bridge.models import SessionRef

from .common import (
    attachments_in,
    block_events,
    columns,
    database,
    finish,
    guard,
    historical_tool_text,
    object_json,
    root,
    scoped,
    text,
    timestamp,
)


def _root(profile):
    return root(profile, "opencode.db", (".local", "share", "opencode"))


def _tables(con):
    found = [t for t in ("session", "session_v2") if "id" in columns(con, t)]
    if not found:
        raise BridgeError("source_schema_unsupported", "No se reconoce el esquema de sesiones OpenCode.")
    return found


def _session_ref(profile, row, table):
    return SessionRef(
        profile_id=profile.id,
        native_id=row["id"],
        tool="opencode",
        title=row.get("title") or "",
        cwd=row.get("directory") or "",
        parent_id=row.get("parent_id"),
        created_at=timestamp(row.get("time_created")),
        updated_at=timestamp(row.get("time_updated")),
        archived=bool(row.get("time_archived")),
        version=row.get("version"),
        locator=str(_root(profile) / "opencode.db"),
        metadata={"table": table},
    )


def discover(profile):
    path = _root(profile) / "opencode.db"
    if not path.exists():
        return []
    found = {}
    with database(path) as con:
        for table in _tables(con):
            cols = columns(con, table)
            selected = [
                c
                for c in (
                    "id",
                    "title",
                    "directory",
                    "parent_id",
                    "time_created",
                    "time_updated",
                    "time_archived",
                    "version",
                )
                if c in cols
            ]
            for row in con.execute("SELECT " + ",".join(selected) + " FROM " + table):
                item = dict(row)
                found[item["id"]] = _session_ref(profile, item, table)
    return sorted(found.values(), key=lambda r: r.updated_at or "", reverse=True)


def read(profile, ref, *, snapshot=False):
    warnings = guard(ref, snapshot)
    path = scoped(ref.locator or _root(profile) / "opencode.db", _root(profile))
    raw, messages, part_map, session = [], {}, {}, None
    related = {}
    with database(path) as con:
        for table in _tables(con):
            row = con.execute("SELECT * FROM " + table + " WHERE id=?", (ref.native_id,)).fetchone()
            if row:
                session = dict(row)
                raw.append({"table": table, "row": session})
        for table in _tables(con):
            if "parent_id" in columns(con, table):
                for row in con.execute(
                    "SELECT id,title,directory,parent_id,time_created,time_updated,time_archived FROM "
                    + table
                    + " WHERE parent_id=?",
                    (ref.native_id,),
                ):
                    value = dict(row)
                    related[value["id"]] = _session_ref(profile, value, table)
        if session is None:
            raise BridgeError("source_not_found", "La sesión OpenCode no existe.")
        if {"id", "session_id", "data"} <= columns(con, "message"):
            for row in con.execute(
                "SELECT * FROM message WHERE session_id=? ORDER BY time_created,id", (ref.native_id,)
            ):
                value = dict(row)
                raw.append({"table": "message", "row": value})
                data = object_json(value["data"], warnings)
                messages[value["id"]] = (value.get("time_created", 0), value, data, "legacy")
        if {"id", "message_id", "session_id", "data"} <= columns(con, "part"):
            for row in con.execute(
                "SELECT * FROM part WHERE session_id=? ORDER BY time_created,id", (ref.native_id,)
            ):
                value = dict(row)
                raw.append({"table": "part", "row": value})
                part_map.setdefault(value["message_id"], []).append(
                    (value, object_json(value["data"], warnings))
                )
        if {"id", "session_id", "type", "seq", "data"} <= columns(con, "session_message"):
            for row in con.execute(
                "SELECT * FROM session_message WHERE session_id=? ORDER BY seq,id", (ref.native_id,)
            ):
                value = dict(row)
                raw.append({"table": "session_message", "row": value})
                data = object_json(value["data"], warnings)
                messages[value["id"]] = (value["seq"], value, data, "v2")
    ordered = sorted(messages.items(), key=lambda pair: (pair[1][0], pair[0]))
    mixed = len({v[3] for _, v in ordered}) > 1
    # IDs are monotonic OpenCode identifiers; mixed generation ordering cannot be inferred from seq vs millis.
    if mixed:
        ordered.sort(key=lambda pair: pair[0])
    ambiguous = mixed
    revert = object_json(session.get("revert"), warnings)
    boundary = revert.get("messageID", revert.get("messageId"))
    if boundary and boundary not in messages:
        ambiguous = True
    events = []
    reverted = False
    for key, (_, row, data, generation) in ordered:
        if key == boundary:
            reverted = True
        kind = row.get("type") if generation == "v2" else data.get("role")
        role = kind if kind in ("user", "assistant", "system") else "unknown"
        parts = part_map.get(key, []) if generation == "legacy" else []
        content = data.get("content", data.get("text", "")) if generation == "v2" else [d for _, d in parts]
        event_kind = "message" if kind in ("user", "assistant", "synthetic", "system") else "metadata"
        if kind == "synthetic":
            role = "user"
        if kind == "shell":
            event_kind, role = "tool", "tool"
            output = data.get("output") or {}
            content = historical_tool_text(
                "opencode",
                "shell",
                call_id=data.get("shellID"),
                arguments=data.get("command"),
                result=output.get("output") if isinstance(output, dict) else output,
            )
        if kind == "compaction" and data.get("status") == "completed":
            event_kind = "summary"
            role = "user"
            content = text(data.get("summary", ""))
            recent = text(data.get("recent", ""))
            if recent:
                content += "\n\n[Retained recent conversation]\n" + recent
            if data.get("providerContext"):
                # Provider-specific encrypted checkpoints cannot be replayed across arbitrary models.
                warnings.append("source_provider_checkpoint_archive_only")
                if not content:
                    ambiguous = True
            if not reverted:
                for earlier in events:
                    earlier.active = False
        if generation == "legacy" and data.get("summary") is True:
            event_kind = "summary"
            if not reverted:
                for earlier in events:
                    earlier.active = False
        events.extend(
            block_events(
                key,
                content,
                role=role,
                kind=event_kind,
                active=not reverted,
                native={"row": row, "data": data},
                source_format="opencode",
                parent_id=data.get("parentID"),
                timestamp=timestamp(row.get("time_created")),
            )
        )
    if revert.get("partID"):
        ambiguous = True
        warnings.append("source_partial_message_revert")
    return finish(
        ref,
        events,
        raw,
        warnings,
        ambiguous=ambiguous,
        attachments=attachments_in(raw, _root(profile)),
        related=list(related.values()),
    )
