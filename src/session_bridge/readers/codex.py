"""Codex rollout archives and read-only paginated history projection."""

from __future__ import annotations

import re
from pathlib import Path

from session_bridge import history as history_index
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
    found, parents, orphans = {}, {}, set()
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
                    "source",
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
                # Codex records helper threads (spawned agents, guardian reviews) as their own rows.
                source = str(value.get("source") or "")
                if value.get("thread_source") in {"subagent", "guardian_review"} or source.startswith(
                    '{"subagent"'
                ):
                    found[value["id"]].metadata["subagent"] = True
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
                    locator = found[key].locator
                    # Codex compresses rollouts after indexing them; a later segment continues the earlier ones.
                    indexed = bool(locator) and (Path(locator).exists() or Path(locator + ".zst").exists())
                    if indexed and not Path(locator).exists():
                        found[key].locator = locator = locator + ".zst"
                    # Without a usable index, the newest segment (its name starts with its time) wins,
                    # whether it sits in sessions or archived_sessions.
                    if (key not in orphans and not indexed) or (
                        key in orphans and path.name > Path(locator).name
                    ):
                        found[key].locator = str(path)
                        orphans.add(key)
                    continue
                orphans.add(key)
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
            # A spawned agent, unlike a fork, is part of its parent's conversation.
            item.metadata["spawned"] = True
    return sorted(found.values(), key=lambda r: r.updated_at or r.created_at or "", reverse=True)


ROLLOUT_NAME = re.compile(
    r"-(?P<thread>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:_(?P<segment>[0-9a-f-]+))?$"
)


def _rollout(base, identity):
    """The rollout file a history base names: a thread's first file, or a segment after its underscore."""
    for directory in ("sessions", "archived_sessions"):
        folder = base / directory
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("rollout-*")):
            match = ROLLOUT_NAME.search(path.name.split(".jsonl")[0])
            if not match or not path.name.endswith((".jsonl", ".jsonl.zst")):
                continue
            if identity == (match["segment"] or match["thread"]):
                try:
                    return scoped(path, base)
                except BridgeError:
                    return None
    return None


def _history_base(records):
    for record in records:
        if record.get("type") == "session_meta":
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            value = payload.get("history_base")
            return value if isinstance(value, dict) else None
    return None


def _lineage(base, records):
    """A segment or fork continues the records of its history base before the ordinal where it takes over."""
    issues, chain, seen = [], [records], set()
    history_base = _history_base(records)
    while history_base:
        source, end = history_base.get("thread_id"), history_base.get("end_ordinal_exclusive")
        path = _rollout(base, source) if isinstance(source, str) and source not in seen else None
        if path is None or not isinstance(end, int):
            issues.append("source_history_base_missing")
            return [record for part in reversed(chain) for record in part], issues, True
        seen.add(source)
        try:
            # The records a base hands over are fixed; a live base that keeps growing past them is not damage.
            earlier, found = capture(path, snapshot=True)
        except BridgeError as exc:
            earlier, found = [], [exc.code]
        issues += [w for w in found if w == "source_changed_during_snapshot"]
        found = [w for w in found if w != "source_changed_during_snapshot"]
        ordinals = [r.get("ordinal", i) for i, r in enumerate(earlier)]
        kept = [(n, r) for n, r in zip(ordinals, earlier, strict=True) if isinstance(n, int) and n < end]
        chain.append([r for _, r in kept])
        # An unreadable base, or one that stops before the ordinal the segment continues from, lost history.
        if found or max((n for n, _ in kept), default=-1) < end - 1:
            issues += found or ["source_history_base_incomplete"]
            return [record for part in reversed(chain) for record in part], issues, True
        history_base = _history_base(earlier)
    return [record for part in reversed(chain) for record in part], issues, False


AGENT_MESSAGE_ENCRYPTED = "[encrypted by the source provider; content unavailable]"


