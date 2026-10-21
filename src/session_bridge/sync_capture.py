"""Select completed native checkpoints without changing the preservation capture."""

from dataclasses import dataclass

from .errors import BridgeError
from .models import Conversation, digest
from .projection import project
from .readers.codex import is_retained_annotation


@dataclass
class Checkpoint:
    conversation: Conversation
    pending: bool


def checkpoint(conversation: Conversation) -> Checkpoint:
    """Use native completion evidence, never elapsed time, as the publication boundary."""
    if conversation.context_ambiguous or any(
        warning.startswith(("source_truncated", "source_changed", "source_invalid_json"))
        for warning in conversation.warnings
    ):
        raise BridgeError(
            "sync_unstable_source",
        )
    result = conversation.model_copy(deep=True)
    if conversation.source.tool == "codex":
        boundary, native_pending = _codex_boundary(conversation)
    else:
        boundary, native_pending = -1, False
        for index, event in enumerate(conversation.events):
            if not event.active:
                continue
            native = event.native
            if conversation.source.tool == "claude":
                message = native.get("message") if isinstance(native.get("message"), dict) else {}
                completed = (
                    event.role == "assistant"
                    and message.get("stop_reason") in {"end_turn", "stop_sequence", "max_tokens", "refusal"}
                ) or (event.kind == "summary" and bool(native.get("isCompactSummary")))
            else:
                data = native.get("data") if isinstance(native.get("data"), dict) else {}
                timing = data.get("time") if isinstance(data.get("time"), dict) else {}
                completed = (event.role == "assistant" and timing.get("completed") is not None) or (
                    event.kind == "summary" and data.get("status") == "completed"
                )
            if completed:
                boundary = index
    if boundary < 0:
        raise BridgeError(
            "sync_pending_turn",
            "error.sync_pending_turn.waiting",
        )
    pending = native_pending or conversation.source.active is True
    for index, event in enumerate(result.events):
        if index > boundary:
            pending |= event.active and event.kind in {"message", "summary", "tool", "reasoning"}
            event.active = False
    try:
        project(result)
    except BridgeError as exc:
        if exc.code != "empty_context":
            raise
        raise BridgeError("sync_pending_turn", "error.sync_pending_turn.context") from None
    return Checkpoint(result, bool(pending))


def _codex_boundary(conversation):
    positions, completed, started = {}, -1, -1
    completed_turns, running_turns = set(), set()
    for index, record in enumerate(conversation.raw_records):
        positions[digest(record)] = index
        if record.get("table") == "thread_turns":
            turn = record.get("row") or {}
            if turn.get("status") in {"completed", "interrupted", "failed"}:
                completed_turns.add(turn.get("turn_id"))
            elif turn.get("status") in {"inProgress", "in_progress", "running"}:
                running_turns.add(turn.get("turn_id"))
        payload = record.get("payload") or {}
        if record.get("type") == "event_msg" and isinstance(payload, dict):
            if payload.get("type") in {"task_complete", "turn_complete", "turn_completed"}:
                completed = index
            elif payload.get("type") in {"task_started", "turn_started"}:
                started = index
        # A persisted replacement context is itself a completed context checkpoint when
        # it was written outside an unfinished turn. Its raw ancestor data stays archival.
        if (
            record.get("type") in {"compacted", "retained_context"}
            and not is_retained_annotation(record)
            and started <= completed
        ):
            completed = index
    boundary = -1
    for index, event in enumerate(conversation.events):
        if not event.active:
            continue
        # Retained replacement items reference their source record instead of copying it.
        position = positions.get(event.native.get("source_record") or digest(event.native))
        row = event.native.get("row") or {}
        if (position is not None and position <= completed) or (
            not positions and row.get("turn_id") in completed_turns
        ):
            boundary = index
        elif row.get("turn_id") in completed_turns and event.native.get("item"):
            # A UI-only paginated capture has no rollout. The native reader already
            # refuses ambiguous retained/compacted histories in that mode.
            if not any(record.get("type") == "response_item" for record in conversation.raw_records):
                boundary = index
    return boundary, started > completed or bool(running_turns)
