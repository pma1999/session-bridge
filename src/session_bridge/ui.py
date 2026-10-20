"""Shared terminal wording: tool names, states and dates, without prompt_toolkit."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

TOOL_NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}

LINK_STATES = {
    "synced": "Al día",
    "waiting": "Esperando un turno terminado",
    "conflict": "Varias continuaciones",
    "paused": "En pausa",
    "unlinked": "Desvinculada; historial conservado",
    "error": "No se puede comprobar el origen",
}

REVISION_REASONS = {"append": "continuación", "context_replacement": "tras una compactación"}

_MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


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
    return visible(text) if text else "Sin título"


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def relative_time(value: str | None, *, now: datetime | None = None) -> str:
    moment = parse_time(value)
    if moment is None:
        return ""
    now = now or datetime.now(UTC)
    seconds = (now - moment).total_seconds()
    if seconds < 60:
        return "ahora"
    if seconds < 3600:
        return f"hace {int(seconds // 60)} min"
    local, today = moment.astimezone(), now.astimezone()
    days = (today.date() - local.date()).days
    if days == 0:
        return f"hace {int(seconds // 3600)} h"
    if days == 1:
        return "ayer"
    if days < 7:
        return f"hace {days} días"
    label = f"{local.day} {_MONTHS[local.month - 1]}"
    return label if local.year == today.year else f"{label} {local.year}"


SHORT_TOOL_NAMES = {"claude": "Claude", "codex": "Codex", "opencode": "OpenCode"}


def compact_time(value: str | None, *, now: datetime | None = None) -> str:
    """relative_time without words that a narrow terminal cannot afford."""
    text = relative_time(value, now=now)
    if text.startswith("hace "):
        text = text[5:].replace(" días", " d")
    return text


def folder_name(value: str) -> str:
    """The project folder's own name, which is how people recognise it."""
    if not value:
        return ""
    path = PureWindowsPath(value) if "\\" in value or ":" in value[:3] else PurePosixPath(value)
    return visible(path.name or value)


# ---------------------------------------------------------------------------
# Synchronized conversations


def link_state(link: dict) -> str:
    if link.get("state") == "conflict":
        return "Dos continuaciones" if len(link.get("heads") or []) <= 2 else "Varias continuaciones"
    state = link.get("state", "")
    return LINK_STATES.get(state, visible(state))


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
            return f"la conversación original de {name}"
        return f"una copia anterior de {name}"
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
    parts = ["La actual" if info.current else "Otra variante"]
    when = relative_time(info.updated_at)
    if when:
        parts.append(when)
    parts.append(f"{info.messages} mensaje" + ("" if info.messages == 1 else "s"))
    if info.prompt:
        parts.append("«" + _excerpt(info.prompt) + "»")
    elif info.reply:
        # The same words were sent again; what came back is what differs.
        parts.append("respuesta «" + _excerpt(info.reply) + "»")
    return " · ".join(parts)
