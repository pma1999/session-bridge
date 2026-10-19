"""Codex rollout archives and read-only paginated history projection."""

from __future__ import annotations

from pathlib import Path

from session_bridge.errors import BridgeError
from session_bridge.models import Event, SessionRef, digest

from .common import (
    attachments_in,
    capture,
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
    home = Path(profile.home).expanduser().resolve()
    if (home / "sessions").exists() or (home / "archived_sessions").exists():
        return home
    return root(profile, "state_5.sqlite", (".codex",))


def discover(profile):
    base = _root(profile)
    found, parents = {}, {}
    db = base / "state_5.sqlite"
    if db.exists():
        with database(db) as con:
            cols = columns(con, "threads")
            if "id" not in cols:
                raise BridgeError("source_schema_unsupported", "No se reconoce el esquema de hilos Codex.")
            selected = [
                k
                for k in (
                    "id",
                    "rollout_path",
                    "title",
                    "name",
                    "cwd",
                    "created_at",
                    "updated_at",
                    "archived",
                    "cli_version",
                    "history_mode",
                    "thread_source",
                )
                if k in cols
            ]
            for row in con.execute("SELECT " + ",".join(selected) + " FROM threads"):
                value = dict(row)
                found[value["id"]] = SessionRef(
                    profile_id=profile.id,
                    native_id=value["id"],
                    tool="codex",
                    title=value.get("name") or value.get("title") or "",
                    cwd=value.get("cwd") or "",
                    archived=bool(value.get("archived")),
                    created_at=timestamp(value.get("created_at")),
                    updated_at=timestamp(value.get("updated_at")),
                    version=value.get("cli_version"),
                    locator=value.get("rollout_path") or "",
                    metadata={"history_mode": value.get("history_mode", "legacy")},
                )
            if {"parent_thread_id", "child_thread_id"} <= columns(con, "thread_spawn_edges"):
                for row in con.execute("SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges"):
                    parents[row["child_thread_id"]] = row["parent_thread_id"]
    # Native indices are accelerators, never the sole truth. Orphan rollout archives remain discoverable.
    for directory in ("sessions", "archived_sessions"):
        for path in sorted((base / directory).rglob("*.jsonl*")):
            if not (path.name.endswith(".jsonl") or path.name.endswith(".jsonl.zst")):
                continue
            try:
                import io

                with path.open("rb") as source:
                    if path.suffix == ".zst":
                        import zstandard

                        stream = zstandard.ZstdDecompressor().stream_reader(source)
                    else:
                        stream = source
                    line = io.TextIOWrapper(stream, encoding="utf-8").readline(4 * 1024 * 1024)
                data = object_json(line, [])
                payload = data.get("payload") or {}
                if (
                    data.get("type") != "session_meta"
                    or not isinstance(payload, dict)
                    or not payload.get("id")
                ):
                    continue
                key = payload["id"]
                if key in found:
                    if not Path(found[key].locator).exists():
                        found[key].locator = str(path)
                    continue
                found[key] = SessionRef(
                    profile_id=profile.id,
                    native_id=key,
                    tool="codex",
                    locator=str(path),
                    cwd=payload.get("cwd") or "",
                    version=payload.get("cli_version"),
                    parent_id=payload.get("forked_from_id"),
                    archived=directory == "archived_sessions",
                    created_at=data.get("timestamp"),
                )
            except (OSError, ValueError, UnicodeError):
                continue
            except zstandard.ZstdError:
                # Corrupt orphan compressed files lack a trustworthy ID; explicit reads report errors.
                continue
    for key, item in found.items():
        if key in parents:
            item.parent_id = parents[key]
    return sorted(found.values(), key=lambda r: r.updated_at or r.created_at or "", reverse=True)


def _event(item, key, *, active=True, native=None, ordinal=None):
    kind = item.get("type")
    role = item.get(
        "role",
        "user"
        if kind in ("userMessage", "user_message")
        else "assistant"
        if kind in ("agentMessage", "agent_message")
        else "unknown",
    )
    role = role if role in ("user", "assistant", "system", "developer", "tool") else "unknown"
    if kind in ("message", "userMessage", "agentMessage", "user_message", "agent_message"):
        event_kind = "message"
    elif kind in ("compacted", "contextCompaction", "retained_context"):
        event_kind = "summary"
        role = "assistant"
    elif kind in ("reasoning",):
        event_kind = "reasoning"
    elif kind in (
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "commandExecution",
        "mcpToolCall",
        "webSearch",
        "fileChange",
    ):
        event_kind = "tool"
    else:
        event_kind = "unknown"
    value = item.get(
        "content", item.get("text", item.get("message", item.get("output", item.get("summary", ""))))
    )
    if event_kind == "tool":
        role = "tool"
        value = historical_tool_text(
            "codex",
            kind,
            tool=item.get("name", item.get("tool")),
            call_id=item.get("call_id", item.get("id")),
            arguments=item.get("arguments", item.get("input", item.get("command"))),
            result=item.get("output", item.get("result", item.get("aggregatedOutput", item.get("content")))),
        )
    return Event(
        id=str(item.get("id") or key),
        kind=event_kind,
        role=role,
        text=text(value),
        active=active,
        timestamp=timestamp(item.get("timestamp")),
        native=native or item,
        branch=str(ordinal) if ordinal is not None else None,
    )


def read(profile, ref, *, snapshot=False):
    warnings = guard(ref, snapshot)
    base = _root(profile)
    raw, records, history, turns = [], [], [], []
    related = []
    ambiguous = False
    locator = ref.locator
    state_db = base / "state_5.sqlite"
    if state_db.exists():
        with database(state_db) as con:
            if "id" in columns(con, "threads"):
                for row in con.execute("SELECT * FROM threads WHERE id=?", (ref.native_id,)):
                    value = dict(row)
                    raw.append({"table": "threads", "row": value})
                    # Spawn-edge refs carry no locator; the thread's own row names its rollout.
                    locator = locator or value.get("rollout_path") or ""
            if {"parent_thread_id", "child_thread_id"} <= columns(con, "thread_spawn_edges"):
                for edge in con.execute(
                    "SELECT parent_thread_id,child_thread_id,status FROM thread_spawn_edges WHERE parent_thread_id=?",
                    (ref.native_id,),
                ):
                    raw.append({"table": "thread_spawn_edges", "row": dict(edge)})
                    child = edge["child_thread_id"]
                    related.append(
                        SessionRef(
                            profile_id=profile.id,
                            native_id=child,
                            tool="codex",
                            parent_id=ref.native_id,
                            metadata={"spawn_status": edge["status"]},
                        )
                    )
    history_db = base / "thread_history_1.sqlite"
    if history_db.exists():
        with database(history_db) as con:
            if {"thread_id", "item_id", "item_json", "rollout_ordinal"} <= columns(con, "thread_items"):
                for row in con.execute(
                    "SELECT * FROM thread_items WHERE thread_id=? ORDER BY rollout_ordinal,item_id",
                    (ref.native_id,),
                ):
                    value = dict(row)
                    raw.append({"table": "thread_items", "row": value})
                    item = object_json(value["item_json"], warnings)
                    history.append(
                        _event(
                            item,
                            value["item_id"],
                            native={"row": value, "item": item},
                            ordinal=value["rollout_ordinal"],
                        )
                    )
            if {"thread_id", "turn_id", "status"} <= columns(con, "thread_turns"):
                for row in con.execute(
                    "SELECT * FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal,turn_id",
                    (ref.native_id,),
                ):
                    value = dict(row)
                    raw.append({"table": "thread_turns", "row": value})
                    turns.append(value)
            if {"thread_id", "next_rollout_ordinal"} <= columns(con, "thread_history_projection_state"):
                for row in con.execute(
                    "SELECT * FROM thread_history_projection_state WHERE thread_id=?", (ref.native_id,)
                ):
                    value = dict(row)
                    raw.append({"table": "thread_history_projection_state", "row": value})
    if any(t.get("status") in ("inProgress", "in_progress", "running") for t in turns):
        if not snapshot:
            raise BridgeError("source_active", "La sesión de origen está activa; solicita una instantánea.")
        warnings.append("source_active_snapshot")
    path = scoped(locator, base) if locator else None
    if path and not path.exists() and Path(str(path) + ".zst").exists():
        path = Path(str(path) + ".zst")
    if path and path.exists():
        records, issues = capture(path, snapshot=snapshot)
        warnings += issues
        ambiguous = bool(issues)
        raw.extend(records)
    elif not history:
        raise BridgeError("source_not_found", "No se encuentra el historial de la sesión Codex.")
    else:
        warnings.append("source_rollout_missing")
    events = []
    response_counts = {}
    for record in records:
        if record.get("type") == "response_item":
            item = record.get("payload") or {}
            if isinstance(item, dict) and item.get("type") == "message":
                identity = (item.get("role"), text(item.get("content", "")))
                response_counts[identity] = response_counts.get(identity, 0) + 1
    for i, record in enumerate(records):
        record_kind = record.get("type")
        item = record.get("payload") or {}
        if not isinstance(item, dict):
            item = {}
        ordinal = record.get("ordinal", i)
        # Response items and explicit replacement histories are the model-context source.
        # Paginated thread_items represent UI history, which may retain pre-compaction messages.
        active = True
        if record_kind in ("response_item", "event_msg"):
            event = _event(item, "rollout-" + str(i), active=active, native=record, ordinal=ordinal)
            identity = (event.role, event.text)
            if record_kind == "event_msg" and event.kind == "message" and response_counts.get(identity, 0):
                response_counts[identity] -= 1
                event.active = False
                event.kind = "metadata"
        elif record_kind in ("compacted", "retained_context"):
            replacement = item.get("replacement_history", item.get("items", item.get("history")))
            event = _event(
                {"type": record_kind, "summary": item.get("message", item.get("summary", ""))},
                "rollout-" + str(i),
                active=active,
                native=record,
                ordinal=ordinal,
            )
            if active:
                for previous in events:
                    previous.active = False
                if isinstance(replacement, list):
                    # Reference the replacement record once per item; copying it would grow
                    # quadratically with its size, and raw_records already preserves it verbatim.
                    source_record = digest(record)
                    for j, replacement_item in enumerate(replacement):
                        if isinstance(replacement_item, dict):
                            native_item = {
                                "type": record_kind,
                                "ordinal": ordinal,
                                "source_record": source_record,
                                "item": replacement_item,
                            }
                            events.append(
                                _event(
                                    replacement_item,
                                    "retained-" + str(i) + "-" + str(j),
                                    native=native_item,
                                )
                            )
                    event.active = False
                elif not event.text:
                    ambiguous = True
        else:
            event = Event(id="rollout-" + str(i), kind="metadata", active=False, native=record)
        events.append(event)
    if history:
        if records:
            for event in history:
                event.active = False
            events = history + events
        else:
            events = history
            if any(event.kind == "summary" for event in history):
                ambiguous = True
                warnings.append("source_retained_context_missing")
    if ref.metadata.get("history_mode") == "paginated" and not history:
        ambiguous = True
        warnings.append("source_paginated_projection_missing")
    return finish(
        ref,
        events,
        raw,
        warnings,
        ambiguous=ambiguous,
        attachments=attachments_in(raw, _root(profile)),
        related=related,
    )
