"""Alternative histories of one conversation, and the full lineage of each.

Editing an earlier message leaves the earlier version on disk: a Claude branch, a superseded
Codex rollout, an OpenCode undo that was not sent yet. Each reader knows which version its
client would resume and marks it with ``Event.active``; this module records the other
versions next to it so the user can pick one, without a new field in the interchange format.

The index lives in ``source.metadata["history"]``, a free-form dict older versions keep as is.
Without an index, a conversation behaves exactly as before: every event is in its lineage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import BridgeError
from .models import digest

if TYPE_CHECKING:
    from .models import Conversation, Event

KEY = "history"
VERSION = 1
MAX_VARIANTS = 64
LEAD_WORDS = 3


@dataclass(frozen=True)
class Version:
    """One history a reader found: the events its model saw, and every event that led there."""

    key: str
    context: frozenset[int]
    lineage: frozenset[int]


@dataclass(frozen=True)
class VariantInfo:
    id: str
    current: bool
    prompt: str
    reply: str
    updated_at: str | None
    messages: int


def _runs(indices):
    runs, start, previous = [], None, None
    for index in sorted(indices):
        if start is None:
            start = previous = index
        elif index == previous + 1:
            previous = index
        else:
            runs.append([start, previous + 1])
            start = previous = index
    if start is not None:
        runs.append([start, previous + 1])
    return runs


def _expand(runs, size):
    if not isinstance(runs, list):
        raise ValueError
    result, last = set(), -1
    for pair in runs:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(type(value) is int for value in pair)
            or not last < pair[0] < pair[1] <= size
        ):
            raise ValueError
        result.update(range(pair[0], pair[1]))
        last = pair[1]
    return result


def _events_digest(events):
    return digest([[event.id, event.kind, event.role] for event in events])


def attach(conversation: Conversation, versions: list[Version], *, current: str) -> None:
    """Record the versions a reader found; the current one must match the events' activity."""
    size = len(conversation.events)
    unique = {}
    for version in versions:
        unique.setdefault(version.key, version)
    chosen = unique.get(current)
    if chosen is None:
        raise ValueError("the current version must be one of the versions")
    everything = frozenset(range(size))
    others = [v for k, v in unique.items() if k != current and v.context != chosen.context]
    if not others and chosen.lineage == everything:
        conversation.source.metadata.pop(KEY, None)
        return
    tool, native = conversation.source.tool, conversation.source.native_id
    entries = []
    for version in [chosen, *others[: MAX_VARIANTS - 1]]:
        entries.append(
            {
                "id": "v_" + digest([tool, native, version.key])[:12],
                "context_on": _runs(version.context - chosen.context),
                "context_off": _runs(chosen.context - version.context),
                "lineage_on": _runs(version.lineage - chosen.lineage),
                "lineage_off": _runs(chosen.lineage - version.lineage),
            }
        )
    if len(others) >= MAX_VARIANTS:
        conversation.warnings.append("source_variants_limited")
    conversation.source.metadata[KEY] = {
        "version": VERSION,
        "events": _events_digest(conversation.events),
        "lineage_excluded": _runs(everything - chosen.lineage),
        "variants": entries,
    }


def _load(conversation):
    """Absolute context and lineage of every variant, the current one first; None without an index."""
    value = conversation.source.metadata.get(KEY)
    if value is None:
        return None
    size = len(conversation.events)
    try:
        if (
            not isinstance(value, dict)
            or value.get("version") != VERSION
            or value.get("events") != _events_digest(conversation.events)
            or not isinstance(value.get("variants"), list)
            or not value["variants"]
        ):
            raise ValueError
        lineage = set(range(size)) - _expand(value["lineage_excluded"], size)
        context = {index for index, event in enumerate(conversation.events) if event.active}
        result, seen = [], set()
        for position, entry in enumerate(value["variants"]):
            identity = entry.get("id") if isinstance(entry, dict) else None
            if not isinstance(identity, str) or identity in seen:
                raise ValueError
            seen.add(identity)
            on, off = _expand(entry["context_on"], size), _expand(entry["context_off"], size)
            gain, loss = _expand(entry["lineage_on"], size), _expand(entry["lineage_off"], size)
            if position == 0 and (on or off or gain or loss):
                raise ValueError
            result.append((identity, frozenset((context - off) | on), frozenset((lineage - loss) | gain)))
        return result
    except (KeyError, TypeError, ValueError):
        if "source_history_index_invalid" not in conversation.warnings:
            conversation.warnings.append("source_history_index_invalid")
        return None


def lineage(conversation: Conversation) -> list[bool]:
    """Whether each event belongs to the history that led to the current context."""
    loaded = _load(conversation)
    if loaded is None:
        return [True] * len(conversation.events)
    return [index in loaded[0][2] for index in range(len(conversation.events))]


