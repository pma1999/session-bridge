"""Claude Code JSONL archive and explicit parent/leaf context selection."""

from __future__ import annotations

from session_bridge.errors import BridgeError
from session_bridge.models import SessionRef

from .common import attachments_in, block_events, capture, finish, guard, iter_records, root, scoped


def _root(profile):
    return root(profile, "projects", (".claude",))


def _blocks(record):
    content = (
        (record.get("message") or {}).get("content") if isinstance(record.get("message"), dict) else None
    )
    return content if isinstance(content, list) else []


def _tool_result_of(record, parent):
    """Whether a user record holds only results of the tool_use blocks in its assistant parent."""
    blocks = _blocks(record)
    if record.get("type") != "user" or not blocks or not parent or parent.get("type") != "assistant":
        return False
    uses = {
        block.get("id")
        for block in _blocks(parent)
        if isinstance(block, dict) and block.get("type") == "tool_use"
    }
    return all(
        isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") in uses
        for block in blocks
    )


class _Graph:
    """Claude records one API response as several assistant records sharing message.id, each
    tool_use chained after the previous one. Each result hangs off its own tool_use, and the turn
    continues from whichever result was written last, so the other results are parallel leaves."""

    def __init__(self, nodes, children, continued):
        self.nodes, self.children, self.continued = nodes, children, continued
        self.turns = {}
        for key, record in nodes.items():
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            if record.get("type") == "assistant" and message.get("id"):
                self.turns.setdefault(message["id"], set()).add(key)

    def turn(self, key):
        message = self.nodes[key].get("message") if isinstance(self.nodes[key].get("message"), dict) else {}
        return self.turns.get(message.get("id"), {key})

    def response(self, key):
        """The turn, or for records without a message id, the chain of assistant records around it."""
        turn = self.turn(key)
        if len(turn) > 1:
            return turn
        found, pending = set(), [key]
        while pending:
            current = pending.pop()
            if current in found or self.nodes.get(current, {}).get("type") != "assistant":
                continue
            found.add(current)
            pending.append(self.nodes[current].get("parentUuid"))
            pending.extend(self.children.get(current, []))
        return found or {key}

    def parallel_result(self, key):
        record = self.nodes[key]
        parent = self.nodes.get(record.get("parentUuid"))
        if key in self.continued or not _tool_result_of(record, parent):
            return False
        # Another call of the same response has its own result; a record that only continues the
        # response (its next text or tool_use block) does not make this result a parallel one.
        for member in self.response(parent["uuid"]):
            for child in self.children.get(member, []):
                if child != key and _tool_result_of(self.nodes[child], self.nodes[member]):
                    return True
        return False


INTERRUPTIONS = {"[Request interrupted by user]", "[Request interrupted by user for tool use]"}


def _interruption(record):
    content = (
        (record.get("message") or {}).get("content") if isinstance(record.get("message"), dict) else None
    )
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else _blocks(record)
    return (
        record.get("type") == "user"
        and bool(blocks)
        and all(
            isinstance(b, dict) and b.get("type") == "text" and b.get("text") in INTERRUPTIONS for b in blocks
        )
    )


def _descendants(start, children):
    found, pending = set(), [start]
    while pending:
        key = pending.pop()
        if key not in found:
            found.add(key)
            pending.extend(children.get(key, []))
    return found


QUEUED_HEADER = "[Delivered to the session while it was working; origin: {}]\n"


