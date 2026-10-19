import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from session_bridge import sync_daemon as daemon
from session_bridge.errors import BridgeError


def test_worker_refuses_an_aliased_lock_path(monkeypatch, tmp_path):
    from pathlib import Path

    lockpath = tmp_path / "worker.lock"
    lockpath.write_bytes(b"unrelated-content")
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == lockpath or original(path))
    with pytest.raises(BridgeError) as error:
        daemon._Lock(lockpath).acquire()
    assert error.value.code == "sync_worker_io"
    assert lockpath.read_bytes() == b"unrelated-content"


def test_once_polls_exactly_once_without_starting_native_targets(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        daemon,
        "_service",
        lambda root: SimpleNamespace(
            poll=lambda: calls.append(root) or {"changed": 1, "links": [], "errors": []}
        ),
    )
    assert daemon.run(tmp_path, once=True)["changed"] == 1
    assert calls == [tmp_path]


def test_stale_running_receipt_is_not_treated_as_live_worker(tmp_path):
    (tmp_path / "worker.json").write_text(
        json.dumps({"state": "running", "generation": "a" * 32, "heartbeat": time.time()}),
        encoding="utf-8",
    )

    assert daemon.status(tmp_path) == {"running": False, "state": "stale"}


def test_start_is_idempotent_for_healthy_lock_generation(monkeypatch, tmp_path):
    generation = "a" * 32
    lock = daemon._Lock(tmp_path / "worker.lock")
    assert lock.acquire()
    try:
        lock.owner(generation)
        daemon._write(
            tmp_path / "worker.json",
            {
                "generation": generation,
                "pid": 123,
                "started_at": time.time(),
                "heartbeat": time.time(),
                "state": "running",
                "last_error": None,
            },
        )
        monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **k: pytest.fail("duplicate worker"))

        result = daemon.start(tmp_path)

        assert result["running"] is True
        assert result["generation"] == generation
        assert result["started"] is False
    finally:
        lock.release()


def test_start_confirms_new_generation_lock_and_uses_fixed_detached_launch(monkeypatch, tmp_path):
    held = {}
    launch = {}
    monkeypatch.setenv("CODEX_HOME", "C:/isolated/native-home")
    monkeypatch.setenv("PYTHONPATH", "C:/source-tree/src")

    def popen(argv, **kwargs):
        launch.update(argv=argv, **kwargs)
        generation = argv[-1]
        lock = daemon._Lock(tmp_path / "worker.lock")
        assert lock.acquire()
        lock.owner(generation)
        daemon._write(
            tmp_path / "worker.json",
            {
                "generation": generation,
                "pid": 321,
                "started_at": time.time(),
                "heartbeat": time.time(),
                "state": "running",
                "last_error": None,
            },
        )
        held["lock"] = lock
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(daemon.subprocess, "Popen", popen)
    try:
        result = daemon.start(tmp_path)

        assert result["started"] is True
        assert result["generation"] == launch["argv"][-1]
        assert launch["argv"] == [
            daemon.sys.executable,
            "-m",
            "session_bridge.sync_daemon",
            "--generation",
            result["generation"],
        ]
        assert launch["stdin"] is daemon.subprocess.DEVNULL
        assert launch["stdout"] is daemon.subprocess.DEVNULL
        assert launch["stderr"] is daemon.subprocess.DEVNULL
        assert launch["env"]["SESSIONBRIDGE_HOME"] == str(tmp_path.resolve())
        assert launch["env"]["CODEX_HOME"] == "C:/isolated/native-home"
        assert launch["env"]["PYTHONPATH"] == "C:/source-tree/src"
        assert launch["creationflags"] == getattr(daemon.subprocess, "CREATE_NO_WINDOW", 0)
        assert launch["cwd"] == str(Path(daemon.__file__).resolve().parent.parent)
        assert "shell" not in launch
    finally:
        if "lock" in held:
            held["lock"].release()


def test_start_does_not_accept_popen_success_without_generation_ownership(monkeypatch, tmp_path):
    monkeypatch.setattr(
        daemon.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(poll=lambda: None)
    )
    monkeypatch.setattr(daemon, "_START_TIMEOUT", 0.02)
    monkeypatch.setattr(daemon.time, "sleep", lambda _: None)

    with pytest.raises(BridgeError) as error:
        daemon.start(tmp_path)

    assert error.value.code == "sync_worker_start_failed"
    stops = list(tmp_path.glob("worker-stop-*.json"))
    assert len(stops) == 1
    assert json.loads(stops[0].read_text(encoding="utf-8"))["generation"] == stops[0].stem.removeprefix(
        "worker-stop-"
    )
    assert daemon.status(tmp_path)["running"] is False


