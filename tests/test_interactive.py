from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from session_bridge import interactive, ui
from session_bridge.errors import BridgeError
from session_bridge.models import Conversation, Fidelity, SessionRef, Transfer

DOWN, ESC = "\x1b[B", "\x1b"


@pytest.fixture(autouse=True)
def text_mode(monkeypatch):
    # The interactive flow only runs in text mode; earlier CLI tests may leave --json set.
    from session_bridge import cli

    monkeypatch.setitem(cli._state, "json", False)


def keys(text, action):
    """Drive the real prompt_toolkit widgets with keystrokes."""
    with create_pipe_input() as source, create_app_session(input=source, output=DummyOutput()):
        source.send_text(text)
        return action()


def session(native_id, title, cwd, *, tool="claude", profile="claude-win", minutes=1, **extra):
    stamp = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
    return SessionRef(
        profile_id=profile, native_id=native_id, tool=tool, title=title, cwd=cwd, updated_at=stamp, **extra
    )


def profile(id, tool, environment="windows"):
    return SimpleNamespace(id=id, tool=tool, enabled=True, environment=environment)


CHOICES = [
    interactive.Choice(value="motor", label="Diseña un motor de búsqueda", detail="Codex · ayer"),
    interactive.Choice(value="ruta", label="Ruta inválida en sessionbridge", detail="Claude Code · ahora"),
    interactive.Choice(value="otra", label="Otra ruta", detail="OpenCode · ayer"),
]


def test_search_ignores_accents_case_and_word_order():
    assert keys("BUSQUEDA   motor disena\r", lambda: interactive.pick(CHOICES, heading=[])) == (
        "motor",
        "BUSQUEDA   motor disena",
    )


def test_arrow_keys_move_through_matches_and_escape_leaves_without_choosing():
    assert keys("ruta" + DOWN + "\r", lambda: interactive.pick(CHOICES, heading=[]))[0] == "otra"
    assert keys(ESC, lambda: interactive.pick(CHOICES, heading=[])) == (None, "")


def test_enter_without_matches_keeps_the_query_and_explains_why():
    with create_app_session(output=DummyOutput()):
        picker = interactive._Picker(
            CHOICES, heading=[], placeholder="", empty="Nada coincide con «{query}».", noun=("a", "b")
        )
        picker.buffer.text = "zzz  nada"
        text = "".join(fragment for _, fragment in picker._lines())
    assert "Nada coincide con «zzz nada»." in text
    # Enter does nothing until something matches; the query is never lost.
    assert keys("\r" + ESC, lambda: interactive.pick(CHOICES, heading=[], query="zzz")) == (None, "zzz")


def test_closer_match_comes_first_and_long_labels_are_truncated():
    with create_app_session(output=DummyOutput()):
        picker = interactive._Picker(CHOICES, heading=[], placeholder="", empty="", noun=("a", "b"))
        picker.buffer.text = "ruta"
    assert [item.value for item in picker.matches] == ["ruta", "otra"]
    assert interactive._fit("una conversación muy larga", 10) == "una conve…"
    assert interactive._fit("corta", 10) == "corta"


def test_home_finds_an_unlinked_conversation_in_another_project_while_links_exist():
    # The reported bug: the home screen used to search only synchronized conversations.
    links = [
        {
            "id": "ln_home",
            "name": "Inicio sessionbridge",
            "state": "conflict",
            "endpoints": [{"key": "codex-win:linked", "cwd": "C:/Projects/session-bridge"}],
        }
    ]
    sessions = [
        session("here", "Ruta inválida en sessionbridge", "C:/Other/place"),
        session(
            "linked",
            "Already represented by its link",
            "C:/Projects/session-bridge",
            tool="codex",
            profile="codex-win",
        ),
    ]
    choices = interactive.home_choices(sessions, links, "C:/Projects/session-bridge")
    found = interactive.ranked(choices, "ruta inválida")
    assert [item.value[1].native_id for item in found] == ["here"]
    assert found[0].group == interactive.ELSEWHERE
    assert not any("Already represented" in item.label for item in choices)


