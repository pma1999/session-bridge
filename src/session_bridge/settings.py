"""The user's saved preferences, in ``settings.json`` next to the rest of Session Bridge's data."""

from __future__ import annotations

from pathlib import Path

from . import jsonfile


def path() -> Path:
    # Imported here: profiles depends on errors, which renders through i18n, which reads settings.
    from .profiles import data_home

    return data_home() / "settings.json"


def load() -> dict:
    """Saved settings; a missing or unreadable file means none, and reading never creates anything."""
    return jsonfile.read(path())


def save(key: str, value) -> Path:
    """Remember ``value`` under ``key``, or forget it with None; other settings are kept."""
    settings = load()
    if value is None:
        settings.pop(key, None)
    else:
        settings[key] = value
    return jsonfile.write(path(), settings)
