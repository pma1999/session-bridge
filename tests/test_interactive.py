from types import SimpleNamespace

from session_bridge import interactive
from session_bridge.errors import BridgeError
from session_bridge.models import Conversation, SessionRef


def test_wizard_searches_metadata_requests_destination_and_explicit_fallback(monkeypatch):
    ref = SessionRef(
        profile_id="source", native_id="abc", tool="claude", title="My feature", cwd="/old/project"
    )
    choices = iter(["source", "target", "current", "native"])

    def choose(label, values):
        wanted = next(choices)
        if wanted == "source":
            assert "My feature" in values[0] and "/old/project" in values[0]
            return values[0]
        return values[0] if wanted == "target" else wanted

    monkeypatch.setattr(interactive, "_choose", choose)
    monkeypatch.setattr(interactive, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(interactive, "prompt", lambda *a, **k: "C:/destination", raising=False)
    observed = []

    def preview(key, target, **options):
        if options.get("cwd") != "C:/destination":
            raise BridgeError("cwd_missing", "Carpeta ausente.")
        return {"native_available": options["mode"] == "handoff", "capability": {"reason": "Unverified"}}

    def convert(key, target, **options):
        observed.append(options)
        return {"state": "prepared"}

    bridge = SimpleNamespace(
        list_sessions=lambda: [ref],
        show=lambda *a, **k: Conversation(source=ref),
        profiles=lambda: [SimpleNamespace(id="target", tool="codex", enabled=True)],
        preview=preview,
        convert=convert,
    )
    assert interactive.run_wizard(bridge) == {"state": "prepared"}
    assert observed[0]["cwd"] == "C:/destination" and observed[0]["mode"] == "handoff"


def test_confirmation_supports_spanish_and_defaults_to_no(monkeypatch):
    answers = iter(["", "sí", "n"])
    monkeypatch.setattr(interactive, "prompt", lambda *a, **k: next(answers))
    assert not interactive.confirm("Proceed?")
    assert interactive.confirm("Proceed?")
    assert not interactive.confirm("Proceed?")


def test_run_home_waits_without_opening_when_link_has_no_completed_head(monkeypatch):
    linked = {"id": "ln_waiting", "name": "Daily", "state": "waiting", "paused": False, "heads": []}
    calls = []

    class Service:
        def __init__(self, *, bridge):
            pass

        def list(self):
            return [linked]

        def poll(self, link, *, force):
            calls.append(("poll", link, force))

        def status(self, link):
            return linked

    class Bridge:
        data_dir = "unused-in-test"

        def profiles(self):
            raise AssertionError("a waiting link must not select a destination")

    def choose(label, values):
        assert label == "Conversación o nueva conversión"
        return next(value for value in values if "ln_waiting" in value)

    monkeypatch.setattr("session_bridge.sync.SyncService", Service)
    monkeypatch.setattr("session_bridge.sync_cli._ensure_worker", lambda root: calls.append(("worker", root)))
    monkeypatch.setattr(interactive, "_choose", choose)

    result = interactive.run_home(Bridge())

    assert result is linked
    assert calls == [("poll", "ln_waiting", True), ("worker", "unused-in-test")]


def test_run_home_conflict_requires_explicit_branch_and_filters_non_windows_targets(monkeypatch):
    linked = {
        "id": "ln_conflict", "name": "Daily", "state": "conflict", "paused": False,
        "heads": [
            {"id": "rv_one", "source_key": "codex-win:a", "reason": "append"},
            {"id": "rv_two", "source_key": "claude-win:b", "reason": "context_replacement"},
        ],
    }
    calls = []

    class Service:
        def __init__(self, *, bridge):
            pass

        def list(self):
            return [linked]

        def poll(self, link, *, force):
            calls.append(("poll", link, force))

        def status(self, link):
            return linked

        def open(self, link, target, *, branch, cwd, launch):
            calls.append(("open", link, target, branch, cwd, launch))
            return {"link_id": link, "profile": target, "revision_id": branch}

    class Bridge:
        data_dir = "unused-in-test"

        def profiles(self):
            return [
                SimpleNamespace(id="linux-codex", tool="codex", enabled=True, environment="posix"),
                SimpleNamespace(id="windows-codex", tool="codex", enabled=True, environment="windows"),
                SimpleNamespace(id="windows-off", tool="claude", enabled=False, environment="windows"),
            ]

    choices = []

    def choose(label, values):
        choices.append((label, values))
        if label == "Conversación o nueva conversión":
            return next(value for value in values if "ln_conflict" in value)
        if label == "Rama verificada":
            return next(value for value in values if value.startswith("rv_two"))
        if label == "Abrir con":
            return values[0]
        raise AssertionError(label)

    monkeypatch.setattr("session_bridge.sync.SyncService", Service)
    monkeypatch.setattr("session_bridge.sync_cli._ensure_worker", lambda root: calls.append(("worker", root)))
    monkeypatch.setattr(interactive, "_choose", choose)

    result = interactive.run_home(Bridge())

    assert result["revision_id"] == "rv_two"
    target_values = next(values for label, values in choices if label == "Abrir con")
    assert target_values == ["windows-codex (codex)"]
    assert calls == [
        ("poll", "ln_conflict", True), ("worker", "unused-in-test"),
        ("open", "ln_conflict", "windows-codex", "rv_two", None, True),
    ]


def test_run_home_paused_link_can_cancel_before_poll_or_worker(monkeypatch):
    linked = {"id": "ln_paused", "name": "Daily", "state": "paused", "paused": True, "heads": []}
    calls = []

    class Service:
        def __init__(self, *, bridge):
            pass

        def list(self):
            return [linked]

        def unpause(self, link):
            calls.append(("unpause", link))

    class Bridge:
        def profiles(self):
            raise AssertionError("cancel must not inspect profiles")

    monkeypatch.setattr("session_bridge.sync.SyncService", Service)
    monkeypatch.setattr("session_bridge.sync_cli._ensure_worker", lambda root: calls.append(("worker", root)))
    monkeypatch.setattr(interactive, "_choose", lambda label, values: next(v for v in values if "ln_paused" in v))
    monkeypatch.setattr(interactive, "confirm", lambda *args, **kwargs: False)

    assert interactive.run_home(Bridge()) == {"cancelled": True}
    assert calls == []


def test_run_home_retries_missing_cwd_and_can_cancel_empty_retry(monkeypatch):
    linked = {
        "id": "ln_ready", "name": "Daily", "state": "synced", "paused": False,
        "heads": [{"id": "rv_head", "source_key": "codex-win:a", "reason": "append"}],
    }

    class Service:
        def __init__(self, *, bridge):
            self.opens = []

        def list(self):
            return [linked]

        def poll(self, link, *, force):
            pass

        def status(self, link):
            return linked

        def open(self, link, target, *, branch, cwd, launch):
            self.opens.append((link, target, branch, cwd, launch))
            if cwd is None:
                raise BridgeError("cwd_required", "Needs a valid working folder.")
            return {"cwd": cwd, "launched": launch}

    service = Service(bridge=None)
    monkeypatch.setattr("session_bridge.sync.SyncService", lambda *, bridge: service)
    monkeypatch.setattr("session_bridge.sync_cli._ensure_worker", lambda root: None)
    monkeypatch.setattr(interactive, "_choose", lambda label, values: next(
        (value for value in values if "ln_ready" in value), values[0]
    ) if label == "Conversación o nueva conversión" else values[0])
    monkeypatch.setattr(interactive, "prompt", lambda _: "C:/existing")

    class Bridge:
        data_dir = "unused-in-test"

        def profiles(self):
            return [SimpleNamespace(id="windows-codex", tool="codex", enabled=True, environment="windows")]

    assert interactive.run_home(Bridge()) == {"cwd": "C:/existing", "launched": True}
    assert service.opens == [
        ("ln_ready", "windows-codex", None, None, True),
        ("ln_ready", "windows-codex", None, "C:/existing", True),
    ]

    service.opens.clear()
    monkeypatch.setattr(interactive, "prompt", lambda _: "")
    assert interactive.run_home(Bridge()) == {"cancelled": True}
    assert service.opens == [("ln_ready", "windows-codex", None, None, True)]
