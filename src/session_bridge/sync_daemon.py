"""A cooperative, generation-scoped local synchronization worker.

The lifetime byte lock is the authority; neither a PID nor a JSON receipt proves
ownership. Its unlocked suffix records the generation held by that lock. A
separate control lock serializes managed start/stop without blocking the worker.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import i18n
from .errors import BridgeError

_START_TIMEOUT = 10.0
_STOP_TIMEOUT = 10.0
_CONTROL_TIMEOUT = 12.0
_HEARTBEAT_SECONDS = 2.0
_STALE_SECONDS = 30.0
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")


def _service(data_dir):
    from .sync import SyncService

    return SyncService(data_dir)


def _root(data_dir: Path | None, *, create: bool = True) -> Path:
    from .profiles import data_home

    try:
        root = (Path(data_dir) if data_dir is not None else data_home()).expanduser().resolve()
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return root
    except (OSError, ValueError):
        raise BridgeError("sync_worker_io", "error.sync_worker_io.folder") from None


class _Lock:
    def __init__(self, path: Path):
        self.path = path
        self.stream = None

    def acquire(self) -> bool:
        if self.path.is_symlink():
            raise BridgeError("sync_worker_io", "error.sync_worker_io.lock_path")
        stream = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600), "r+b", buffering=0)
        try:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.stream = stream
        return True

    def owner(self, generation: str) -> None:
        self.stream.seek(1)
        self.stream.write(generation.encode("ascii"))
        self.stream.truncate()
        os.fsync(self.stream.fileno())

    def release(self) -> None:
        if self.stream is None:
            return
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
            self.stream = None


def _read(path: Path) -> dict:
    try:
        with path.open("rb") as stream:
            raw = stream.read(8193)
        value = json.loads(raw) if len(raw) <= 8192 else None
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _write(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _owner(root: Path) -> tuple[bool, str | None]:
    lock = _Lock(root / "worker.lock")
    if lock.acquire():
        lock.release()
        return False, None
    try:
        with (root / "worker.lock").open("rb") as stream:
            stream.seek(1)
            token = stream.read(33).decode("ascii")
        return True, token if _TOKEN.fullmatch(token) else None
    except (OSError, UnicodeError):
        return True, None


def status(data_dir: Path | None = None) -> dict:
    """Report only bounded metadata authenticated by the held lifetime lock."""
    root = _root(data_dir, create=False)
    if not root.is_dir():
        return {"running": False, "state": "stopped"}
    try:
        held, generation = _owner(root)
        receipt = _read(root / "worker.json")
        if not held:
            stale = receipt.get("state") == "running"
            return {"running": False, "state": "stale" if stale else "stopped"}
        valid = generation is not None and receipt.get("generation") == generation
        held_again, owner_again = _owner(root)
        if not held_again or owner_again != generation:
            return {"running": False, "state": "changing"}
        heartbeat = receipt.get("heartbeat") if valid else None
        age = time.time() - heartbeat if isinstance(heartbeat, (int, float)) else math.inf
        healthy = valid and math.isfinite(age) and -5 <= age <= _STALE_SECONDS
        result = {"running": True, "state": "running" if healthy else "unresponsive", "healthy": healthy}
        if generation:
            result["generation"] = generation
        if valid:
            for key in ("pid", "heartbeat", "started_at", "last_error"):
                value = receipt.get(key)
                if key == "last_error":
                    value = _error_code(value) if value else None
                elif not isinstance(value, (int, float)) or not math.isfinite(value):
                    continue
                result[key] = value
        return result
    except OSError:
        raise BridgeError("sync_worker_io", "error.sync_worker_io.query") from None


def _control(root: Path) -> _Lock:
    lock = _Lock(root / "worker-control.lock")
    deadline = time.monotonic() + _CONTROL_TIMEOUT
    while not lock.acquire():
        if time.monotonic() >= deadline:
            raise BridgeError("sync_worker_busy")
        time.sleep(0.05)
    return lock


def start(data_dir: Path | None = None) -> dict:
    """Start one managed generation and verify its lock and ready receipt."""
    root = _root(data_dir)
    control = _control(root)
    try:
        current = status(root)
        if current["running"]:
            if current.get("healthy"):
                return {**current, "started": False}
            raise BridgeError("sync_worker_unresponsive")
        generation = uuid.uuid4().hex
        env = i18n.child_environment(os.environ.copy())
        env["SESSIONBRIDGE_HOME"] = str(root)
        # Preserve PYTHONPATH: editable/source-tree runtimes require it. No global
        # environment, native discovery variables, startup tasks or shells change.
        argv = [sys.executable, "-m", "session_bridge.sync_daemon", "--generation", generation]
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=str(Path(__file__).resolve().parent.parent),
            close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        deadline = time.monotonic() + _START_TIMEOUT
        while time.monotonic() < deadline:
            current = status(root)
            if current.get("generation") == generation and current.get("healthy"):
                return {**current, "started": True}
            if process.poll() is not None:
                break
            time.sleep(0.05)
        # Cooperative cancellation also handles a child delayed until after timeout.
        _write(root / f"worker-stop-{generation}.json", {"generation": generation})
        raise BridgeError("sync_worker_start_failed")
    except OSError:
        raise BridgeError("sync_worker_io", "error.sync_worker_io.start") from None
    finally:
        control.release()


def stop(data_dir: Path | None = None) -> dict:
    """Request exact-generation shutdown; never signal or kill a PID."""
    root = _root(data_dir)
    control = _control(root)
    try:
        current = status(root)
        if not current["running"]:
            return {**current, "acknowledged": False}
        generation = current.get("generation")
        if not generation:
            raise BridgeError("sync_worker_identity")
        _write(root / f"worker-stop-{generation}.json", {"generation": generation})
        deadline = time.monotonic() + _STOP_TIMEOUT
        while time.monotonic() < deadline:
            ack = _read(root / f"worker-ack-{generation}.json")
            held, owner = _owner(root)
            if (not held or owner != generation) and ack.get("generation") != generation:
                # The worker publishes its acknowledgement before releasing its
                # lifetime lock. Re-read after observing that release so a stop
                # between our two reads cannot be misreported as a stale crash.
                ack = _read(root / f"worker-ack-{generation}.json")
            if ack.get("generation") == generation and (not held or owner != generation):
                return {**status(root), "stopped_generation": generation, "acknowledged": True}
            if not held and not ack:
                return {"running": False, "state": "stale", "acknowledged": False}
            time.sleep(0.05)
        raise BridgeError("sync_worker_stop_timeout")
    except OSError:
        raise BridgeError("sync_worker_io", "error.sync_worker_io.stop") from None
    finally:
        control.release()


def _error_code(value) -> str:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value)
        else "sync_poll_failed"
    )


def _poll(service) -> dict:
    try:
        return service.poll()
    except Exception as exc:  # Never persist exception text or session bodies.
        code = _error_code(exc.code) if isinstance(exc, BridgeError) else "sync_poll_failed"
        return {
            "changed": 0,
            "links": [],
            "errors": [{"code": code, "message": i18n.t("error.sync_poll_failed")}],
        }


def run(
    data_dir: Path | None = None, *, once: bool = False, interval: float = 2.0, _generation: str | None = None
) -> dict:
    """Reconcile periodically; filesystem silence never determines completion."""
    if not math.isfinite(interval) or not 0.1 <= interval <= 3600:
        raise BridgeError("sync_interval")
    root = _root(data_dir)
    if once:
        return _poll(_service(root))
    generation = _generation or uuid.uuid4().hex
    if not _TOKEN.fullmatch(generation):
        raise BridgeError("sync_worker_identity", "error.sync_worker_identity.invalid")
    lock = _Lock(root / "worker.lock")
    if not lock.acquire():
        raise BridgeError("sync_worker_running")
    receipt = {
        "schema_version": 1,
        "generation": generation,
        "pid": os.getpid(),
        "started_at": time.time(),
        "heartbeat": time.time(),
        "state": "running",
        "last_error": None,
    }
    result = {"changed": 0, "links": [], "errors": []}
    stop_path = root / f"worker-stop-{generation}.json"
    try:
        lock.owner(generation)
        service = _service(root)
        _write(root / "worker.json", receipt)
        next_poll = time.monotonic()
        next_heartbeat = next_poll + _HEARTBEAT_SECONDS
        while _read(stop_path).get("generation") != generation:
            now = time.monotonic()
            if now >= next_poll:
                result = _poll(service)
                errors = result.get("errors", [])
                receipt["last_error"] = _error_code(errors[0].get("code")) if errors else None
                next_poll = time.monotonic() + interval
                next_heartbeat = 0
            if time.monotonic() >= next_heartbeat:
                receipt["heartbeat"] = time.time()
                _write(root / "worker.json", receipt)
                next_heartbeat = time.monotonic() + _HEARTBEAT_SECONDS
            time.sleep(min(0.25, max(0.01, next_poll - time.monotonic())))
    except KeyboardInterrupt:
        pass
    except OSError:
        raise BridgeError("sync_worker_io", "error.sync_worker_io.state") from None
    finally:
        try:
            # Still holding the lifetime lock: no successor can publish yet. Do
            # not remove a receipt based on an old generation or PID snapshot.
            current = _read(root / "worker.json")
            if current.get("generation") == generation:
                _write(root / "worker.json", {**receipt, "state": "stopped", "heartbeat": time.time()})
            _write(
                root / f"worker-ack-{generation}.json", {"generation": generation, "stopped_at": time.time()}
            )
        finally:
            lock.release()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=i18n.t("worker.description"))
    parser.add_argument("--generation", required=True)
    args = parser.parse_args()
    try:
        run(_generation=args.generation)
    except (BridgeError, OSError):
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