def test_home_lists_this_folder_first_links_marked_and_subagents_hidden():
    links = [
        {"id": "ln_a", "name": "Daily", "state": "synced", "endpoints": [{"key": "x:1", "cwd": "/current"}]}
    ]
    sessions = [
        session("new", "Elsewhere newest", "/other", minutes=1),
        session("old", "Here older", "/current", minutes=90),
        session("child", "", "/current", metadata={"subagent": True}, parent_id="old"),
        session(
            "spawned", "Worker", "/current", tool="codex", profile="codex-win", metadata={"spawned": True}
        ),
        session("fork", "Forked plan", "/current", tool="codex", profile="codex-win", parent_id="old"),
    ]
    choices = interactive.home_choices(sessions, links, "/current")
    assert [(item.group, item.label) for item in choices] == [
        (interactive.HERE, "Daily"),
        (interactive.HERE, "Here older"),
        (interactive.HERE, "Forked plan"),
        (interactive.ELSEWHERE, "Elsewhere newest"),
    ]
    assert choices[0].marker == "●" and "Sincronizada · Al día" in choices[0].detail
    # Other projects name their folder so two equal titles can be told apart.
    assert "other" in choices[3].detail and "Claude Code" in choices[3].detail


def test_all_projects_scope_orders_by_recency_without_folder_groups():
    sessions = [session("a", "One", "/one", minutes=1), session("b", "", "/two", minutes=5)]
    choices = interactive.home_choices(sessions, [], None)
    assert [(item.group, item.label) for item in choices] == [("", "One"), ("", "Sin título")]


def _home(monkeypatch, sessions, links=()):
    monkeypatch.setattr(
        "session_bridge.sync.SyncService", lambda **kwargs: SimpleNamespace(list=lambda: list(links))
    )
    return SimpleNamespace(list_sessions=lambda **options: sessions, profiles=lambda: [], discovery_errors=[])


def test_going_back_from_an_action_returns_to_the_search_with_the_same_query(monkeypatch):
    ref = session("a", "Ruta inválida", "/current")
    bridge = _home(monkeypatch, [ref])
    queries = []

    def pick(choices, *, query, **options):
        queries.append(query)
        return (choices[0].value, "ruta") if len(queries) == 1 else (None, query)

    monkeypatch.setattr(interactive, "pick", pick)
    monkeypatch.setattr(interactive, "_convert", lambda bridge, value, profiles: interactive.BACK)
    assert interactive.run_home(bridge, cwd="/current") is None
    assert queries == ["", "ruta"]


def test_home_stays_usable_when_the_sync_store_is_busy(monkeypatch):
    def busy(**kwargs):
        raise BridgeError("sync_store_invalid", "database is locked")

    monkeypatch.setattr("session_bridge.sync.SyncService", busy)
    bridge = SimpleNamespace(
        list_sessions=lambda **options: [session("a", "Still here", "/current")],
        profiles=lambda: [],
        discovery_errors=[],
    )
    seen = {}

    def pick(choices, *, heading, **options):
        seen["labels"] = [item.label for item in choices]
        seen["heading"] = "".join(text for _, text in heading)
        return None, ""

    monkeypatch.setattr(interactive, "pick", pick)
    assert interactive.run_home(bridge, cwd="/current") is None
    assert seen["labels"] == ["Still here"]
    assert "No se pudieron leer las conversaciones sincronizadas" in seen["heading"]


def test_home_without_conversations_explains_what_to_check(monkeypatch, capsys):
    bridge = _home(monkeypatch, [])
    monkeypatch.setattr(interactive, "pick", lambda *a, **k: pytest.fail("nothing to pick"))
    assert interactive.run_home(bridge, cwd="/current") is None
    assert "sessionbridge doctor" in capsys.readouterr().out