def _agent_message(item, self_path):
    """Messages between agents: this agent's own are its output, the others reach it as input."""
    parts = []
    for block in item.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "encrypted_content":
            parts.append(AGENT_MESSAGE_ENCRYPTED)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    role = "assistant" if item.get("author") == self_path else "user"
    return role, "".join(parts)


RETAINED_ANNOTATIONS = {"verified_answer"}


def is_retained_annotation(record):
    """A retained_context record that annotates the turn instead of replacing the model context."""
    payload = record.get("payload")
    return (
        record.get("type") == "retained_context"
        and isinstance(payload, dict)
        and payload.get("type") in RETAINED_ANNOTATIONS
    )


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


SUMMARY_PREFIX = (
    "Another language model started to solve this problem and produced a summary of its thinking process."
)
AGENTS_PREFIX = "# AGENTS.md instructions for "


def _contextual(event):
    """Context Codex injects as a user message, which never counts as a turn of the user's."""
    stripped = event.text.lstrip()
    if stripped.startswith(AGENTS_PREFIX):
        return True
    tag = re.match(r"<([A-Za-z_][\w-]*)[\s>]", stripped)
    return bool(tag) and "</" + tag.group(1) + ">" in stripped


def _turn_boundary(event):
    """Codex 0.159 ``is_user_turn_boundary``: a real user message or a message between agents."""
    native = event.native.get("payload") if isinstance(event.native.get("payload"), dict) else event.native
    if not isinstance(native, dict):
        return False
    if native.get("type") == "agent_message":
        return True
    return (
        native.get("type") == "message"
        and native.get("role") == "user"
        and not _contextual(event)
        and not event.text.startswith(SUMMARY_PREFIX + "\n")
    )


def _build(records, self_path):
    """Model context of one rollout lineage, with the turns a legacy rollback dropped as versions.

    Returns the events, the record index each one comes from, the versions before each
    rollback as (key, context, lineage) index sets, and whether the context is ambiguous."""
    events, origins, versions = [], [], []
    ambiguous = False
    response_counts = {}
    for record in records:
        if record.get("type") == "response_item":
            item = record.get("payload") or {}
            if isinstance(item, dict) and item.get("type") == "message":
                identity = (item.get("role"), text(item.get("content", "")))
                response_counts[identity] = response_counts.get(identity, 0) + 1
    dropped = set()
    for i, record in enumerate(records):
        record_kind = record.get("type")
        item = record.get("payload") or {}
        if not isinstance(item, dict):
            item = {}
        ordinal = record.get("ordinal", i)
        if record_kind == "event_msg" and item.get("type") == "thread_rolled_back":
            # Codex drops the newest user turns from the context it rebuilt so far (drop_last_n_user_turns).
            turns = item.get("num_turns")
            live = [index for index, event in enumerate(events) if event.active]
            boundaries = [index for index in live if _turn_boundary(events[index])]
            if isinstance(turns, int) and turns > 0 and boundaries:
                cut = boundaries[-turns] if turns < len(boundaries) else boundaries[0]
                # Context updates recorded just before the dropped turn go with it.
                earlier = [index for index in live if index < cut]
                while earlier and (
                    events[earlier[-1]].role in ("developer", "system") or _contextual(events[earlier[-1]])
                ):
                    cut = earlier.pop()
                if any(
                    events[index].kind == "summary" or index in dropped for index in live if index >= cut
                ) or (turns > len(boundaries) and any(event.kind == "summary" for event in events)):
                    # Codex then replays an older checkpoint; which items survive is not modelled here.
                    ambiguous = True
                    events.append(Event(id="rollout-" + str(i), kind="metadata", active=False, native=record))
                    origins.append(i)
                    continue
                before = frozenset(live)
                versions.append(("rollback:" + str(ordinal), before, frozenset(range(len(events))) - dropped))
                for index in live:
                    if index >= cut:
                        events[index].active = False
                        dropped.add(index)
            events.append(Event(id="rollout-" + str(i), kind="metadata", active=False, native=record))
            origins.append(i)
            continue
        # Response items and explicit replacement histories are the model-context source.
        # Paginated thread_items represent UI history, which may retain pre-compaction messages.
        active = True
        if record_kind in ("response_item", "event_msg"):
            event = _event(item, "rollout-" + str(i), active=active, native=record, ordinal=ordinal)
            if item.get("type") == "agent_message" and "author" in item:
                event.role, event.text = _agent_message(item, self_path)
            identity = (event.role, event.text)
            if record_kind == "event_msg" and event.kind == "message" and response_counts.get(identity, 0):
                response_counts[identity] -= 1
                event.active = False
                event.kind = "metadata"
        elif record_kind == "inter_agent_communication" and "author" in item:
            # Codex hands a message between agents to the model as input (to_model_input_item).
            event = _event(
                {"type": "agent_message", **item}, "rollout-" + str(i), native=record, ordinal=ordinal
            )
            event.kind = "message"
            event.role, event.text = _agent_message(item, self_path)
        elif is_retained_annotation(record):
            # Annotations persist answers already present in the tool output; they replace nothing.
            event = Event(id="rollout-" + str(i), kind="metadata", active=False, native=record)
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
                            retained = _event(
                                replacement_item,
                                "retained-" + str(i) + "-" + str(j),
                                native=native_item,
                            )
                            if (
                                replacement_item.get("type") == "agent_message"
                                and "author" in replacement_item
                            ):
                                retained.role, retained.text = _agent_message(replacement_item, self_path)
                            events.append(retained)
                            origins.append(i)
                    event.active = False
                elif not event.text:
                    ambiguous = True
        else:
            event = Event(id="rollout-" + str(i), kind="metadata", active=False, native=record)
        # Rollout records carry the time; their payloads usually do not.
        event.timestamp = event.timestamp or timestamp(record.get("timestamp"))
        events.append(event)
        origins.append(i)
    lineage = frozenset(range(len(events))) - dropped
    return events, origins, versions, lineage, ambiguous


