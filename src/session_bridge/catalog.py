"""Shared metadata search and lexical directory identity, without reading conversations."""

from __future__ import annotations

import ntpath
import posixpath
import re
import unicodedata
from pathlib import Path, PureWindowsPath


def search_text(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(character for character in folded if not unicodedata.combining(character))
    return re.sub(r"[\W_]+", " ", plain).strip()


def search_rank(query: str, value: str, *, normalized_value: str | None = None) -> int | None:
    haystack = normalized_value if normalized_value is not None else search_text(value)
    needle = search_text(query)
    if not needle:
        return 0 if not query.strip() or query.casefold() in value.casefold() else None
    if not all(word in haystack for word in needle.split()):
        return None
    return 0 if haystack.startswith(needle) else 1 if needle in haystack else 2


def search_matches(query: str, value: str) -> bool:
    return search_rank(query, value) is not None


def is_subagent(ref) -> bool:
    """A helper agent that runs inside another conversation; a fork is a conversation of its own."""
    metadata = getattr(ref, "metadata", {}) or {}
    if metadata.get("subagent") or metadata.get("spawned"):
        return True
    return ref.tool == "opencode" and bool(ref.parent_id)


def _directory_key(value: str) -> tuple[str, str] | None:
    if not value:
        return None
    if PureWindowsPath(value).is_absolute():
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return "windows", ntpath.normcase(ntpath.normpath(value))
    if value.startswith("/"):
        return "posix", posixpath.normpath(value)
    return None


def same_directory(recorded: str, requested: str) -> bool:
    """Compare complete paths, preserving POSIX case and Windows case/separator rules."""
    right = _directory_key(str(requested))
    if right is None:
        right = _directory_key(str(Path(requested).expanduser().absolute()))
    left = _directory_key(recorded)
    return left is not None and left == right