class ConvertBridge:
    data_dir = "unused-in-test"

    def __init__(self, ref, *, ambiguous=False, native=True, missing_cwd=False, variants=()):
        self.ref, self.ambiguous, self.native, self.missing_cwd = ref, ambiguous, native, missing_cwd
        self.variants = list(variants)
        self.previews, self.converts = [], []

    def show(self, key, snapshot=False):
        return Conversation(source=self.ref, context_ambiguous=self.ambiguous)

    def preview(self, key, target, **options):
        self.previews.append(options)
        if self.missing_cwd and options["cwd"] != "C:/destination":
            raise BridgeError("cwd_missing", "Carpeta ausente.")
        native = self.native or options["mode"] == "handoff"
        return {
            "cwd": options["cwd"] or self.ref.cwd,
            "message_count": 3,
            "native_available": native,
            "capability": {"reason": "Versión no verificada"},
            "fidelity": {},
            "variant": options.get("variant"),
            "variants": self.variants,
            "subagents": 0,
        }

    def convert(self, key, target, **options):
        self.converts.append((target, options))
        return Transfer(
            id="tr_test",
            identity="i",
            state="completed" if options["mode"] == "native" else "prepared",
            mode=options["mode"],
            source_key=key,
            target_profile=target,
            native_id="new-session" if options["mode"] == "native" else None,
            cwd=options["cwd"] or self.ref.cwd,
            created_at="2026-10-20T10:00:00Z",
            updated_at="2026-10-20T10:00:00Z",
            archive="C:/archive.zip",
            initial_message="Continúa desde aquí" if options["mode"] == "handoff" else None,
            fidelity=Fidelity(),
        )


def _answers(monkeypatch, *answers):
    asked = []
    pending = list(answers)

    def ask(message, options, **kwargs):
        asked.append((message, [label for _, label in options]))
        wanted = pending.pop(0)
        if wanted is interactive.BACK:
            return interactive.BACK
        return next(value for value, label in options if value == wanted or label == wanted)

    monkeypatch.setattr(interactive, "_ask", ask)
    return asked


PROFILES = [
    profile("claude-win", "claude"),
    profile("codex-win", "codex"),
    profile("opencode-win", "opencode"),
]


def test_conversion_asks_only_destination_and_confirmation_then_offers_to_open(monkeypatch, capsys):
    ref = session("a", "Mi tarea", "C:/project")
    bridge = ConvertBridge(ref)
    asked = _answers(monkeypatch, "codex-win", "convert", "open")
    opened = []
    monkeypatch.setattr("session_bridge.cli._resume", lambda bridge, key: opened.append(key))
    assert interactive._convert(bridge, ref, PROFILES) is None
    # The source assistant is not offered as its own destination.
    assert asked[0][1] == ["Codex", "OpenCode", "Volver a la búsqueda"]
    assert "Convertir y mantenerla sincronizada (solo su historial)" in asked[1][1]
    assert bridge.converts == [
        (
            "codex-win",
            {
                "cwd": None,
                "context": "current",
                "context_from": None,
                "snapshot": False,
                "mode": "native",
                "variant": None,
            },
        )
    ]
    assert opened == ["tr_test"]
    out = capsys.readouterr().out
    assert "Contexto actual (desde la última compactación) · 3 mensajes" in out
    assert "✓ Conversión verificada" in out


def test_conversion_can_switch_to_the_full_history_from_the_summary(monkeypatch):
    ref = session("a", "Mi tarea", "C:/project")
    bridge = ConvertBridge(ref)
    _answers(monkeypatch, "codex-win", "toggle", "convert", "done")
    interactive._convert(bridge, ref, PROFILES)
    assert [options["context"] for options in bridge.previews] == ["current", "all"]
    assert bridge.converts[0][1]["context"] == "all"


EDITED = [
    {
        "id": "v_current0001",
        "current": True,
        "prompt": "…as a list please",
        "reply": "",
        "updated_at": None,
        "messages": 2,
    },
    {
        "id": "v_earlier0002",
        "current": False,
        "prompt": "…as a table please",
        "reply": "",
        "updated_at": None,
        "messages": 2,
    },
]


def test_edited_conversation_takes_the_current_version_and_offers_the_others(monkeypatch, capsys):
    ref = session("a", "Mi tarea", "C:/project")
    bridge = ConvertBridge(ref, variants=EDITED)
    asked = _answers(monkeypatch, "codex-win", "convert", "done")
    interactive._convert(bridge, ref, PROFILES)
    assert "Elegir otra variante (2)" in asked[1][1]
    assert "Convertir y mantenerla sincronizada (solo su historial)" in asked[1][1]
    assert bridge.converts[0][1]["variant"] is None
    assert "La actual (de 2) · «…as a list please»" in capsys.readouterr().out