def _segments(base, thread):
    """Every rollout file of ``thread``: its first one and each segment a revert started."""
    found = []
    for directory in ("sessions", "archived_sessions"):
        folder = base / directory
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("rollout-*")):
            match = ROLLOUT_NAME.search(path.name.split(".jsonl")[0])
            if match and match["thread"] == thread and path.name.endswith((".jsonl", ".jsonl.zst")):
                try:
                    found.append(scoped(path, base))
                except BridgeError:
                    continue
    # A compressed copy and its plain file are the same rollout.
    plain = {str(path)[: -len(".zst")] if str(path).endswith(".zst") else str(path) for path in found}
    return sorted(Path(name) if Path(name).exists() else Path(name + ".zst") for name in plain)


def _superseded(base, thread, current, records, self_path):
    """Histories a revert left behind: other rollouts of the thread that are not part of ``records``.

    Codex 0.159 edits an earlier prompt by writing a new rollout whose history base keeps the
    records before that turn; the old rollout stays on disk with the turns after it."""
    result = []
    signatures = [digest(record) for record in records]
    for path in _segments(base, thread):
        if current is not None and path.resolve() == current.resolve():
            continue
        try:
            own, issues = capture(path, snapshot=True)
            chain, lineage_issues, broken = _lineage(base, own)
        except BridgeError:
            continue
        if broken or issues or lineage_issues:
            continue
        shared = 0
        for left, right in zip(signatures, (digest(record) for record in chain), strict=False):
            if left != right:
                break
            shared += 1
        if shared == len(chain):
            continue  # Everything it holds is already in the current history.
        result.append((path, chain, shared))
    return result