def resolve(conversation: Conversation, variant: str) -> str:
    loaded = _load(conversation) or []
    matches = [identity for identity, _, _ in loaded if identity == variant]
    matches = matches or [identity for identity, _, _ in loaded if identity.startswith(variant)]
    if not matches:
        raise BridgeError("variant_not_found", variant=variant)
    if len(matches) > 1:
        raise BridgeError("variant_ambiguous", candidates=matches)
    return matches[0]


def select(conversation: Conversation, variant: str | None) -> Conversation:
    """A copy whose current version is ``variant``; the others stay selectable from it."""
    if variant is None:
        return conversation
    identity = resolve(conversation, variant)
    loaded = _load(conversation)
    if identity == loaded[0][0]:
        return conversation
    _, context, chosen_lineage = next(item for item in loaded if item[0] == identity)
    events = [
        event.model_copy(update={"active": index in context})
        for index, event in enumerate(conversation.events)
    ]
    source = conversation.source.model_copy(update={"metadata": dict(conversation.source.metadata)})
    result = conversation.model_copy(
        update={"events": events, "source": source, "warnings": list(conversation.warnings)}
    )
    size = len(events)
    entries = []
    ordered = [item for item in loaded if item[0] == identity] + [
        item for item in loaded if item[0] != identity
    ]
    for other, other_context, other_lineage in ordered:
        entries.append(
            {
                "id": other,
                "context_on": _runs(other_context - context),
                "context_off": _runs(context - other_context),
                "lineage_on": _runs(other_lineage - chosen_lineage),
                "lineage_off": _runs(chosen_lineage - other_lineage),
            }
        )
    source.metadata[KEY] = {
        "version": VERSION,
        "events": _events_digest(events),
        "lineage_excluded": _runs(set(range(size)) - chosen_lineage),
        "variants": entries,
        "original": loaded[0][0],
    }
    return result


def current(conversation: Conversation) -> str | None:
    loaded = _load(conversation)
    return loaded[0][0] if loaded else None


def original(conversation: Conversation) -> str | None:
    """The variant the source client itself would resume, even after ``select``."""
    value = conversation.source.metadata.get(KEY)
    if isinstance(value, dict) and isinstance(value.get("original"), str):
        return value["original"]
    return current(conversation)


def strip(conversation: Conversation) -> None:
    conversation.source.metadata.pop(KEY, None)


def _prompt(event: Event) -> bool:
    return event.kind == "message" and event.role == "user" and bool(event.text.strip())


def _differing(text, other):
    """The words of ``text`` from the first one that differs from ``other``."""
    words, others = text.split(), other.split()
    common = 0
    while common < min(len(words), len(others)) and words[common] == others[common]:
        common += 1
    if common == len(words) or common == 0:
        return " ".join(words)
    # A few shared words before the change show where in the message it is.
    start = max(0, common - LEAD_WORDS)
    return ("…" if start else "") + " ".join(words[start:])


def describe(conversation: Conversation) -> list[VariantInfo]:
    """What tells the variants apart, the one the source would resume first; empty with only one."""
    loaded = _load(conversation)
    if not loaded or len(loaded) < 2:
        return []
    events = conversation.events
    native = original(conversation)
    loaded = sorted(loaded, key=lambda item: item[0] != native)
    result = []
    for identity, context, own in loaded:
        # Where this version first departs from the closest other one, and what was typed there.
        best = None
        for other, _, other_lineage in loaded:
            if other == identity:
                continue
            mine = next((i for i in sorted(own - other_lineage) if _prompt(events[i])), None)
            theirs = next((i for i in sorted(other_lineage - own) if _prompt(events[i])), None)
            if mine is not None and (best is None or mine > best[0]):
                best = (mine, theirs)
        start, rival = best or (None, None)
        prompt = events[start].text if start is not None else ""
        reply = ""
        if start is not None:
            following = [i for i in sorted(context) if i > start and events[i].kind == "message"]
            reply = next((events[i].text for i in following if events[i].role == "assistant"), "")
            if rival is not None and " ".join(prompt.split()) == " ".join(events[rival].text.split()):
                prompt = ""
            elif rival is not None:
                prompt = _differing(prompt, events[rival].text)
        tail = [i for i in sorted(context) if start is None or i >= start]
        stamps = [events[i].timestamp for i in tail if events[i].timestamp]
        result.append(
            VariantInfo(
                id=identity,
                current=identity == native,
                prompt=" ".join(prompt.split()),
                reply=" ".join(reply.split()),
                updated_at=max(stamps) if stamps else None,
                messages=sum(1 for i in tail if events[i].kind == "message" and events[i].text),
            )
        )
    return result