def test_choosing_another_version_converts_it_without_offering_sync(monkeypatch, capsys):
    ref = session("a", "Mi tarea", "C:/project")
    bridge = ConvertBridge(ref, variants=EDITED)
    asked = _answers(monkeypatch, "codex-win", "variant", "v_earlier0002", "convert", "done")
    interactive._convert(bridge, ref, PROFILES)
    labels = asked[2][1]
    assert labels[0] == "La actual · 2 mensajes · «…as a list please»"
    assert labels[1] == "Otra variante · 2 mensajes · «…as a table please»" and labels[-1] == "Volver"
    # With another version chosen, the summary says so and sync is not offered.
    assert not any("sincronizada" in label for label in asked[3][1])
    assert bridge.converts[0][1]["variant"] == "v_earlier0002"
    out = capsys.readouterr().out
    assert "Otra variante (de 2) · «…as a table please»" in out
    assert "La sincronización sigue siempre la variante actual de Claude Code." in out


def test_going_back_from_the_version_list_keeps_the_choice(monkeypatch):
    ref = session("a", "Mi tarea", "C:/project")
    bridge = ConvertBridge(ref, variants=EDITED)
    _answers(monkeypatch, "codex-win", "variant", interactive.BACK, "convert", "done")
    interactive._convert(bridge, ref, PROFILES)
    assert bridge.converts[0][1]["variant"] is None


def test_conversation_without_versions_offers_no_version_choice(monkeypatch):
    ref = session("a", "Mi tarea", "C:/project")
    bridge = ConvertBridge(ref)
    asked = _answers(monkeypatch, "codex-win", "convert", "done")
    interactive._convert(bridge, ref, PROFILES)
    assert not any("variante" in label for label in asked[1][1])


def test_missing_folder_and_unsupported_version_lead_to_an_explicit_manual_handoff(monkeypatch, capsys):
    ref = session("a", "Mi tarea", "C:/gone")
    bridge = ConvertBridge(ref, native=False, missing_cwd=True)
    asked = _answers(monkeypatch, "codex-win", "handoff", "convert")
    monkeypatch.setattr(interactive, "_ask_folder", lambda message, default="": "C:/destination")
    assert interactive._convert(bridge, ref, PROFILES) is None
    target, options = bridge.converts[0]
    assert options["cwd"] == "C:/destination" and options["mode"] == "handoff"
    # A handoff cannot be opened natively, and it cannot be kept synchronized.
    assert not any("¿Abrir ahora" in message for message, _ in asked)
    assert not any("sincronizada" in label for _, labels in asked for label in labels)
    out = capsys.readouterr().out
    assert "no existe en este equipo" in out and "Continúa desde aquí" in out


def test_empty_folder_answer_goes_back_without_converting(monkeypatch):
    ref = session("a", "Mi tarea", "C:/gone")
    bridge = ConvertBridge(ref, missing_cwd=True)
    _answers(monkeypatch, "codex-win")
    monkeypatch.setattr(interactive, "_ask_folder", lambda message, default="": None)
    assert interactive._convert(bridge, ref, PROFILES) is interactive.BACK
    assert bridge.converts == []


def test_ambiguous_context_requires_an_explicit_choice(monkeypatch):
    ref = session("a", "Mi tarea", "C:/project")
    bridge = ConvertBridge(ref, ambiguous=True)
    asked = _answers(monkeypatch, "codex-win", "all", "convert", "done")
    interactive._convert(bridge, ref, PROFILES)
    assert asked[1][0].startswith("No se puede saber con certeza")
    assert bridge.converts[0][1]["context"] == "all"
    # The full history was chosen deliberately; the summary does not offer a guess back.
    assert "Llevar solo el contexto actual" not in asked[2][1]


def test_escape_at_the_destination_goes_back_to_the_search(monkeypatch):
    ref = session("a", "Mi tarea", "C:/project")
    bridge = ConvertBridge(ref)
    _answers(monkeypatch, interactive.BACK)
    assert interactive._convert(bridge, ref, PROFILES) is interactive.BACK
    assert bridge.previews == [] and bridge.converts == []