def read(profile, ref, *, snapshot=False):
    warnings = guard(ref, snapshot)
    base = _root(profile)
    raw, records, history, turns = [], [], [], []
    related = []
    ambiguous = False
    locator = ref.locator
    self_path = None
    state_db = base / "state_5.sqlite"
    if state_db.exists():
        with database(state_db) as con:
            if "id" in columns(con, "threads"):
                for row in con.execute("SELECT * FROM threads WHERE id=?", (ref.native_id,)):
                    value = dict(row)
                    raw.append({"table": "threads", "row": value})
                    # Spawn-edge refs carry no locator; the thread's own row names its rollout.
                    locator = locator or value.get("rollout_path") or ""
                    self_path = value.get("agent_path") or self_path
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
    history_known = False
    if history_db.exists():
        with database(history_db) as con:
            if {"thread_id", "item_id", "item_json", "rollout_ordinal"} <= columns(con, "thread_items"):
                history_known = True
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
        # A thread outside the index names its agent in its own metadata, not in the base it continues.
        meta = next((r.get("payload") for r in records if r.get("type") == "session_meta"), None)
        if not self_path and isinstance(meta, dict) and isinstance(meta.get("agent_path"), str):
            self_path = meta["agent_path"]
        records, lineage_issues, broken = _lineage(base, records)
        warnings += issues + lineage_issues
        ambiguous = bool(issues) or broken
        raw.extend(records)
    elif not history:
        raise BridgeError("source_not_found", "No se encuentra el historial de la sesión Codex.")
    else:
        warnings.append("source_rollout_missing")
    self_path = self_path or "/root"
    events, origins, rollbacks, own_lineage, unstable = _build(records, self_path)
    ambiguous |= unstable
    if unstable:
        warnings.append("source_rollback_across_compaction")
    offset = len(history) if records else 0
    versions = [
        history_index.Version(
            key="current",
            context=frozenset(offset + i for i, event in enumerate(events) if event.active),
            lineage=frozenset(offset + i for i in own_lineage),
        )
    ]
    for key, context, lineage in rollbacks:
        versions.append(
            history_index.Version(
                key=key,
                context=frozenset(offset + i for i in context),
                lineage=frozenset(offset + i for i in lineage),
            )
        )
    if records and not unstable:
        # Earlier versions a prompt edit left in other rollouts of this thread: their shared
        # records are already here, so only what follows the edit is added, as inactive history.
        for alternative, chain, shared in _superseded(base, ref.native_id, path, records, self_path):
            own, own_origins, _, own_lineage_alt, broken = _build(chain, self_path)
            prefix = sum(1 for origin in own_origins if origin < shared)
            if broken or [e.id for e in own[:prefix]] != [e.id for e in events[:prefix]]:
                warnings.append("source_variant_unreadable")
                continue
            label = alternative.name.split(".jsonl")[0]
            start = offset + len(events)
            for event in own[prefix:]:
                event.id = label + ":" + event.id
            tail = own[prefix:]
            context = {offset + i for i in range(prefix) if own[i].active}
            context |= {start + j for j, event in enumerate(tail) if event.active}
            lineage = {offset + i for i in own_lineage_alt if i < prefix}
            lineage |= {start + j for j in range(len(tail))}
            for event in tail:
                event.active = False
            events.extend(tail)
            raw.extend(
                {"table": "discarded_rollout", "segment": label, "row": chain[origin]}
                for origin in sorted({origin for origin in own_origins if origin >= shared})
            )
            versions.append(
                history_index.Version(key=label, context=frozenset(context), lineage=frozenset(lineage))
            )
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
    # Codex resumes a thread whose history it has not projected yet from the rollout itself.
    if ref.metadata.get("history_mode") == "paginated" and not history and not (history_known and records):
        ambiguous = True
        warnings.append("source_paginated_projection_missing")
    conversation = finish(
        ref,
        events,
        raw,
        warnings,
        ambiguous=ambiguous,
        attachments=attachments_in(raw, _root(profile)),
        related=related,
    )
    if records:
        # The paginated UI history duplicates the rollout; it belongs to no version's history.
        history_index.attach(conversation, versions, current="current")
    return conversation
