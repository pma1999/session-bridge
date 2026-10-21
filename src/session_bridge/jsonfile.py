"""Small JSON documents Session Bridge keeps for itself, such as settings and the update state."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

MAX_BYTES = 64 * 1024


def read(path: Path) -> dict:
    """The document's object; a missing, oversized, unreadable or non-object file reads as empty."""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        value = json.loads(raw) if len(raw) <= MAX_BYTES else None
    except (OSError, ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write(path: Path, value: dict) -> Path:
    """Replace the document atomically; a concurrent writer never sees, or leaves, a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path