class LinkService:
    def __init__(self, link, *, fail_cwd=False):
        self.link, self.fail_cwd, self.calls, self.reuse = link, fail_cwd, [], False

    def unpause(self, link):
        self.calls.append(("unpause", link))

    def poll(self, link, *, force):
        self.calls.append(("poll", link, force))

    def status(self, link):
        return self.link

    def open(self, link, target, *, branch, cwd, launch):
        self.calls.append(("open", link, target, branch, cwd, launch))
        if self.fail_cwd and cwd is None:
            raise BridgeError("cwd_required", "Needs a working folder.")

    def plan_open(self, link, target, *, branch, cwd):
        if self.fail_cwd and cwd is None:
            raise BridgeError("cwd_required", "Needs a working folder.")
        tool = target.split("-", 1)[0]
        head = next((h for h in self.link["heads"] if h["id"] == branch), None) or self.link["heads"][0]
        return {
            "tool": tool,
            "profile_id": target,
            "source_key": head["source_key"],
            "reuse": self.reuse,
        }

    def head_previews(self, link):
        return {head["id"]: "request " + head["id"] for head in self.link["heads"]}

    def select(self, link, branch):
        self.calls.append(("select", link, branch))


def _endpoint(key, role="current", origin="copy"):
    profile_id = key.split(":", 1)[0]
    return {
        "key": key,
        "profile_id": profile_id,
        "tool": profile_id.split("-", 1)[0],
        "role": role,
        "origin": origin,
        "enabled": True,
    }


def _link(state, heads=(), paused=False, endpoints=None):
    heads = list(heads)
    if endpoints is None:
        endpoints = [_endpoint(key) for key in dict.fromkeys(head["source_key"] for head in heads)]
    return {
        "id": "ln_x",
        "name": "Daily",
        "state": state,
        "paused": paused,
        "heads": heads,
        "selected_head": None,
        "endpoints": endpoints,
    }


def test_link_without_a_completed_turn_waits_without_choosing_a_destination(monkeypatch, capsys):
    service = LinkService(_link("waiting"))
    workers = []
    monkeypatch.setattr("session_bridge.sync_cli._ensure_worker", workers.append)
    monkeypatch.setattr(interactive, "_ask", lambda *a, **k: pytest.fail("nothing to ask"))
    bridge = SimpleNamespace(data_dir="data")
    assert interactive._open_link(bridge, service, service.link, PROFILES) is None
    assert service.calls == [("poll", "ln_x", True)] and workers == ["data"]
    assert "Aún no hay un turno terminado" in capsys.readouterr().out


def test_conflict_requires_an_explicit_version_and_offers_only_windows_assistants(monkeypatch):
    heads = [
        {"id": "rv_one", "source_key": "codex-win:a", "reason": "append", "created_at": None},
        {"id": "rv_two", "source_key": "claude-win:b", "reason": "context_replacement", "created_at": None},
    ]
    service = LinkService(_link("conflict", heads))
    monkeypatch.setattr("session_bridge.sync_cli._ensure_worker", lambda root: None)
    asked = _answers(monkeypatch, "rv_two", "codex-win")
    profiles = [profile("linux-codex", "codex", "posix"), profile("codex-win", "codex")]
    interactive._open_link(SimpleNamespace(data_dir="d"), service, service.link, profiles)
    assert asked[0][1] == ["Codex · «request rv_one»", "Claude Code · «request rv_two»", "Volver"]
    assert "Seguiste la conversación en dos sitios" in asked[0][0]
    assert asked[1][1] == ["Codex", "Volver"]
    # The choice is remembered, so the next open does not ask again.
    assert ("select", "ln_x", "rv_two") in service.calls
    assert service.calls[-1] == ("open", "ln_x", "codex-win", "rv_two", None, True)


def test_paused_link_can_go_back_before_polling(monkeypatch):
    service = LinkService(_link("paused", paused=True))
    _answers(monkeypatch, interactive.BACK)
    assert interactive._open_link(SimpleNamespace(), service, service.link, PROFILES) is interactive.BACK
    assert service.calls == []


