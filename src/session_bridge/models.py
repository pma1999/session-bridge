"""Versioned interchange and adapter contracts. Unknown native data stays archival."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Tool = Literal["claude", "opencode", "codex"]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path: Path) -> str:
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Profile(Record):
    id: str
    tool: Tool
    environment: Literal["windows", "wsl", "posix"] = "windows"
    home: str
    executable: str | None = None
    version: str | None = None
    distro: str | None = None
    helper: str | None = None
    machine: str = "local"
    enabled: bool = True


class SessionRef(Record):
    profile_id: str
    native_id: str
    tool: Tool
    title: str = ""
    cwd: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    parent_id: str | None = None
    archived: bool = False
    active: bool | None = None
    locator: str = ""
    version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.profile_id}:{self.native_id}"


class Event(Record):
    id: str
    kind: Literal["message", "summary", "tool", "reasoning", "metadata", "unknown"]
    role: Literal["user", "assistant", "system", "developer", "tool", "unknown"] = "unknown"
    text: str = ""
    timestamp: str | None = None
    parent_id: str | None = None
    branch: str | None = None
    # Effective events are selected by the native reader, never by display order alone.
    active: bool = True
    native: dict[str, Any] = Field(default_factory=dict)


class Attachment(Record):
    id: str
    name: str
    media_type: str = "application/octet-stream"
    # Local managed file, embedded base64, or un-fetched remote reference.
    path: str | None = None
    data_base64: str | None = None
    url: str | None = None
    managed: bool = False
    sha256: str | None = None
    status: str = "available"


class Conversation(Record):
    schema_version: int = 1
    source: SessionRef
    captured_at: str = Field(default_factory=utc_now)
    events: list[Event] = Field(default_factory=list)
    related: list[SessionRef] = Field(default_factory=list)
    related_conversations: list[Conversation] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # Ambiguous branch/compaction requires --context-from or --context all explicitly.
    context_ambiguous: bool = False
    source_fingerprint: str = ""
    raw_records: list[dict[str, Any]] = Field(default_factory=list)


class ProjectedMessage(Record):
    role: Literal["user", "assistant"]
    text: str
    source_ids: list[str] = Field(default_factory=list)
    adapted: bool = False


class Fidelity(Record):
    preserved: list[str] = Field(default_factory=list)
    adapted: list[str] = Field(default_factory=list)
    archive_only: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Projection(Record):
    messages: list[ProjectedMessage]
    fidelity: Fidelity
    context: str = "current"
    context_from: str | None = None


class Capability(Record):
    tool: Tool
    version: str | None
    native: bool = False
    reason: str
    max_messages: int | None = None
    evidence: list[str] = Field(default_factory=list)


class WriteRequest(Record):
    operation_id: str
    profile: Profile
    native_id: str
    cwd: str
    title: str
    messages: list[ProjectedMessage]
    model: str | None = None


class NativeReceipt(Record):
    native_id: str
    profile_id: str
    cwd: str
    fingerprint: str
    locator: str = ""
    created_ids: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


class Transfer(Record):
    id: str
    identity: str
    state: Literal["prepared", "importing", "verifying", "completed", "recovery", "undone"]
    mode: Literal["native", "handoff"]
    source_key: str
    target_profile: str
    target: Profile | None = None
    context_policy: dict[str, Any] = Field(default_factory=dict)
    native_id: str | None = None
    cwd: str
    created_at: str
    updated_at: str
    archive: str
    archive_sha256: str | None = None
    context_file: str | None = None
    initial_message: str | None = None
    receipt: NativeReceipt | None = None
    fidelity: Fidelity
    error: str | None = None