def _queued_prompt(record):
    """Text of a prompt queued during a turn, which the client delivers to the model as input."""
    attachment = record.get("attachment") if record.get("type") == "attachment" else None
    if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
        return None
    prompt = attachment.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    origin = attachment.get("origin")
    origin = (
        (origin.get("kind") if isinstance(origin, dict) else origin)
        or attachment.get("commandMode")
        or "client"
    )
    origin = origin if isinstance(origin, str) else "client"
    return prompt if origin == "human" else QUEUED_HEADER.format(origin) + prompt


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
    children = {}
    for key, record in nodes.items():
        if subagent_context or not record.get("isSidechain"):
            # A compaction boundary starts a new root that continues its logical parent's tip.
            parent = record.get("parentUuid")
            if parent is None and record.get("logicalParentUuid") in nodes:
                parent = record["logicalParentUuid"]
            children.setdefault(parent, []).append(key)
    conversational = {
        key
        for key, record in nodes.items()
        if record.get("type") in ("user", "assistant") and (subagent_context or not record.get("isSidechain"))
    }

    # Timing, hook and summary records may follow a reply; only conversation descendants continue it.
    # A compaction boundary starts a new root and names the tip it continues as its logical parent.
    def upward(key):
        record = nodes[key]
        if record.get("parentUuid") is None and record.get("logicalParentUuid") in nodes:
            return record["logicalParentUuid"]
        return record.get("parentUuid")

    continued = set()
    for key in conversational:
        current = upward(key)
        while current in nodes and current not in continued:
            continued.add(current)
            current = upward(current)
    graph = _Graph(nodes, children, continued)
    order = {key: index for index, key in enumerate(nodes)}
    leaves = [key for key in nodes if key in conversational and key not in continued]
    # A turn companion (such as an image scaling note) rides beside its record and never forks the turn.
    # Client failure notices are never sent to the model, so a retry beside one does not fork the turn.
    candidates = [
        key
        for key in leaves
        if not graph.parallel_result(key)
        and not nodes[key].get("turnCompanion")
        and not nodes[key].get("isApiErrorMessage")
    ]
    # An interrupted request that the client retried from an earlier point is a superseded attempt.
    candidates = [
        key
        for position, key in enumerate(candidates)
        if not (_interruption(nodes[key]) and position < len(candidates) - 1)
    ]
    explicit = [
        r["leafUuid"] for r in records if isinstance(r.get("leafUuid"), str) and r["leafUuid"] in nodes
    ]
    ambiguous = bool(issues)
    if explicit:
        # The pointer names the newest record when it was written. Its descendants hold the rest of
        # the turn; a parallel result sits beside the continuation, so anchor at the turn's start.
        anchor = explicit[-1]
        if graph.parallel_result(anchor):
            anchor = min(graph.turn(nodes[anchor]["parentUuid"]), key=order.get)
        scope = _descendants(anchor, children)
        within = [key for key in candidates if key in scope]
        ambiguous |= len(within) > 1
        pending = [key for key in leaves if key in scope]
        selected = within[-1] if within else (pending[-1] if pending else explicit[-1])
    else:
        ambiguous |= bool(nodes) and len(candidates) > 1
        # A turn cut off while its parallel results arrived has no continuation yet; its newest
        # result still reaches every call through the turn's records.
        selected = candidates[-1] if candidates else (leaves[-1] if leaves else None)
        ambiguous |= bool(nodes) and selected is None
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
    # Every record of an active response belongs to the context, and so does each tool result that
    # hangs off one of them beside the continuing chain, with the companion notes of those results.
    for key in [key for key in active if nodes.get(key, {}).get("type") == "assistant"]:
        active |= graph.turn(key)
    active |= {
        key
        for key, record in nodes.items()
        if key in conversational
        and record.get("parentUuid") in active
        and _tool_result_of(record, nodes.get(record.get("parentUuid")))
    }
    active |= {
        key
        for key, record in nodes.items()
        if key in conversational and record.get("turnCompanion") and record.get("parentUuid") in active
    }
    events, groups, queued = [], {}, []
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
        if record.get("isApiErrorMessage"):
            # Client-generated failure notices are shown to the user but never sent to the model.
            event_kind = "metadata"
        prompt = _queued_prompt(record)
        if prompt is not None:
            role, event_kind, content = "user", "message", prompt
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
        if prompt is not None:
            queued.append((len(events), emitted, record["attachment"]["prompt"].strip()))
    # A queued prompt the client also recorded later as a regular user message is already in the
    # context; the same words typed earlier in the conversation are a separate prompt.
    for position, emitted, prompt in queued:
        # Only the user message recorded right after it, before the model answers, can be the same delivery.
        following = None
        for event in events[position:]:
            if not event.active or event.kind not in ("message", "summary"):
                continue
            if event.role == "assistant" or event.native.get("type") == "user":
                following = event
                break
        if following is not None and following.role == "user" and following.text.strip() == prompt:
            for event in emitted:
                event.kind, event.active = "metadata", False
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