def test_link_asks_for_a_folder_when_its_own_is_gone(monkeypatch):
    head = {"id": "rv", "source_key": "codex-win:a", "reason": "append"}
    service = LinkService(_link("synced", [head]), fail_cwd=True)
    monkeypatch.setattr("session_bridge.sync_cli._ensure_worker", lambda root: None)
    _answers(monkeypatch, "codex-win")
    monkeypatch.setattr(interactive, "_ask_folder", lambda message, default="": "C:/existing")
    assert interactive._open_link(SimpleNamespace(data_dir="d"), service, service.link, PROFILES) is None
    # The missing folder is found while planning, before anything is launched.
    assert [call for call in service.calls if call[0] == "open"] == [
        ("open", "ln_x", "codex-win", None, "C:/existing", True),
    ]
    service.calls.clear()
    _answers(monkeypatch, "codex-win")
    monkeypatch.setattr(interactive, "_ask_folder", lambda message, default="": None)
    assert (
        interactive._open_link(SimpleNamespace(data_dir="d"), service, service.link, PROFILES)
        is interactive.BACK
    )


def test_confirmation_supports_spanish_and_defaults_to_no(monkeypatch):
    answers = iter(["", "sí", "n"])
    monkeypatch.setattr(interactive, "prompt", lambda *a, **k: next(answers))
    assert not interactive.confirm("Proceed?")
    assert interactive.confirm("Proceed?")
    assert not interactive.confirm("Proceed?")


def test_relative_dates_paths_and_assistant_names_read_naturally():
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    stamps = [now - timedelta(seconds=20), now - timedelta(minutes=5), now - timedelta(days=3)]
    assert [ui.relative_time(stamp.isoformat(), now=now) for stamp in stamps] == [
        "ahora",
        "hace 5 min",
        "hace 3 días",
    ]
    assert ui.relative_time("2025-03-02T10:00:00Z", now=now) == "2 mar 2025"
    assert ui.relative_time("not a date") == ""
    assert ui.folder_name("\\\\?\\C:\\Users\\me\\Projects\\app") == "app"
    assert ui.folder_name("/home/me/work/app") == "app"
    assert ui.folder_name("C:\\") == "C:\\"
    assert ui.profile_label(profile("codex-wsl", "codex", "wsl")) == "Codex (WSL)"
    shared = [profile("a", "codex"), profile("b", "codex")]
    assert ui.profile_label(shared[0], shared) == "Codex (a)"


# ---------------------------------------------------------------------------
# Review findings


class SyncFake:
    """SyncService stand-in: records links and refuses taken names like the real store."""

    names = ["Taken name"]
    linked = []

    def __init__(self, *, bridge=None):
        self.store = SimpleNamespace(links=lambda include_unlinked=False: [{"name": n} for n in self.names])

    def link(self, key, *, name=None, transfer_id=None):
        self.linked.append((key, name, transfer_id))
        return {"id": "ln_new", "name": name or "ln_new", "state": "synced", "heads": [], "endpoints": []}


def _sync_flow(monkeypatch, title, texts):
    ref = session("a", title, "C:/project")
    bridge = ConvertBridge(ref)
    SyncFake.linked = []
    monkeypatch.setattr("session_bridge.sync.SyncService", SyncFake)
    monkeypatch.setattr("session_bridge.sync_cli._ensure_worker", lambda root: {"running": True})
    defaults = []
    pending = list(texts)

    def ask_text(message, default="", **kwargs):
        defaults.append(default)
        return pending.pop(0)

    monkeypatch.setattr(interactive, "_ask_text", ask_text)
    return ref, bridge, defaults


def test_sync_name_is_settled_before_converting_and_a_taken_name_is_asked_again(monkeypatch, capsys):
    ref, bridge, defaults = _sync_flow(monkeypatch, "Taken name", ["taken NAME", "Mi sincronizada"])
    _answers(monkeypatch, "codex-win", "sync", "done")
    interactive._convert(bridge, ref, PROFILES)
    # The taken title is not offered as the default, and the duplicate is refused before converting.
    assert defaults == ["", "taken NAME"]
    assert "Ya hay una conversación sincronizada con ese nombre" in capsys.readouterr().out
    assert len(bridge.converts) == 1
    assert SyncFake.linked == [(ref.key, "Mi sincronizada", "tr_test")]


def test_long_title_is_not_a_default_name_and_escape_returns_to_the_summary(monkeypatch):
    ref, bridge, defaults = _sync_flow(monkeypatch, "x" * 130, [None, ""])
    asked = _answers(monkeypatch, "codex-win", "sync", "sync", "done")
    interactive._convert(bridge, ref, PROFILES)
    assert defaults == ["", ""]
    # Esc at the name went back to the summary; nothing was written until the second choice.
    assert [message for message, _ in asked].count("¿Qué quieres hacer?") == 2
    assert len(bridge.converts) == 1 and SyncFake.linked == [(ref.key, None, "tr_test")]


