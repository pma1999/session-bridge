"""Claude's private JSONL format, enabled only by exact-version runtime gates."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from session_bridge.archive import atomic_write
from session_bridge.errors import BridgeError
from session_bridge.models import NativeReceipt, digest, utc_now

from .common import validate_id

VERSION = "2.1.304"


def transcript(request):
    validate_id("claude", request.native_id)
    parent, rows, stamp = None, [], utc_now()
    for index, message in enumerate(request.messages):
        item_id = str(uuid.uuid5(uuid.UUID(request.native_id), str(index)))
        payload = {"role": message.role, "content": [{"type": "text", "text": message.text}]}
        if message.role == "assistant":
            payload.update(
                {
                    "id": f"msg_{item_id.replace('-', '')}",
                    "type": "message",
                    "model": request.model or "imported",
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            )
        rows.append(
            {
                "type": message.role,
                "message": payload,
                "uuid": item_id,
                "parentUuid": parent,
                "sessionId": request.native_id,
                "isSidechain": False,
                "cwd": request.cwd,
                "version": VERSION,
                "timestamp": stamp,
                "sessionBridge": {"operation_id": request.operation_id, "imported": True},
            }
        )
        parent = item_id
    if request.title:
        rows.append({"type": "custom-title", "customTitle": request.title, "sessionId": request.native_id})
    return rows


def locate(profile, native_id):
    validate_id("claude", native_id)
    root = Path(profile.home) / "projects"
    candidates = list(root.glob(f"*/{native_id}.jsonl")) if root.exists() else []
    if len(candidates) > 1:
        raise BridgeError("native_ambiguous", "Hay varios registros para el mismo identificador nativo.")
    if not candidates:
        return None
    target = candidates[0]
    if target.is_symlink() or not target.resolve().is_relative_to(Path(profile.home).resolve()):
        raise BridgeError("unsafe_native_path", "La ruta nativa sale del perfil seleccionado.")
    return target


def read_rows(profile, native_id):
    path = locate(profile, native_id)
    if path is None:
        return None
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("invalid native row")
    except (ValueError, OSError):
        raise BridgeError("native_corrupt", "No se puede verificar el registro nativo.") from None
    return path, rows


def inspect(profile, native_id):
    value = read_rows(profile, native_id)
    if value is None:
        return None
    path, rows = value
    subtree = {path.name: path.read_bytes().hex()}
    children = path.with_suffix("")
    if children.exists():
        if children.is_symlink() or not children.resolve().is_relative_to(Path(profile.home).resolve()):
            raise BridgeError("unsafe_native_path", "El subárbol nativo sale del perfil seleccionado.")
        for item in sorted(children.rglob("*")):
            if item.is_symlink() or not item.resolve().is_relative_to(Path(profile.home).resolve()):
                raise BridgeError("unsafe_native_path", "El subárbol nativo contiene un enlace simbólico.")
            if item.is_file():
                subtree[str(item.relative_to(path.parent))] = item.read_bytes().hex()
    provenance = [row.get("sessionBridge") for row in rows if row.get("type") in {"user", "assistant"}]
    owned = bool(provenance) and all(isinstance(p, dict) and p.get("imported") is True for p in provenance)
    cwd = next((row.get("cwd", "") for row in rows if row.get("cwd")), "")
    return NativeReceipt(
        native_id=native_id,
        profile_id=profile.id,
        cwd=cwd,
        fingerprint=digest(subtree),
        locator=str(path),
        created_ids=[native_id],
        evidence={
            "owned": owned,
            "activity": "unknown",
            "subtree_files": len(subtree),
            "operation_ids": sorted({p.get("operation_id", "") for p in provenance if p}),
        },
    )


def write(request):
    if locate(request.profile, request.native_id):
        raise BridgeError("native_conflict", "El identificador ya existe; reconcilia la operación.")
    project_name = os.environ.get("CLAUDE_CODE_PROJECT_DIR_NAME") or re.sub(r"[^a-zA-Z0-9]", "-", request.cwd)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,200}", project_name):
        raise BridgeError(
            "native_path_unsupported",
            "La codificación de carpeta Claude requiere un formato no verificado; usa handoff.",
        )
    directory = Path(request.profile.home) / "projects" / project_name
    root = Path(request.profile.home).resolve()
    if not directory.resolve().is_relative_to(root):
        raise BridgeError("unsafe_native_path", "La ruta nativa sale del perfil seleccionado.")
    path = directory / f"{request.native_id}.jsonl"
    try:
        data = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in transcript(request)).encode(
            "utf-8"
        )
        atomic_write(path, data)
    except BridgeError as exc:
        if exc.code == "already_exists":
            raise BridgeError(
                "native_conflict", "El identificador ya existe; reconcilia la operación."
            ) from None
        raise
    result = inspect(request.profile, request.native_id)
    if result is None:
        raise BridgeError("native_missing", "El destino desapareció durante la escritura; reconcilia.")
    return result


def messages(profile, native_id):
    value = read_rows(profile, native_id)
    if value is None:
        raise BridgeError("native_missing", "No existe la conversación nativa.")
    result = []
    for row in value[1]:
        if row.get("type") not in {"user", "assistant"}:
            continue
        payload = row.get("message", {})
        if not isinstance(payload, dict):
            raise BridgeError("native_corrupt", "Hay mensajes nativos inválidos.")
        content = payload.get("content", [])
        if not isinstance(content, (str, list)):
            raise BridgeError("native_corrupt", "Hay mensajes nativos inválidos.")
        if isinstance(content, list) and any(not isinstance(part, dict) for part in content):
            raise BridgeError("native_corrupt", "Hay bloques nativos inválidos.")
        text = (
            content
            if isinstance(content, str)
            else "".join(part.get("text", "") for part in content if part.get("type") == "text")
        )
        result.append((payload.get("role"), text))
    return result
