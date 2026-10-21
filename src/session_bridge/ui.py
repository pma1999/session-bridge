"""Shared terminal wording: tool names, states and dates, without prompt_toolkit."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from . import i18n

TOOL_NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}

LINK_STATES = ("synced", "waiting", "conflict", "paused", "unlinked", "error")


def visible(value: Any, *, preserve_newlines: bool = False) -> str:
    """Make terminal controls inert while preserving ordinary Unicode text."""
    return "".join(
        ch
        if preserve_newlines and ch == "\n"
        else f"\\x{ord(ch):02x}"
        if ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F
        else ch
        for ch in str(value)
    )


def tool_name(tool: str) -> str:
    return TOOL_NAMES.get(tool, tool)


def profile_label(profile: Any, profiles: list[Any] | None = None) -> str:
    """The assistant's name; the profile ID only when two profiles share a tool."""
    name = tool_name(profile.tool)
    shared = profiles is not None and sum(1 for other in profiles if other.tool == profile.tool) > 1
    environment = getattr(profile, "environment", "windows")
    if shared or environment != "windows":
        detail = profile.id if shared else "WSL" if environment == "wsl" else environment
        return f"{name} ({detail})"
    return name


def title(ref: Any) -> str:
    text = " ".join(str(getattr(ref, "title", "") or "").split())
    return visible(text) if text else i18n.t("ui.untitled")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _elapsed(moment: datetime, now: datetime) -> tuple[str, int]:
    """How long ago, as a unit and an amount: the wording is the catalog's, per language."""
    seconds = (now - moment).total_seconds()
    if seconds < 60:
        return "now", 0
    if seconds < 3600:
        return "minutes", int(seconds // 60)
    days = (now.astimezone().date() - moment.astimezone().date()).days
    if days == 0:
        return "hours", int(seconds // 3600)
    if days == 1:
        return "yesterday", 1
    if days < 7:
        return "days", days
    return "date", 0


def relative_time(value: str | None, *, now: datetime | None = None, compact: bool = False) -> str:
    """``hace 3 min``; ``compact`` drops the words a narrow terminal cannot afford (``3 min``)."""
    moment = parse_time(value)
    if moment is None:
        return ""
    now = now or datetime.now(UTC)
    unit, amount = _elapsed(moment, now)
    if unit != "date":
        return i18n.t(f"time.{unit}{'.short' if compact else ''}", count=amount)
    local = moment.astimezone()
    month = i18n.t(f"time.month.{local.month}")
    if local.year == now.astimezone().year:
        return i18n.t("time.date", day=local.day, month=month)
    return i18n.t("time.date_year", day=local.day, month=month, year=local.year)


SHORT_TOOL_NAMES = {"claude": "Claude", "codex": "Codex", "opencode": "OpenCode"}


def compact_time(value: str | None, *, now: datetime | None = None) -> str:
    """relative_time without words that a narrow terminal cannot afford."""
    return relative_time(value, now=now, compact=True)


def folder_name(value: str) -> str:
    """The project folder's own name, which is how people recognise it."""
    if not value:
        return ""
    path = PureWindowsPath(value) if "\\" in value or ":" in value[:3] else PurePosixPath(value)
    return visible(path.name or value)


# ---------------------------------------------------------------------------
# Synchronized conversations


def link_state(link: dict) -> str:
    state = link.get("state", "")
    if state == "conflict":
        many = len(link.get("heads") or []) > 2
        return i18n.t("link.state.conflict_many" if many else "link.state.conflict_two")
    return i18n.t(f"link.state.{state}") if state in LINK_STATES else visible(state)


def endpoint_state(state: str) -> str:
    """Why one source of a link is not up to date."""
    return i18n.t(f"link.endpoint.{state}") if state in ("waiting", "error", "detached") else visible(state)


def code_text(code: str) -> str:
    """What a stored error code means, with the code itself for searching or reporting it."""
    code = str(code)
    if i18n.has(f"error.{code}", i18n.DEFAULT):
        return i18n.t("ui.code_text", text=i18n.t(f"error.{code}"), code=visible(code))
    return visible(code)


def warning_text(warning: str) -> str:
    """A reader's warning code in words; ``code:detail`` codes carry one value, such as a line.

    Records from before 0.7 may hold a sentence instead of a code; a catalog recognises it.
    """
    code, _, detail = str(warning).partition(":")
    if i18n.has(f"warning.{code}", i18n.DEFAULT):
        return i18n.t(f"warning.{code}", detail=visible(detail))
    if i18n.has(f"error.{code}", i18n.DEFAULT):
        return i18n.t(f"error.{code}")
    return visible(i18n.text(str(warning)))


def value_text(value: Any) -> str:
    """A plain field value in words: yes or no for a flag, a dash for nothing."""
    if isinstance(value, bool):
        return i18n.t("value.yes" if value else "value.no")
    if value is None:
        return i18n.t("value.none")
    return str(value)


def discovery_error(item: Any) -> str:
    """A profile that could not be read, from what discovery recorded (also by older versions)."""
    if not isinstance(item, dict):
        return visible(item)
    key = item.get("key")
    if i18n.is_key(key) and i18n.has(key, i18n.DEFAULT):
        return i18n.t(key)
    if item.get("message"):
        return visible(i18n.text(str(item["message"])))
    return code_text(item.get("code", ""))


def field_label(name: Any) -> str:
    """The label for a field of a result shown as plain key-value lines."""
    key = f"field.{name}"
    return i18n.t(key) if i18n.has(key, i18n.DEFAULT) else visible(name)


def revision_reason(reason: str | None) -> str:
    """Why a revision exists, when that tells the user something (a continuation, a compaction)."""
    return i18n.t(f"link.reason.{reason}") if reason in ("append", "context_replacement") else ""


def _endpoint(link: dict, key: str) -> dict | None:
    return next((item for item in link.get("endpoints") or [] if item.get("key") == key), None)


def endpoint_tool(link: dict, endpoint: dict | None, key: str = "") -> str:
    """The assistant's name for an endpoint, falling back to its profile ID."""
    tool = (endpoint or {}).get("tool")
    return tool_name(tool) if tool else visible((key or (endpoint or {}).get("key", "")).split(":", 1)[0])


def head_origin(head: dict, link: dict) -> str:
    """Where a continuation was typed: an assistant, its original session, or an earlier copy."""
    key = str(head.get("source_key", ""))
    endpoint = _endpoint(link, key)
    name = endpoint_tool(link, endpoint, key)
    if endpoint and endpoint.get("role") == "superseded":
        if endpoint.get("origin") == "original":
            return i18n.t("link.origin.original", name=name)
        return i18n.t("link.origin.earlier_copy", name=name)
    return name


def chosen_head(link: dict) -> dict | None:
    """The continuation that opening follows: the selected one, or the only one."""
    heads = link.get("heads") or []
    selected = next((head for head in heads if head.get("id") == link.get("selected_head")), None)
    return selected or (heads[0] if len(heads) == 1 else None)


def capitalized(text: str) -> str:
    return text[:1].upper() + text[1:]


PREVIEW_CHARS = 60


def _excerpt(text: str, limit: int = PREVIEW_CHARS) -> str:
    text = visible(" ".join(text.split()))
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def variant_label(info: Any) -> str:
    """One line that tells a version of an edited conversation apart from the others.

    What was typed goes last: a narrow terminal cuts it, never when or how long the version is."""
    parts = [i18n.t("variant.current" if info.current else "variant.other")]
    when = relative_time(info.updated_at)
    if when:
        parts.append(when)
    parts.append(i18n.t("variant.messages", count=info.messages))
    if info.prompt:
        parts.append(i18n.quote(_excerpt(info.prompt)))
    elif info.reply:
        # The same words were sent again; what came back is what differs.
        parts.append(i18n.t("variant.reply", text=_excerpt(info.reply)))
    return " · ".join(parts)