def test_unlinked_conversations_and_detached_sessions_are_listed_as_sessions():
    links = [
        {
            "id": "ln_gone",
            "name": "Old",
            "state": "unlinked",
            "unlinked": True,
            "endpoints": [{"key": "claude-win:a"}],
        },
        {
            "id": "ln_live",
            "name": "Live",
            "state": "synced",
            "endpoints": [{"key": "claude-win:b", "enabled": False}, {"key": "claude-win:c"}],
        },
    ]
    sessions = [
        session("a", "Was linked", "/x"),
        session("b", "Detached", "/x"),
        session("c", "Followed", "/x"),
    ]
    labels = [item.label for item in interactive.home_choices(sessions, links, None)]
    assert labels == ["Live", "Was linked", "Detached"]


def test_single_pending_version_says_what_opening_it_means(monkeypatch):
    head = {"id": "rv_one", "source_key": "codex-win:a", "reason": "append"}
    service = LinkService(_link("waiting", [head]))
    monkeypatch.setattr("session_bridge.sync_cli._ensure_worker", lambda root: None)
    asked = _answers(monkeypatch, "rv_one", "codex-win")
    interactive._open_link(SimpleNamespace(data_dir="d"), service, service.link, PROFILES)
    assert asked[0][0].startswith("El origen tiene cambios aún sin comprobar")
    assert asked[0][1][0] == "Abrir el último turno terminado · Desde Codex · continuación"


def test_busy_worker_while_checking_a_link_returns_to_the_search(monkeypatch, capsys):
    service = LinkService(_link("synced"))

    def busy(link, *, force):
        raise BridgeError("sync_busy", "busy")

    service.poll = busy
    assert interactive._open_link(SimpleNamespace(), service, service.link, PROFILES) is interactive.BACK
    assert "vuelve a intentarlo" in capsys.readouterr().out


def test_escape_in_a_text_prompt_goes_back_and_enter_keeps_the_default():
    assert keys(ESC, lambda: interactive._ask_text("Nombre: ", "Mi tarea")) is None
    assert keys("\r", lambda: interactive._ask_text("Nombre: ", "Mi tarea")) == "Mi tarea"


