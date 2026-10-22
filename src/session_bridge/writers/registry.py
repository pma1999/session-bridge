"""Version-gated facade; unsupported never silently becomes handoff.

A version is ``verified`` when it passed the four isolated checks itself, and ``compatible`` when it is
newer than the oldest verified one with the same major number: the format each writer emits is then
checked on every transfer (Claude Code before writing, against the sessions that version wrote;
Codex by having the client read the session back; OpenCode through its own import and a readback of
its database), so a client update needs a new release only when its format changes.
"""

from __future__ import annotations

import platform
import sys
import uuid
from pathlib import Path

from session_bridge.errors import BridgeError
from session_bridge.models import Capability

from . import claude, codex, opencode
from .common import check_version, detect_version, executable, semver, validate_id

VERSIONS = {"claude": claude.VERSION, "opencode": opencode.VERSION, "codex": codex.VERSION}
# Each passed observed persistence, presentation, exact resume and model-visible context.
VERIFIED: dict[str, tuple[str, ...]] = {
    "claude": ("2.1.304", "2.1.308"),
    "opencode": ("2.0.29", "2.1.3"),
    "codex": ("0.159.0", "0.163.0"),
}
EVIDENCE = "docs/native-compatibility.md"
REASONS = {"verified": "capability.verified", "compatible": "capability.compatible"}
# Reasons no version gives any more, which stored transfers and older WSL helpers still carry.
RETIRED_REASONS = ("capability.pending_checks",)


def status(tool, version):
    """``verified``, ``compatible`` or ``None`` for a version string such as ``codex-cli 0.163.0``."""
    found = semver(version or "")
    if found is None:
        return None
    verified = [semver(item) for item in VERIFIED[tool]]
    if found in verified:
        return "verified"
    oldest = min(verified)
    return "compatible" if found[0] == oldest[0] and found > oldest else None


def capability(profile):
    version = (profile.version or "").strip()
    if not version and profile.tool == "claude" and profile.executable:
        try:
            version = detect_version(profile) or ""
        except BridgeError:
            version = ""
    state = status(profile.tool, version)
    enabled = state is not None
    reason = REASONS.get(state, "capability.unverified_version")
    if enabled and (sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}):
        enabled = False
        reason = "capability.windows_x64_only"
    return Capability(
        tool=profile.tool,
        version=version or None,
        native=enabled,
        reason=reason,
        max_messages=9362 if profile.tool == "opencode" and state else None,
        evidence=[EVIDENCE],
    )


def allocate_id(profile):
    value = uuid.uuid4()
    return "ses_" + value.hex if profile.tool == "opencode" else str(value)


def write(request):
    cap = capability(request.profile)
    if not cap.native:
        raise BridgeError(
            "native_unsupported", cap.reason, tool=request.profile.tool, version=request.profile.version
        )
    validate_id(request.profile.tool, request.native_id)
    if not request.messages:
        raise BridgeError("empty_projection")
    if cap.max_messages is not None and len(request.messages) > cap.max_messages:
        raise BridgeError(
            "native_limit",
            "error.native_limit.opencode_messages",
            max_messages=cap.max_messages,
            message_count=len(request.messages),
        )
    if not Path(request.cwd).is_dir():
        raise BridgeError("cwd_missing", "error.cwd_missing.target")
    installed = check_version(request.profile, lambda found: status(request.profile.tool, found), cwd=request.cwd)
    if request.profile.tool == "claude":
        if status("claude", installed) == "compatible":
            claude.check_format(request.profile, installed)
        receipt = claude.write(request)
    elif request.profile.tool == "opencode":
        receipt = opencode.write(request)
    else:
        receipt = codex.write(request)
    return verify(request.profile, receipt, request.messages)


def inspect(profile, native_id):
    validate_id(profile.tool, native_id)
    return {"claude": claude.inspect, "opencode": opencode.inspect, "codex": codex.inspect}[profile.tool](
        profile, native_id
    )


def verify(profile, receipt, messages):
    if receipt.profile_id != profile.id:
        raise BridgeError("profile_mismatch", "error.profile_mismatch.receipt")
    if profile.tool == "opencode":
        result = opencode.readback(profile, receipt.native_id)
        current, observed = result if result else (None, [])
    elif profile.tool == "claude":
        current = claude.inspect(profile, receipt.native_id)
        observed = claude.messages(profile, receipt.native_id) if current else []
    else:
        current = codex.inspect(profile, receipt.native_id)
        observed = codex.messages(profile, receipt.native_id) if current else []
        if current and codex.native_history(profile, receipt.native_id, receipt.cwd) != observed:
            raise BridgeError("native_verification_failed", "error.native_verification_failed.codex_context")
    if current is None or observed != [(item.role, item.text) for item in messages]:
        raise BridgeError("native_verification_failed", "error.native_verification_failed.projection")
    if receipt.fingerprint != current.fingerprint:
        raise BridgeError("native_changed")
    current.evidence["verified_messages"] = len(messages)
    return current


def undo(profile, receipt):
    if receipt.profile_id != profile.id:
        raise BridgeError("profile_mismatch", "error.profile_mismatch.receipt")
    current = inspect(profile, receipt.native_id)
    if current is None:
        raise BridgeError("native_missing", "error.native_missing.target")
    if current.fingerprint != receipt.fingerprint:
        raise BridgeError("undo_changed", "error.undo_changed.target")
    raise BridgeError(
        "undo_manual_required",
        native_id=receipt.native_id,
    )


def resume_argv(profile, native_id, cwd):
    validate_id(profile.tool, native_id)
    current = (
        opencode.inspect(profile, native_id, cwd=cwd)
        if profile.tool == "opencode"
        else inspect(profile, native_id)
    )
    if current is None:
        raise BridgeError("native_missing", "error.native_missing.resume_empty")
    client = executable(profile)
    if profile.tool == "claude":
        return [client, "--resume", native_id]
    if profile.tool == "opencode":
        return [client, cwd, "--session", native_id]
    return [client, "--cd", cwd, "resume", native_id]
