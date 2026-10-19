"""Fixed JSON protocol adapter for the Linux side of WSL calls."""

from __future__ import annotations

import json
import re
import sys
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from .errors import BridgeError
from .models import NativeReceipt, Profile, ProjectedMessage, SessionRef, WriteRequest

MAX_REQUEST_BYTES = 64 * 1024 * 1024
OPS = {"discover", "read", "capability", "write", "verify", "inspect", "undo", "resume_argv"}


class Packet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: int
    op: str
    args: dict[str, Any]


class ProfileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: Profile


class ReadArgs(ProfileArgs):
    ref: SessionRef
    snapshot: bool = False


class WriteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: WriteRequest


class VerifyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: Profile
    receipt: NativeReceipt
    messages: list[ProjectedMessage]


class InspectArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: Profile
    native_id: str


class UndoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: Profile
    receipt: NativeReceipt


class ResumeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: Profile
    native_id: str
    cwd: str


def _profile(profile: Profile) -> Profile:
    if (
        profile.environment != "wsl"
        or not profile.distro
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}", profile.distro)
        or profile.distro.casefold() in {"docker-desktop", "docker-desktop-data"}
        or not PurePosixPath(profile.home).is_absolute()
        or "\x00" in profile.home
        or not profile.helper
        or not PurePosixPath(profile.helper).is_absolute()
        or "\x00" in profile.helper
    ):
        raise BridgeError(
            "invalid_wsl_profile", "El helper solo atiende perfiles WSL con distribución configurada."
        )
    return profile


def dispatch(op: str, raw_args: dict[str, Any]) -> Any:
    """Validate one allowlisted operation, then delegate to local native adapters."""
    if op not in OPS:
        raise BridgeError("unsupported_helper_operation", "La operación no está permitida para el helper.")
    try:
        if op in {"discover", "capability"}:
            args = ProfileArgs.model_validate(raw_args)
            profile = _profile(args.profile)
            if op == "capability":
                from .writers.registry import capability

                return capability(profile)
            module = __import__(f"session_bridge.readers.{profile.tool}", fromlist=["discover"])
            return module.discover(profile)
        if op == "read":
            args = ReadArgs.model_validate(raw_args)
            profile = _profile(args.profile)
            if args.ref.profile_id != profile.id or args.ref.tool != profile.tool:
                raise BridgeError("profile_mismatch", "La sesión no pertenece al perfil indicado.")
            module = __import__(f"session_bridge.readers.{profile.tool}", fromlist=["read"])
            return module.read(profile, args.ref, snapshot=args.snapshot)
        if op == "write":
            args = WriteArgs.model_validate(raw_args)
            _profile(args.request.profile)
            from .writers.registry import write

            return write(args.request)
        if op == "verify":
            args = VerifyArgs.model_validate(raw_args)
            profile = _profile(args.profile)
            from .writers.registry import verify

            return verify(profile, args.receipt, args.messages)
        if op == "inspect":
            args = InspectArgs.model_validate(raw_args)
            from .writers.registry import inspect

            return inspect(_profile(args.profile), args.native_id)
        if op == "undo":
            args = UndoArgs.model_validate(raw_args)
            from .writers.registry import undo

            return undo(_profile(args.profile), args.receipt)
        args = ResumeArgs.model_validate(raw_args)
        from .writers.registry import resume_argv

        return resume_argv(_profile(args.profile), args.native_id, args.cwd)
    except ValidationError:
        raise BridgeError(
            "invalid_helper_arguments", "Los argumentos del helper no cumplen el protocolo v1."
        ) from None


def main() -> None:
    """Read one bounded JSON packet and write exactly one JSON response."""
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    try:
        if len(raw) > MAX_REQUEST_BYTES:
            raise BridgeError("request_too_large", "La solicitud supera el límite del protocolo.")
        packet = Packet.model_validate_json(raw)
        if packet.protocol_version != 1:
            raise BridgeError("unsupported_protocol", "La versión del protocolo no está soportada.")
        data = dispatch(packet.op, packet.args)
        response = {"protocol_version": 1, "ok": True, "data": _jsonable(data)}
    except BridgeError as exc:
        response = {"protocol_version": 1, "ok": False, "error": exc.as_dict()}
    except (ValidationError, ValueError):
        error = BridgeError("invalid_helper_request", "La solicitud del helper no cumple el protocolo v1.")
        response = {"protocol_version": 1, "ok": False, "error": error.as_dict()}
    except Exception:  # noqa: BLE001 - protocol responses must not leak traces or session content
        error = BridgeError("helper_operation_failed", "El helper no pudo completar la operación.")
        response = {"protocol_version": 1, "ok": False, "error": error.as_dict()}
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


if __name__ == "__main__":
    main()