def test_no_color_reaches_every_prompt_but_not_launched_assistants(monkeypatch):
    import io
    import os

    from prompt_toolkit.application import get_app_session
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.output import ColorDepth
    from prompt_toolkit.output.vt100 import Vt100_Output

    from session_bridge import cli

    def terminal_output():
        # Like prompt_toolkit's own create_output: the depth is fixed from the environment now.
        return Vt100_Output(
            io.StringIO(), lambda: Size(24, 80), term="xterm", default_color_depth=ColorDepth.from_env()
        )

    monkeypatch.setattr(interactive, "create_output", terminal_output)
    monkeypatch.delenv("PROMPT_TOOLKIT_COLOR_DEPTH", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    seen = {}

    def pick(choices, **options):
        seen["depth"] = get_app_session().output.get_default_color_depth()
        seen["environment"] = os.environ.get("PROMPT_TOOLKIT_COLOR_DEPTH")
        return None, ""

    monkeypatch.setattr(interactive, "pick", pick)
    bridge = _home(monkeypatch, [session("a", "One", "/x")])
    monkeypatch.setitem(cli._state, "no_color", True)
    interactive.run_home(bridge, cwd="/x")
    # Every prompt in the flow draws without colour, and nothing a child process inherits changed.
    assert seen == {"depth": ColorDepth.DEPTH_1_BIT, "environment": None}
    assert "PROMPT_TOOLKIT_COLOR_DEPTH" not in os.environ


def test_conflict_names_an_earlier_copy_and_the_original_session(monkeypatch):
    heads = [
        {"id": "rv_old", "source_key": "codex-win:old", "reason": "append", "created_at": None},
        {"id": "rv_tab", "source_key": "claude-win:tab", "reason": "append", "created_at": None},
        {"id": "rv_new", "source_key": "codex-win:new", "reason": "append", "created_at": None},
    ]
    endpoints = [
        _endpoint("claude-win:tab", "superseded", "original"),
        _endpoint("codex-win:old", "superseded"),
        _endpoint("claude-win:copy"),
        _endpoint("codex-win:new"),
    ]
    service = LinkService(_link("conflict", heads, endpoints=endpoints))
    monkeypatch.setattr("session_bridge.sync_cli._ensure_worker", lambda root: None)
    asked = _answers(monkeypatch, interactive.BACK)
    assert interactive._open_link(SimpleNamespace(data_dir="d"), service, service.link, PROFILES) is (
        interactive.BACK
    )
    assert "en varios sitios" in asked[0][0] and "Las demás se conservan" in asked[0][0]
    assert asked[0][1][:3] == [
        "Una copia anterior de Codex · «request rv_old»",
        "La conversación original de Claude Code · «request rv_tab»",
        "Codex · «request rv_new»",
    ]
    assert not any(call[0] == "select" for call in service.calls)


def test_opening_says_whether_the_session_is_reused_or_new(monkeypatch, capsys):
    head = {"id": "rv", "source_key": "claude-win:b", "reason": "append"}
    endpoints = [_endpoint("claude-win:b"), _endpoint("codex-win:a", origin="original")]
    service = LinkService(_link("synced", [head], endpoints=endpoints))
    monkeypatch.setattr("session_bridge.sync_cli._ensure_worker", lambda root: None)
    _answers(monkeypatch, "codex-win")
    interactive._open_link(SimpleNamespace(data_dir="d"), service, service.link, PROFILES)
    out = " ".join(capsys.readouterr().out.split())
    assert "Se abrirá una sesión nueva de Codex con lo último de Claude Code." in out
    assert "La anterior queda como estaba" in out
    service.reuse = True
    _answers(monkeypatch, "codex-win")
    interactive._open_link(SimpleNamespace(data_dir="d"), service, service.link, PROFILES)
    assert "Se abrirá tu sesión de Codex, ya al día." in " ".join(capsys.readouterr().out.split())


def test_first_opening_in_an_assistant_does_not_mention_an_earlier_session(monkeypatch, capsys):
    head = {"id": "rv", "source_key": "claude-win:b", "reason": "append"}
    service = LinkService(_link("synced", [head]))
    monkeypatch.setattr("session_bridge.sync_cli._ensure_worker", lambda root: None)
    _answers(monkeypatch, "codex-win")
    interactive._open_link(SimpleNamespace(data_dir="d"), service, service.link, PROFILES)
    out = " ".join(capsys.readouterr().out.split())
    assert "sesión nueva de Codex" in out and "La anterior" not in out


def test_home_row_says_where_the_conversation_was_last_continued():
    now = datetime.now(UTC)
    head = {"id": "rv", "source_key": "claude-win:b", "created_at": (now - timedelta(minutes=3)).isoformat()}
    synced = _link("synced", [head])
    synced["selected_head"] = "rv"
    conflict = _link("conflict", [head, {**head, "id": "rv2", "source_key": "codex-win:a"}])
    conflict["id"], conflict["name"] = "ln_y", "Other"
    rows = interactive.home_choices([], [synced, conflict], None)
    assert rows[0].detail == "Sincronizada · última vez en Claude Code · hace 3 min"
    assert rows[1].detail == "Sincronizada · Dos continuaciones"


def test_menus_fit_long_labels_and_wrap_the_question_to_the_terminal(monkeypatch):
    seen = {}

    def fake_choice(*, message, options, **kwargs):
        seen["message"], seen["labels"] = message, [label for _, label in options]
        return options[0][0]

    monkeypatch.setattr(interactive, "choice", fake_choice)
    monkeypatch.setattr(interactive, "_columns", lambda: 40)
    interactive._ask(
        "Seguiste la conversación en dos sitios. ¿Con cuál continúas?", [("a", "x" * 80), ("b", "short")]
    )
    assert all(len(line) <= 39 for line in seen["message"].split("\n"))
    assert "continúas?" in seen["message"].split("\n")[-1]
    assert seen["labels"][0].endswith("…") and len(seen["labels"][0]) == 39 - interactive.MENU_INDENT
    assert seen["labels"][1] == "short"
