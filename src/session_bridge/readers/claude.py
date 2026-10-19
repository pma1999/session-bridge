"""Claude Code JSONL archive and explicit parent/leaf context selection."""

from __future__ import annotations

from session_bridge.errors import BridgeError
from session_bridge.models import SessionRef

from .common import attachments_in, block_events, capture, finish, guard, iter_records, root, scoped


def _root(profile):
    return root(profile, "projects", (".claude",))


def _ref(profile, path):
    # Discovery scans records but emits only explicit metadata, never a prompt-derived title.
    meta = {}
    for record in iter_records(path, snapshot=True):
        for field in ("cwd", "sessionId", "version", "timestamp", "customTitle", "summary", "agentId"):
            if field in record and (field != "summary" or record.get("type") == "summary"):
                meta[field] = record[field]
    return SessionRef(
        profile_id=profile.id,
        native_id=str(meta.get("agentId") or meta.get("sessionId") or path.stem),
        tool="claude",
        title=str(meta.get("customTitle") or ""),
        cwd=str(meta.get("cwd") or ""),
        updated_at=meta.get("timestamp"),
        locator=str(path),
        version=meta.get("version"),
    )


def _subagent_ref(profile, path, parent=None):
    item = _ref(profile, path)
    parent_id = parent.native_id if parent else path.parent.parent.name
    # agentId is scoped to its parent and sessionId commonly repeats the parent's ID.
    item.native_id = parent_id + "/subagents/" + path.stem
    item.parent_id = parent_id
    item.metadata = {"subagent": True, "agent_id": path.stem}
    if parent:
        item.cwd = item.cwd or parent.cwd
        item.version = item.version or parent.version
    return item


def discover(profile):
    base = _root(profile) / "projects"
    result, parents = [], {}
    for path in sorted(base.glob("*/*.jsonl")):
        try:
            item = _ref(profile, path)
            result.append(item)
            parents[path.parent / path.stem] = item
        except BridgeError:
            continue
    for path in sorted(base.glob("*/*/subagents/*.jsonl")):
        try:
            result.append(_subagent_ref(profile, path, parents.get(path.parent.parent)))
        except BridgeError:
            continue
    return result


def read(profile, ref, *, snapshot=False):
    warnings = guard(ref, snapshot)
    path = scoped(ref.locator, _root(profile) / "projects")
    subagent_context = bool(ref.metadata.get("subagent")) or path.parent.name == "subagents"
    records, issues = capture(path, snapshot=snapshot)
    warnings += issues
    nodes = {r["uuid"]: r for r in records if isinstance(r.get("uuid"), str)}
    parent_ids = {r.get("parentUuid") for r in nodes.values() if r.get("parentUuid")}
    leaves = [
        key
        for key, r in nodes.items()
        if key not in parent_ids and (subagent_context or not r.get("isSidechain"))
    ]
    explicit = [r["leafUuid"] for r in records if isinstance(r.get("leafUuid"), str)]
    selected = explicit[-1] if explicit else (leaves[0] if len(leaves) == 1 else None)
    ambiguous = (bool(nodes) and selected is None) or bool(issues)
    children = {}
    for key, record in nodes.items():
        if subagent_context or not record.get("isSidechain"):
            children.setdefault(record.get("parentUuid"), []).append(key)
    visited = set()
    while selected and children.get(selected):
        if selected in visited or len(children[selected]) != 1:
            ambiguous = True
            break
        visited.add(selected)
        selected = children[selected][0]
    active = set()
    current = selected
    while current:
        if current not in nodes and subagent_context:
            parent_path = scoped(path.parent.parent.with_suffix(".jsonl"), _root(profile) / "projects")
            try:
                known_parent = any(
                    record.get("uuid") == current for record in iter_records(parent_path, snapshot=True)
                )
            except BridgeError:
                known_parent = False
            if known_parent:
                warnings.append("source_external_parent_outside_subagent")
            else:
                ambiguous = True
                warnings.append("source_subagent_parent_relationship_incomplete")
            break
        if current in active or current not in nodes:
            ambiguous = True
            break
        active.add(current)
        current = nodes[current].get("parentUuid")
    events, groups = [], {}
    for i, record in enumerate(records):
        kind = record.get("type")
        message = record.get("message") or {}
        if not isinstance(message, dict):
            message = {}
        role = message.get("role", kind)
        role = role if role in ("user", "assistant", "system", "developer", "tool") else "unknown"
        is_summary = bool(record.get("isCompactSummary")) or kind == "summary"
        event_kind = (
            "summary"
            if is_summary
            else ("message" if kind in ("user", "assistant") else "tool" if kind == "tool" else "metadata")
        )
        content = message.get("content", record.get("summary", record.get("content", "")))
        key = str(record.get("uuid", "record-" + str(i)))
        emitted = block_events(
            key,
            content,
            role=role,
            kind=event_kind,
            active=key in active and (subagent_context or not record.get("isSidechain", False)),
            native=record,
            source_format="claude",
            parent_id=record.get("parentUuid"),
            timestamp=record.get("timestamp"),
            branch="sidechain" if record.get("isSidechain") else None,
        )
        events.extend(emitted)
        groups[key] = emitted
    # Explicit compact summaries are retained alongside the selected suffix; unsupported layouts refuse projection.
    for i, event in enumerate(events):
        if event.active and event.kind == "summary":
            for earlier in events[:i]:
                earlier.active = False
    # Explicit preservation metadata is a native second graph, independent of parentUuid.
    for record in records:
        metadata = record.get("compactMetadata") or {}
        preserved = metadata.get("preservedMessages") or {}
        segment = metadata.get("preservedSegment") or {}
        anchor = preserved.get("anchorUuid", segment.get("anchorUuid"))
        if not anchor or anchor not in groups or not any(event.active for event in groups[anchor]):
            continue
        ids = preserved.get("uuids")
        if ids is None and segment.get("headUuid") and segment.get("tailUuid"):
            ids, cursor, visited = [], segment["tailUuid"], set()
            while cursor and cursor not in visited:
                visited.add(cursor)
                ids.append(cursor)
                if cursor == segment["headUuid"]:
                    break
                cursor = nodes.get(cursor, {}).get("parentUuid")
            if not ids or ids[-1] != segment["headUuid"]:
                ids = None
            else:
                ids.reverse()
        if not isinstance(ids, list) or not all(isinstance(key, str) and key in groups for key in ids):
            ambiguous = True
            warnings.append("source_partial_compaction_relationship_incomplete")
            continue
        if (
            segment
            and ids
            and (ids[0] != segment.get("headUuid", ids[0]) or ids[-1] != segment.get("tailUuid", ids[-1]))
        ):
            ambiguous = True
            warnings.append("source_partial_compaction_relationship_conflict")
            continue
        kept = [
            event
            for key in ids
            if subagent_context or not nodes.get(key, {}).get("isSidechain")
            for event in groups[key]
        ]
        kept_ids = {event.id for event in kept}
        for event in kept:
            event.active = True
        events = [event for event in events if event.id not in kept_ids]
        anchor_ids = {event.id for event in groups[anchor]}
        anchor_index = max(i for i, event in enumerate(events) if event.id in anchor_ids)
        events[anchor_index + 1 : anchor_index + 1] = kept
    related = []
    for child in sorted((path.parent / path.stem / "subagents").glob("*.jsonl")):
        related.append(_subagent_ref(profile, child, ref))
    return finish(
        ref,
        events,
        records,
        warnings,
        ambiguous=ambiguous,
        related=related,
        attachments=attachments_in(records, _root(profile)),
    )
