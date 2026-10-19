"""Version-gated, exclusively published Codex legacy history and native API readback."""

from __future__ import annotations

import contextlib
import hashlib
import json
import queue
import subprocess
import threading
import uuid
from pathlib import Path

import zstandard

from session_bridge import __version__
from session_bridge.archive import atomic_write
from session_bridge.errors import BridgeError
from session_bridge.models import NativeReceipt, utc_now

from .common import env_for_profile, executable, validate_id

VERSION = "0.159.0"
MAX_NATIVE_BYTES = 128 * 1024 * 1024


class _AppServer:
    """Only history/name RPCs; no turn, approval, tool or shell request is ever executed."""

    def __init__(self, profile, cwd):
        self.sequence = 0
        self.output = queue.Queue(maxsize=128)
        self.failed = False
        try:
            self.process = subprocess.Popen(
                [executable(profile), "app-server"],
                cwd=cwd,
                env=env_for_profile(profile),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            raise BridgeError("client_unavailable", "No se pudo iniciar el lector nativo Codex.") from None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.call("initialize", {"clientInfo": {"name": "sessionbridge", "version": __version__}})
            self._send({"method": "initialized", "params": {}})
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            while line := self.process.stdout.readline(MAX_NATIVE_BYTES + 1):
                if len(line) > MAX_NATIVE_BYTES:
                    raise ValueError("RPC limit")
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("RPC object required")
                self.output.put_nowait(value)
        except (OSError, ValueError, queue.Full):
            self.failed = True
        finally:
            try:
                self.output.put_nowait(None)
            except queue.Full:
                self.failed = True

    def _send(self, value):
        try:
            self.process.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except OSError:
            raise BridgeError("native_rpc_failed", "Se interrumpió la comunicación con Codex.") from None

    def call(self, method, params, *, timeout=30):
        import time

        if method not in {"initialize", "thread/read", "thread/name/set"}:
            raise BridgeError(
                "native_rpc_method", "Esta conexión solo permite consultar y nombrar el historial."
            )
        self.sequence += 1
        identity = self.sequence
        self._send({"id": identity, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = self.output.get(timeout=max(0.001, deadline - time.monotonic()))
            except queue.Empty:
                break
            if value is None or self.failed:
                raise BridgeError("native_rpc_failed", "Codex no devolvió una respuesta nativa válida.")
            if "method" in value and "id" in value:
                self._send({"id": value["id"], "error": {"code": -32601, "message": "Actions disabled"}})
                continue
            if value.get("id") == identity:
                if "error" in value or "result" not in value:
                    raise BridgeError("native_rpc_failed", "Codex rechazó la consulta nativa.", method=method)
                return value["result"]
        raise BridgeError("native_timeout", "La verificación nativa Codex agotó el tiempo.", method=method)

    def close(self):
        with contextlib.suppress(OSError, ValueError):
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.reader.join(timeout=1)
        self.process.stdout.close()


def response_items(messages):
    return [
        {
            "type": "message",
            "role": message.role,
            "content": [
                {"type": "input_text" if message.role == "user" else "output_text", "text": message.text}
            ],
        }
        for message in messages
    ]


def _read_rows(profile, native_id):
    validate_id("codex", native_id)
    candidates = []
    for directory in ["sessions", "archived_sessions"]:
        root = Path(profile.home) / directory
        if root.exists():
            candidates.extend(
                path
                for path in root.rglob(f"*{native_id}.jsonl*")
                if path.name.endswith((".jsonl", ".jsonl.zst"))
            )
    if not candidates:
        return None
    if len(candidates) != 1:
        raise BridgeError("native_ambiguous", "Hay varios registros para el identificador nativo.")
    path = candidates[0]
    if path.is_symlink() or not path.resolve().is_relative_to(Path(profile.home).resolve()):
        raise BridgeError("unsafe_native_path", "La ruta nativa sale del perfil seleccionado.")
    try:
        if path.stat().st_size > MAX_NATIVE_BYTES:
            raise BridgeError("native_limit", "El historial nativo supera el límite de lectura explícito.")
        with path.open("rb") as stream:
            body = stream.read(MAX_NATIVE_BYTES + 1)
        if len(body) > MAX_NATIVE_BYTES:
            raise BridgeError(
                "native_limit", "El historial creció por encima del límite de lectura explícito."
            )
        decoded = body
        if path.suffix == ".zst":
            import io

            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(body)) as stream:
                decoded = stream.read(MAX_NATIVE_BYTES + 1)
            if len(decoded) > MAX_NATIVE_BYTES:
                raise BridgeError("native_limit", "El historial descomprimido supera el límite explícito.")
        rows = [json.loads(line) for line in decoded.decode("utf-8").splitlines() if line]
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("invalid row")
        meta = next(row["payload"] for row in rows if row.get("type") == "session_meta")
        if not isinstance(meta, dict):
            raise ValueError("invalid metadata")
    except (ValueError, OSError, StopIteration, KeyError, zstandard.ZstdError):
        raise BridgeError("native_corrupt", "No se puede verificar el registro nativo.") from None
    if meta.get("id") != native_id:
        raise BridgeError("native_corrupt", "El identificador del registro nativo no coincide.")
    return path, body, rows, meta


def inspect(profile, native_id):
    value = _read_rows(profile, native_id)
    if value is None:
        return None
    path, body, rows, meta = value
    provenance = meta.get("session_bridge") or {}
    owned = isinstance(provenance, dict) and provenance.get("imported") is True
    return NativeReceipt(
        native_id=native_id,
        profile_id=profile.id,
        cwd=meta.get("cwd", ""),
        fingerprint=hashlib.sha256(body).hexdigest(),
        locator=str(path),
        created_ids=[native_id],
        evidence={
            "owned": owned,
            "activity": "unknown",
            "operation_ids": [provenance.get("operation_id")] if owned else [],
            "history_mode": meta.get("history_mode", "legacy"),
        },
    )


def messages(profile, native_id):
    value = _read_rows(profile, native_id)
    if value is None:
        raise BridgeError("native_missing", "No existe la conversación nativa Codex.")
    result = []
    for row in value[2]:
        item = row.get("payload") or {}
        if row.get("type") != "response_item" or not isinstance(item, dict):
            continue
        if item.get("type") == "message" and item.get("role") in {"user", "assistant"}:
            content = item.get("content")
            if not isinstance(content, list) or any(
                not isinstance(part, dict) or not isinstance(part.get("text"), str) for part in content
            ):
                raise BridgeError("native_corrupt", "Hay mensajes nativos Codex inválidos.")
            result.append((item["role"], "".join(part["text"] for part in content)))
    return result


def native_history(profile, native_id, cwd, *, title=None):
    rpc = _AppServer(profile, cwd)
    try:
        value = rpc.call("thread/read", {"threadId": native_id, "includeTurns": True})
        thread = value.get("thread", {}) if isinstance(value, dict) else {}
        if thread.get("id") != native_id or not isinstance(thread.get("turns"), list):
            raise BridgeError(
                "native_verification_failed", "Codex no devolvió el historial completo solicitado."
            )
        result = []
        for turn in thread["turns"]:
            if not isinstance(turn, dict) or not isinstance(turn.get("items"), list):
                raise BridgeError(
                    "native_verification_failed", "Codex devolvió un turno de historial inválido."
                )
            for item in turn["items"]:
                if not isinstance(item, dict):
                    raise BridgeError(
                        "native_verification_failed", "Codex devolvió un elemento de historial inválido."
                    )
                if item.get("type") == "userMessage":
                    content = item.get("content", [])
                    if not isinstance(content, list) or any(
                        not isinstance(part, dict)
                        or part.get("type") != "text"
                        or not isinstance(part.get("text"), str)
                        for part in content
                    ):
                        raise BridgeError(
                            "native_verification_failed", "El historial Codex contiene bloques no esperados."
                        )
                    result.append(("user", "".join(part["text"] for part in content)))
                elif item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    result.append(("assistant", item["text"]))
                else:
                    raise BridgeError(
                        "native_verification_failed", "El historial Codex contiene elementos no esperados."
                    )
        if title:
            rpc.call("thread/name/set", {"threadId": native_id, "name": title})
        return result
    finally:
        rpc.close()


def write(request):
    if inspect(request.profile, request.native_id) is not None:
        raise BridgeError("native_conflict", "El identificador ya existe; reconcilia la operación.")
    stamp = utc_now()
    provenance = {"operation_id": request.operation_id, "imported": True, "schema_version": 1}
    rows = [
        {
            "timestamp": stamp,
            "type": "session_meta",
            "payload": {
                "id": request.native_id,
                "session_id": request.native_id,
                "timestamp": stamp,
                "cwd": request.cwd,
                "originator": "sessionbridge",
                "cli_version": VERSION,
                "source": "cli",
                "history_mode": "legacy",
                "session_bridge": provenance,
            },
        }
    ]
    # Each projected message is a closed historical turn, including an initial assistant summary.
    # UI event records and model-visible response records are separate native contracts.
    for index, message in enumerate(request.messages):
        turn_id = str(uuid.uuid5(uuid.UUID(request.native_id), "turn:" + str(index)))
        event = (
            {
                "type": "user_message",
                "message": message.text,
                "images": [],
                "local_images": [],
                "text_elements": [],
            }
            if message.role == "user"
            else {"type": "agent_message", "message": message.text, "phase": None}
        )
        payloads = [
            (
                "event_msg",
                {
                    "type": "task_started",
                    "turn_id": turn_id,
                    "model_context_window": None,
                    "collaboration_mode_kind": "default",
                },
            ),
            ("event_msg", event),
            ("response_item", response_items([message])[0]),
            ("event_msg", {"type": "task_complete", "turn_id": turn_id, "last_agent_message": None}),
        ]
        rows.extend({"timestamp": stamp, "type": kind, "payload": payload} for kind, payload in payloads)
    data = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")
    if len(data) > MAX_NATIVE_BYTES:
        raise BridgeError(
            "native_limit", "El historial supera 128 MiB; selecciona un punto de contexto o handoff."
        )
    directory = Path(request.profile.home) / "sessions" / stamp[:4] / stamp[5:7] / stamp[8:10]
    if not directory.resolve().is_relative_to(Path(request.profile.home).resolve()):
        raise BridgeError("unsafe_native_path", "La ruta nativa sale del perfil seleccionado.")
    path = directory / f"rollout-{stamp[:19].replace(':', '-')}-{request.native_id}.jsonl"
    try:
        atomic_write(path, data)
    except BridgeError as exc:
        if exc.code == "already_exists":
            raise BridgeError(
                "native_conflict", "El identificador ya existe; reconcilia la operación."
            ) from None
        raise
    observed = native_history(request.profile, request.native_id, request.cwd, title=request.title)
    if observed != [(m.role, m.text) for m in request.messages]:
        raise BridgeError(
            "native_verification_failed", "El historial visible Codex no coincide con la proyección completa."
        )
    receipt = inspect(request.profile, request.native_id)
    if receipt is None:
        raise BridgeError("native_missing", "El destino desapareció durante la escritura; reconcilia.")
    return receipt
