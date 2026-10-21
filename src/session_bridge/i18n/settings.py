"""The saved language choice, kept with the rest of the user's settings."""

from __future__ import annotations

from pathlib import Path


def language() -> str | None:
    from .. import settings

    value = settings.load().get("language")
    return value if isinstance(value, str) else None


def save_language(lang: str | None) -> Path:
    """Remember ``lang``, or forget the choice with None so the system language applies again."""
    from .. import settings

    return settings.save("language", lang)
