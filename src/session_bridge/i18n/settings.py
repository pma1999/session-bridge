"""The user's saved preferences, in ``settings.json`` next to the rest of Session Bridge's data."""

from __future__ import annotations

import json
import os
from pathlib import Path


def _path() -> Path:
    # Imported here: profiles depends on errors, which renders through this package.
    from ..profiles import data_home

    return data_home() / "settings.json"


def load() -> dict:
    """Saved settings; a missing or unreadable file means none, and reading never creates anything."""
    try:
        value = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def language() -> str | None:
    value = load().get("language")
    return value if isinstance(value, str) else None


def save_language(lang: str | None) -> Path:
    """Remember ``lang``, or forget the choice with None so the system language applies again."""
    settings = load()
    if lang is None:
        settings.pop("language", None)
    else:
        settings["language"] = lang
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path
