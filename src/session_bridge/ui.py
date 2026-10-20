"""Shared terminal wording: tool names, states and dates, without prompt_toolkit."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

TOOL_NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}

LINK_STATES = {
    "synced": "Al día",
    "waiting": "Esperando un turno terminado",
    "conflict": "Hay ramas pendientes de elegir",
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
