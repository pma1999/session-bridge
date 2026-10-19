"""Version-gated facade; unsupported never silently becomes handoff."""

from __future__ import annotations

import platform
import sys
import uuid
from pathlib import Path

from session_bridge.errors import BridgeError
from session_bridge.models import Capability

from . import claude, codex, opencode
from .common import check_version, detect_version, executable, validate_id

VERSIONS = {"claude": claude.VERSION, "opencode": opencode.VERSION, "codex": codex.VERSION}
# Requires observed persistence, presentation, exact resume and model-visible context.
VERIFIED: dict[str, str] = {"claude": "2.1.304", "opencode": "2.0.29", "codex": "0.159.0"}
EVIDENCE = "docs/native-compatibility.md"


def capability(profile):
    version = (profile.version or "").strip()
    if not version and profile.tool == "claude" and profile.executable:
        try:
            version = detect_version(profile) or ""
        except BridgeError:
            version = ""
    exact = VERSIONS[profile.tool]
    matches = version == exact or version.split(" ")[0] == exact or version == f"codex-cli {exact}"
    enabled = matches and VERIFIED.get(profile.tool) == exact
    if not matches:
        reason = "Versión nativa no verificada; selecciona explícitamente el modo handoff."
    else:
        reason = "Compatibilidad nativa pendiente de las cuatro pruebas aisladas; usa handoff explícito."
    if enabled:
        reason = (
            "Persistencia, presentación, reanudación y contexto del modelo verificados para esta versión."
        )
    if enabled and (sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}):
        enabled = False
        reason = "La escritura nativa de esta versión está verificada en Windows x64; en este entorno elige handoff explícito."
    return Capability(
        tool=profile.tool,
        version=version or None,
        native=enabled,
        reason=reason,
        max_messages=9362 if profile.tool == "opencode" and matches else None,
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
        raise BridgeError("empty_projection", "No hay mensajes transferibles.")
    if cap.max_messages is not None and len(request.messages) > cap.max_messages:
        raise BridgeError(
            "native_limit",
            "Esta versión de OpenCode admite como máximo 9362 mensajes por importación nativa. "
            "Conserva el archivo completo y selecciona handoff o un punto de contexto explícito; no se ha truncado nada.",
            max_messages=cap.max_messages,
            message_count=len(request.messages),
        )
    if not Path(request.cwd).is_dir():
        raise BridgeError("cwd_missing", "La carpeta de destino no existe.")
    check_version(request.profile, VERSIONS[request.profile.tool], cwd=request.cwd)
    if request.profile.tool == "claude":
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
        raise BridgeError("profile_mismatch", "El recibo pertenece a otro perfil.")
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
            raise BridgeError(
                "native_verification_failed", "El historial visible Codex no coincide con su contexto."
            )
    if current is None or observed != [(item.role, item.text) for item in messages]:
        raise BridgeError(
            "native_verification_failed", "El historial nativo leído no coincide con la proyección completa."
        )
    if receipt.fingerprint != current.fingerprint:
        raise BridgeError("native_changed", "El destino cambió durante la verificación; reconcilia.")
    current.evidence["verified_messages"] = len(messages)
    return current


def undo(profile, receipt):
    if receipt.profile_id != profile.id:
        raise BridgeError("profile_mismatch", "El recibo pertenece a otro perfil.")
    current = inspect(profile, receipt.native_id)
    if current is None:
        raise BridgeError("native_missing", "El destino ya no existe.")
    if current.fingerprint != receipt.fingerprint:
        raise BridgeError("undo_changed", "El destino cambió; no se eliminará.")
    raise BridgeError(
        "undo_manual_required",
        "No puede verificarse inactividad y propiedad de todos los descendientes; "
        "elimina manualmente la conversación desde el cliente nativo.",
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
        raise BridgeError("native_missing", "El destino no existe; reanudarlo podría crear una sesión vacía.")
    client = executable(profile)
    if profile.tool == "claude":
        return [client, "--resume", native_id]
    if profile.tool == "opencode":
        return [client, cwd, "--session", native_id]
    return [client, "--cd", cwd, "resume", native_id]