def test_stop_waits_for_the_current_generation_ack_and_ignores_wrong_ack(monkeypatch, tmp_path):
    generation = "b" * 32
    poll_started = threading.Event()
    worker_done = threading.Event()
    original_read = daemon._read
    wrong_ack_injected = False

    def read(path):
        nonlocal wrong_ack_injected
        if path == tmp_path / f"worker-ack-{generation}.json" and not wrong_ack_injected:
            wrong_ack_injected = True
            return {"generation": "c" * 32}
        return original_read(path)

    class Service:
        def poll(self):
            poll_started.set()
            return {"changed": 0, "links": [], "errors": []}

    monkeypatch.setattr(daemon, "_service", lambda root: Service())
    monkeypatch.setattr(daemon, "_read", read)
    monkeypatch.setattr(daemon, "_STOP_TIMEOUT", 2.0)

    def worker():
        try:
            daemon.run(tmp_path, interval=1.0, _generation=generation)
        finally:
            worker_done.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        assert poll_started.wait(2), "worker never completed its initial reconciliation"
        result = daemon.stop(tmp_path)
    finally:
        if not worker_done.is_set():
            daemon._write(tmp_path / f"worker-stop-{generation}.json", {"generation": generation})
        thread.join(2)

    assert worker_done.is_set()
    assert result["acknowledged"] is True
    assert result["stopped_generation"] == generation
    assert (tmp_path / f"worker-stop-{generation}.json").exists()
    assert original_read(tmp_path / f"worker-ack-{generation}.json")["generation"] == generation


def test_worker_loop_sleeps_between_reconciliations(monkeypatch, tmp_path):
    generation = "d" * 32
    clock = [100.0]
    sleeps = []
    stop_checks = [0]
    original_read = daemon._read

    class Service:
        def poll(self):
            return {"changed": 0, "links": [], "errors": []}

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    def fake_read(path):
        if path == tmp_path / f"worker-stop-{generation}.json":
            stop_checks[0] += 1
            if stop_checks[0] >= 4:
                return {"generation": generation}
            return {}
        return original_read(path)

    monkeypatch.setattr(daemon, "_service", lambda root: Service())
    monkeypatch.setattr(daemon.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(daemon.time, "sleep", fake_sleep)
    monkeypatch.setattr(daemon, "_read", fake_read)

    daemon.run(tmp_path, interval=1.0, _generation=generation)

    assert len(sleeps) == 3
    assert all(0.01 <= seconds <= 0.25 for seconds in sleeps)
    assert clock[0] - 100.0 >= 0.03


@pytest.mark.parametrize("interval", [0, -1, 0.01, 3601, float("nan"), float("inf")])
def test_invalid_interval_never_enters_poll_loop(tmp_path, interval):
    with pytest.raises(BridgeError, match="intervalo"):
        daemon.run(tmp_path, once=True, interval=interval)


@pytest.mark.parametrize("interval", [0.1, 3600])
def test_interval_bounds_are_accepted_for_single_reconciliation(monkeypatch, tmp_path, interval):
    calls = []
    monkeypatch.setattr(
        daemon,
        "_service",
        lambda root: SimpleNamespace(
            poll=lambda: calls.append(root) or {"changed": 0, "links": [], "errors": []}
        ),
    )

    daemon.run(tmp_path, once=True, interval=interval)

    assert calls == [tmp_path]


def test_poll_error_keeps_exception_text_out_of_worker_result():
    secret_text = "private conversation body"

    result = daemon._poll(
        SimpleNamespace(poll=lambda: (_ for _ in ()).throw(BridgeError("sync_source_error", secret_text)))
    )

    assert result["errors"] == [
        {"code": "sync_source_error", "message": "No se pudo comprobar la sincronización."}
    ]
    assert secret_text not in repr(result)


def test_stop_rechecks_ack_published_between_ack_read_and_lock_release(monkeypatch, tmp_path):
    generation = "e" * 32
    reads = []
    monkeypatch.setattr(daemon, "status", lambda root: {"running": True, "generation": generation})
    monkeypatch.setattr(daemon, "_control", lambda root: SimpleNamespace(release=lambda: None))
    monkeypatch.setattr(daemon, "_write", lambda path, data: None)

    def read(path):
        reads.append(path)
        return {} if len(reads) == 1 else {"generation": generation}

    monkeypatch.setattr(daemon, "_read", read)
    monkeypatch.setattr(daemon, "_owner", lambda root: (False, None))
    result = daemon.stop(tmp_path)
    assert result["acknowledged"] is True
    assert result["stopped_generation"] == generation
    assert len(reads) == 2
