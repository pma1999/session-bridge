"""Interactive terminal flow: one search over every conversation, then a short guided action."""

from __future__ import annotations

import contextlib
import os
import textwrap
from dataclasses import dataclass, field
from typing import Any

from prompt_toolkit import prompt
from prompt_toolkit.application import Application, create_app_session, get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import PathCompleter
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import AfterInput
from prompt_toolkit.output import create_output
from prompt_toolkit.shortcuts import choice
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from . import ui
from .catalog import is_subagent, same_directory, search_rank, search_text
from .errors import BridgeError

# Colour only marks; meaning is always carried by words and symbols, so every theme stays legible.
STYLE = Style.from_dict(
    {
        "title": "bold",
        "accent": "ansicyan bold",
        "muted": "ansibrightblack",
        "group": "bold",
        "selected": "reverse",
        "selected-option": "bold",
        "number": "ansibrightblack",
    }
)

BACK = object()
NARROW = 72  # below this width, rows show their brief detail
HERE, ELSEWHERE = "En esta carpeta", "En otros proyectos"


def _out():
    from . import cli

    return cli._console_out


def _status(message: str):
    console = _out()
    return console.status(message) if console.is_terminal else contextlib.nullcontext()


def _note(text: str, style: str = "") -> None:
    _out().print(text, style=style or None)


# ---------------------------------------------------------------------------
# Live search list


@dataclass
class Choice:
    value: Any
    label: str
    detail: str = ""
    group: str = ""
    marker: str = ""
    search: str = ""
    brief: str = ""
    key: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self.search = self.search or f"{self.label} {self.detail}"
        self.key = search_text(self.search)


def ranked(choices: list[Choice], query: str) -> list[Choice]:
    """Matches keep their group order; within a group, a closer match comes first, then the given order."""
    if not query.strip():
        return list(choices)
    groups: dict[str, int] = {}
    hits = []
    for index, item in enumerate(choices):
        order = groups.setdefault(item.group, len(groups))
        rank = search_rank(query, item.search, normalized_value=item.key)
        if rank is not None:
            hits.append((order, rank, index, item))
    hits.sort(key=lambda hit: hit[:3])
    return [hit[3] for hit in hits]


def _fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if get_cwidth(text) <= width:
        return text
    kept, used = [], 0
    for character in text:
        size = get_cwidth(character)
        if used + size > width - 1:
            break
        kept.append(character)
        used += size
    return "".join(kept) + "…"


class _Picker:
    def __init__(
        self,
        choices: list[Choice],
        *,
        heading: list[tuple[str, str]],
        placeholder: str,
        empty: str,
        noun: tuple[str, str],
        query: str = "",
    ):
        self.choices = choices
        self.heading = heading
        self.empty = empty
        self.noun = noun
        self.matches = ranked(choices, query)
        self.selected = 0
        self.offset = 0
        self.buffer = Buffer(multiline=False, on_text_changed=self._refilter)
        self.buffer.text = query
        self.buffer.cursor_position = len(query)
        bindings = KeyBindings()

        @bindings.add("up")
        @bindings.add("c-p")
        @bindings.add("s-tab")
        def _(event):
            self._move(-1)

        @bindings.add("down")
        @bindings.add("c-n")
        @bindings.add("tab")
        def _(event):
            self._move(1)

        @bindings.add("pageup")
        def _(event):
            self._move(-self._capacity())

        @bindings.add("pagedown")
        def _(event):
            self._move(self._capacity())

        @bindings.add("enter")
        def _(event):
            if self.matches:
                event.app.exit(result=self.matches[self.selected].value)

        @bindings.add("escape", eager=True)
        def _(event):
            event.app.exit(result=None)

        @bindings.add("c-c")
        def _(event):
            event.app.exit(exception=KeyboardInterrupt())

        search = BufferControl(
            buffer=self.buffer,
            input_processors=[
                AfterInput(lambda: "" if self.buffer.text else placeholder, style="class:muted")
            ],
        )
        self.app: Application = Application(
            layout=Layout(
                HSplit(
                    [
                        Window(FormattedTextControl(self._heading), dont_extend_height=True),
                        VSplit(
                            [
                                Window(FormattedTextControl([("class:accent", "› ")]), width=2),
                                Window(search, height=1),
                            ]
                        ),
                        Window(FormattedTextControl(self._lines), height=self._height),
                        Window(FormattedTextControl(self._status), height=1),
                    ]
                ),
                focused_element=search,
            ),
            key_bindings=bindings,
            style=STYLE,
            full_screen=False,
            erase_when_done=True,
        )

    def run(self) -> tuple[Any, str]:
        return self.app.run(), self.buffer.text

    def _refilter(self, _buffer) -> None:
        self.matches = ranked(self.choices, self.buffer.text)
        self.selected = self.offset = 0

    def _move(self, step: int) -> None:
        if self.matches:
            self.selected = max(0, min(len(self.matches) - 1, self.selected + step))

    def _size(self):
        try:
            return get_app().output.get_size()
        except Exception:  # noqa: BLE001 - layout must render even without a measurable terminal
            from prompt_toolkit.data_structures import Size

            return Size(rows=24, columns=80)

    def _capacity(self) -> int:
        reserved = sum(text.count("\n") for _, text in self.heading) + 1 + 3
        return max(3, min(18, self._size().rows - reserved))

    def _rows(self, start: int, end: int) -> int:
        rows = 0
        for index in range(start, end + 1):
            group = self.matches[index].group
            if group and (index == start or self.matches[index - 1].group != group):
                rows += 1
            rows += 1
        return rows

    def _visible(self) -> list[tuple[str, Any]]:
        capacity = self._capacity()
        if self.selected < self.offset:
            self.offset = self.selected
        while self.offset < self.selected and self._rows(self.offset, self.selected) > capacity:
            self.offset += 1
        rows: list[tuple[str, Any]] = []
        for index in range(self.offset, len(self.matches)):
            item = self.matches[index]
            header = bool(item.group) and (
                index == self.offset or self.matches[index - 1].group != item.group
            )
            if len(rows) + header + 1 > capacity:
                break
            if header:
                rows.append(("group", item.group))
            rows.append(("item", index))
        return rows

    def _height(self) -> Dimension:
        return Dimension.exact(len(self._visible()) if self.matches else len(self._empty_lines()))

    def _empty_lines(self) -> list[str]:
        query = " ".join(self.buffer.text.split())
        message = self.empty.format(query=ui.visible(query))
        return textwrap.wrap(message, max(10, self._size().columns - 3)) or [""]

    def _lines(self):
        if not self.matches:
            return [("class:muted", "\n".join("  " + line for line in self._empty_lines()))]
        width = max(20, self._size().columns - 1)
        rows = self._visible()
        room = width - 4
        narrow = width < NARROW

        def detail_of(item: Choice) -> str:
            return item.brief if narrow and item.brief else item.detail

        # One detail column for every visible row keeps the titles aligned.
        details = [get_cwidth(detail_of(self.matches[value])) for kind, value in rows if kind == "item"]
        detail_width = min(max(details, default=0), min(room // 2, max(16, room * 2 // 5)))
        label_width = room - detail_width - (2 if detail_width else 0)
        fragments: list[tuple[str, str]] = []
        for number, (kind, value) in enumerate(rows):
            if number:
                fragments.append(("", "\n"))
            if kind == "group":
                fragments.append(("class:group", "  " + _fit(value, width - 2)))
                continue
            item = self.matches[value]
            chosen = value == self.selected
            base = "class:selected " if chosen else ""
            label = _fit(item.label, label_width)
            detail = _fit(detail_of(item), detail_width)
            gap = " " * (label_width - get_cwidth(label) + (2 if detail_width else 0))
            fragments += [
                (base + "class:accent", ("›" if chosen else " ") + (item.marker or " ")),
                (base, " " + label + gap),
                (base + "class:muted", detail + " " * (detail_width - get_cwidth(detail)) + " "),
            ]
        return fragments

    def _status(self):
        total, shown = len(self.choices), len(self.matches)
        count = (
            f"{shown} de {total}"
            if self.buffer.text.strip()
            else f"{total} {self.noun[0] if total == 1 else self.noun[1]}"
        )
        width = self._size().columns - 1
        # Drop the hints a reader needs least when the terminal is narrow.
        for hints in (
            " · ↑↓ elegir · Intro abrir · Esc salir",
            " · Intro abrir · Esc salir",
            " · Esc salir",
            "",
        ):
            text = f"  {count}{hints}"
            if get_cwidth(text) <= width:
                break
        return [("class:muted", _fit(text, width))]

    def _heading(self):
        width = self._size().columns - 1
        lines, current = [], []
        for style, text in self.heading:
            for number, part in enumerate(text.split("\n")):
                if number:
                    lines.append(current)
                    current = []
                current.append((style, part))
        lines.append(current)
        fragments: list[tuple[str, str]] = []
        for number, line in enumerate(lines):
            if number:
                fragments.append(("", "\n"))
            room = width
            for style, part in line:
                part = _fit(part, room)
                room -= get_cwidth(part)
                fragments.append((style, part))
        return fragments


def pick(
    choices: list[Choice],
    *,
    heading: list[tuple[str, str]],
    placeholder: str = "Escribe para buscar",
    empty: str = "Nada coincide con «{query}».",
    noun: tuple[str, str] = ("opción", "opciones"),
    query: str = "",
) -> tuple[Any, str]:
    """Search-as-you-type list: returns the chosen value (None on Esc) and the final query."""
    return _Picker(
        choices, heading=heading, placeholder=placeholder, empty=empty, noun=noun, query=query
    ).run()


@contextlib.contextmanager
def _colour_session():
    """Honour --no-color in every prompt of this flow without changing what launched assistants inherit.

    Each prompt_toolkit output fixes its colour depth from the environment when it is created,
    so the variable is set only while this flow's own output is built.
    """
    from . import cli

    if not cli._state.get("no_color"):
        yield
        return
    previous = os.environ.get("PROMPT_TOOLKIT_COLOR_DEPTH")
    os.environ["PROMPT_TOOLKIT_COLOR_DEPTH"] = "DEPTH_1_BIT"
    try:
        output = create_output()
    finally:
        if previous is None:
            os.environ.pop("PROMPT_TOOLKIT_COLOR_DEPTH", None)
        else:
            os.environ["PROMPT_TOOLKIT_COLOR_DEPTH"] = previous
    with create_app_session(output=output):
        yield


def _back_on_escape(result: Any) -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("escape", eager=True)
    def _(event):
        event.app.exit(result=result)

    return bindings


def _ask(message: str, options: list[tuple[Any, str]], *, default: Any = None) -> Any:
    """Arrow-key menu; Esc goes back one step."""
    return choice(
        message=message,
        options=options,
        default=default,
        style=STYLE,
        key_bindings=_back_on_escape(BACK),
        symbol="›",
    )


def _ask_text(message: str, default: str = "", *, completer=None) -> str | None:
    """A line of text; Esc goes back (None) and keeps nothing half-typed."""
    answer = prompt(
        message,
        default=default,
        completer=completer,
        complete_while_typing=completer is not None,
        key_bindings=_back_on_escape(None),
        style=STYLE,
    )
    return None if answer is None else answer.strip()


def _ask_folder(message: str, default: str = "") -> str | None:
    answer = _ask_text(message, default, completer=PathCompleter(only_directories=True, expanduser=True))
    return answer or None


def confirm(message: str, default: bool = False) -> bool:
    suffix = " [S/n]: " if default else " [s/N]: "
    while True:
        answer = prompt(message + suffix).strip().casefold()
        if not answer:
            return default
        if answer in {"s", "si", "sí", "y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Responde s o n; Intro conserva la opción indicada.")


# ---------------------------------------------------------------------------
# Home: every conversation in one search


def _session_detail(ref, *, folder: bool) -> str:
    parts = [ui.tool_name(ref.tool), ui.relative_time(ref.updated_at or ref.created_at)]
    if ref.archived:
        parts.append("archivada")
    if folder and ref.cwd:
        parts.append(ui.folder_name(ref.cwd))
    return " · ".join(part for part in parts if part)


def _link_cwds(link) -> list[str]:
    return [
        endpoint.get("cwd") or "" for endpoint in link.get("endpoints", []) if endpoint.get("enabled", True)
    ]


def home_choices(sessions, links, cwd: str | None) -> list[Choice]:
    """Linked conversations first, then sessions by recency; this folder before other projects."""
    # An unlinked conversation or a detached session is ordinary again: it is listed as a session.
    links = [link for link in links if link.get("state") != "unlinked" and not link.get("unlinked")]
    linked = {
        endpoint.get("key")
        for link in links
        for endpoint in link.get("endpoints", [])
        if endpoint.get("enabled", True)
    }
    groups: dict[str, list[Choice]] = {HERE: [], ELSEWHERE: []}

    def place(paths: list[str]) -> str:
        return (
            HERE
            if cwd is not None and any(same_directory(path, cwd) for path in paths if path)
            else ELSEWHERE
        )

    for link in links:
        where = place(_link_cwds(link))
        detail = "Sincronizada · " + ui.LINK_STATES.get(link.get("state"), ui.visible(link.get("state", "")))
        folders = [path for path in _link_cwds(link) if path]
        if where == ELSEWHERE and folders:
            detail += " · " + ui.folder_name(folders[0])
        keys = " ".join(str(endpoint.get("key", "")) for endpoint in link.get("endpoints", []))
        groups[where].append(
            Choice(
                value=("link", link),
                label=ui.visible(link.get("name") or link["id"]),
                detail=detail,
                brief="Sincronizada",
                marker="●",
                search=f"{link.get('name', '')} {' '.join(folders)} {link['id']} {keys} sincronizada",
            )
        )
    for ref in sessions:
        if ref.key in linked or is_subagent(ref):
            continue
        where = place([ref.cwd])
        groups[where].append(
            Choice(
                value=("session", ref),
                label=ui.title(ref),
                detail=_session_detail(ref, folder=where == ELSEWHERE),
                brief=" · ".join(
                    part
                    for part in (
                        ui.SHORT_TOOL_NAMES.get(ref.tool, ref.tool),
                        ui.compact_time(ref.updated_at or ref.created_at),
                    )
                    if part
                ),
                search=f"{ref.title} {ref.cwd} {ref.native_id} {ui.tool_name(ref.tool)}",
            )
        )
    if cwd is None:
        for item in groups[ELSEWHERE]:
            item.group = ""
        return groups[ELSEWHERE]
    for name, items in groups.items():
        for item in items:
            item.group = name
    return groups[HERE] + groups[ELSEWHERE]


def run_home(bridge: Any, *, cwd: str | None = None) -> Any:
    """Find any conversation, then convert it or open its synchronized copy."""
    with _colour_session():
        return _home(bridge, cwd)


def _home(bridge: Any, cwd: str | None) -> Any:
    from .sync import SyncService

    notices = []
    with _status("Buscando conversaciones…"):
        sessions = bridge.list_sessions(cwd=None)
        try:
            service = SyncService(bridge=bridge)
            links = service.list()
        except BridgeError:
            # Everything else stays usable while the synchronization store is busy or damaged.
            service, links = None, []
            notices.append(
                "No se pudieron leer las conversaciones sincronizadas; vuelve a intentarlo en unos segundos."
            )
    profiles = list(bridge.profiles())
    choices = home_choices(sessions, links, cwd)
    heading = [
        ("class:title", "Session Bridge"),
        ("class:muted", "  ·  " + (ui.visible(cwd) if cwd else "Todos los proyectos")),
    ]
    for warning in getattr(bridge, "discovery_errors", []):
        message = (
            warning.get("message", "No se pudo leer un perfil.") if isinstance(warning, dict) else warning
        )
        notices.append(message)
    for message in notices:
        heading.append(("", "\nAviso: " + ui.visible(message)))
    if not choices:
        _note("No se encontró ninguna conversación de Claude Code, Codex ni OpenCode.")
        _note("Comprueba los perfiles configurados con `sessionbridge doctor`.", "dim")
        return None
    query = ""
    while True:
        picked, query = pick(
            choices,
            heading=heading,
            placeholder="Busca por título, carpeta, asistente o ID",
            empty="Ninguna conversación coincide con «{query}». Prueba con otras palabras, una carpeta o un ID.",
            noun=("conversación", "conversaciones"),
            query=query,
        )
        if picked is None:
            return None
        kind, value = picked
        result = (
            _open_link(bridge, service, value, profiles)
            if kind == "link"
            else _convert(bridge, value, profiles)
        )
        if result is not BACK:
            return result


# ---------------------------------------------------------------------------
# Converting a session


def _context_label(context: str, context_from: str | None) -> str:
    if context_from:
        return "Desde el mensaje elegido"
    return "Historial completo" if context == "all" else "Contexto actual (desde la última compactación)"


def _pick_start(conversation) -> str | None:
    speakers = {"user": "Tú", "assistant": "Asistente"}
    choices = [
        Choice(
            value=event.id,
            label=" ".join(ui.visible(event.text).split()) or "(sin texto)",
            detail=" · ".join(
                part
                for part in (speakers.get(event.role, event.role), ui.relative_time(event.timestamp))
                if part
            ),
        )
        for event in conversation.events
        if event.active and event.kind in {"message", "summary"} and event.role in speakers
    ]
    if not choices:
        return None
    value, _ = pick(
        choices,
        heading=[("class:title", "¿Desde qué mensaje empieza el contexto?")],
        noun=("mensaje", "mensajes"),
        empty="Ningún mensaje coincide con «{query}».",
    )
    return value


def _summary(ref, target, preview, *, context: str, context_from: str | None, mode: str) -> None:
    from rich.table import Table

    fidelity = preview.get("fidelity", {})
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("Origen", f"{ui.title(ref)} · {ui.tool_name(ref.tool)}")
    grid.add_row("Destino", f"{target} · {ui.visible(preview.get('cwd') or ref.cwd)}")
    attachments = preview.get("attachments", 0)
    history = f"{_context_label(context, context_from)} · {preview.get('message_count', 0)} mensajes"
    grid.add_row("Historial", history + (f" · {attachments} adjuntos" if attachments else ""))
    grid.add_row(
        "Formato",
        "Conversación nativa, verificada al terminar"
        if mode == "native"
        else "Traspaso manual: copiarás un mensaje en una conversación nueva",
    )
    preserved, adapted, archived, missing = (
        len(fidelity.get(key, [])) for key in ("preserved", "adapted", "archive_only", "missing")
    )
    parts = [f"{preserved} conservados", f"{adapted} adaptados", f"{archived} solo en el archivo"]
    if missing:
        parts.append(f"{missing} ausentes")
    grid.add_row("Fidelidad", " · ".join(parts))
    _out().print()
    _out().print(grid)
    if adapted or archived:
        _note(
            "Lo adaptado (por ejemplo, herramientas) pasa a texto; el archivo portable guarda el original completo.",
            "dim",
        )
    if "source_activity_unknown" in fidelity.get("warnings", []):
        _note("No se sabe si la conversación sigue abierta; la copia detectará cambios mientras lee.", "dim")


def _preview(bridge, ref, conversation, target_profile, state) -> Any:
    """Preview, asking for a folder or a manual handoff only when the target needs it."""
    name = ui.tool_name(target_profile.tool)
    while True:
        try:
            with _status("Preparando la vista previa…"):
                preview = bridge.preview(
                    ref.key,
                    target_profile.id,
                    cwd=state["cwd"],
                    context=state["context"],
                    context_from=state["context_from"],
                    snapshot=state["snapshot"],
                    mode=state["mode"],
                )
        except BridgeError as exc:
            if exc.code not in {"cwd_missing", "cwd_required"}:
                raise
            folder = state["cwd"] or conversation.source.cwd
            _note(
                f"La carpeta «{ui.visible(folder)}» no existe en este equipo."
                if folder and exc.code == "cwd_missing"
                else "Esta conversación no registra su carpeta de trabajo.",
            )
            state["cwd"] = _ask_folder(f"Carpeta de trabajo para {name} (vacío para volver): ", folder or "")
            if state["cwd"] is None:
                return BACK
            continue
        if state["mode"] == "native" and preview.get("native_available") is False:
            reason = ui.visible(preview.get("capability", {}).get("reason", "versión no verificada"))
            answer = _ask(
                f"{name} no admite aquí una conversión nativa: {reason}",
                [
                    (
                        "handoff",
                        "Preparar un traspaso manual (copiarás un mensaje en una conversación nueva)",
                    ),
                    (BACK, "Volver"),
                ],
            )
            if answer is BACK:
                return BACK
            state["mode"] = "handoff"
            continue
        return preview


MAX_LINK_NAME = 120


def _sync_name(bridge, ref) -> Any:
    """A name the synchronization store will accept; None uses the link ID, BACK returns to the summary."""
    from .sync import SyncService

    try:
        store = SyncService(bridge=bridge).store
        taken = {str(link.get("name", "")).casefold() for link in store.links(include_unlinked=True)}
    except BridgeError:
        taken = set()

    def problem(name: str) -> str | None:
        if len(name) > MAX_LINK_NAME or any(ord(character) < 32 for character in name):
            return f"El nombre necesita como máximo {MAX_LINK_NAME} caracteres visibles."
        if name.casefold() in taken:
            return "Ya hay una conversación sincronizada con ese nombre; elige otro."
        return None

    title = " ".join(ref.title.split())
    default = title if title and problem(title) is None else ""
    while True:
        answer = _ask_text("Nombre de la conversación sincronizada (vacío usa su ID; Esc vuelve): ", default)
        if answer is None:
            return BACK
        if not answer:
            return None
        message = problem(answer)
        if message is None:
            return answer
        _note(message)
        default = answer[:MAX_LINK_NAME]


def _convert(bridge, ref, profiles) -> Any:
    from .cli import _emit, _resume

    targets = [profile for profile in profiles if profile.enabled and profile.id != ref.profile_id]
    _note(f"› {ui.title(ref)}  ·  {_session_detail(ref, folder=True)}", "bold")
    if not targets:
        _note("No hay otro asistente configurado. Añade uno con `sessionbridge profile add`.")
        return BACK
    target_id = _ask(
        "¿Dónde quieres continuarla?",
        [(profile.id, ui.profile_label(profile, profiles)) for profile in targets]
        + [(BACK, "Volver a la búsqueda")],
    )
    if target_id is BACK:
        return BACK
    target_profile = next(profile for profile in targets if profile.id == target_id)
    target = ui.profile_label(target_profile, profiles)
    state = {"cwd": None, "context": "current", "context_from": None, "snapshot": False, "mode": "native"}
    if ref.active is True:
        answer = _ask(
            "La conversación está abierta ahora mismo.",
            [(True, "Copiar su estado actual (instantánea de solo lectura)"), (BACK, "Volver")],
        )
        if answer is BACK:
            return BACK
        state["snapshot"] = True
    with _status("Leyendo la conversación…"):
        conversation = bridge.show(ref.key, snapshot=state["snapshot"])
    if conversation.context_ambiguous:
        answer = _ask(
            "No se puede saber con certeza dónde empieza el contexto actual.",
            [
                ("all", "Llevar el historial completo"),
                ("pick", "Elegir el mensaje desde el que empezar"),
                (BACK, "Volver"),
            ],
        )
        if answer is BACK:
            return BACK
        if answer == "all":
            state["context"] = "all"
        else:
            state["context_from"] = _pick_start(conversation)
            if state["context_from"] is None:
                return BACK
    windows = {profile.id: profile.environment == "windows" for profile in profiles}
    while True:
        preview = _preview(bridge, ref, conversation, target_profile, state)
        if preview is BACK:
            return BACK
        _summary(
            ref,
            target,
            preview,
            context=state["context"],
            context_from=state["context_from"],
            mode=state["mode"],
        )
        options: list[tuple[Any, str]] = [("convert", "Convertir")]
        if state["mode"] == "native" and windows.get(ref.profile_id) and windows.get(target_id):
            options.append(("sync", "Convertir y mantenerla sincronizada (solo su historial)"))
        if not conversation.context_ambiguous:
            options.append(
                (
                    "toggle",
                    "Llevar el historial completo"
                    if state["context"] == "current"
                    else "Llevar solo el contexto actual",
                )
            )
        options.append((BACK, "Volver a la búsqueda"))
        action = _ask("¿Qué quieres hacer?", options)
        if action is BACK:
            return BACK
        if action == "toggle":
            state["context"] = "all" if state["context"] == "current" else "current"
            continue
        name = None
        if action == "sync":
            # Settle the name before anything is written, so linking cannot fail after converting.
            name = _sync_name(bridge, ref)
            if name is BACK:
                continue
        break
    options = {key: state[key] for key in ("cwd", "context", "context_from", "snapshot", "mode")}
    with _status("Convirtiendo y verificando…"):
        transfer = bridge.convert(ref.key, target_id, **options)
    _out().print()
    _emit(transfer)
    if action == "sync":
        from .sync import SyncService
        from .sync_cli import _ensure_worker

        linked = SyncService(bridge=bridge).link(ref.key, name=name, transfer_id=transfer.id)
        linked["worker"] = _ensure_worker(bridge.data_dir)
        _emit(linked)
    if state["mode"] == "native":
        answer = _ask(f"¿Abrir ahora en {target}?", [("open", f"Abrir en {target}"), ("done", "Terminar")])
        if answer == "open":
            _resume(bridge, transfer.id)
    return None


# ---------------------------------------------------------------------------
# Opening a synchronized conversation


def _open_link(bridge, service, link, profiles) -> Any:
    from .sync_cli import _ensure_worker

    tools = {profile.id: ui.tool_name(profile.tool) for profile in profiles}
    state = ui.LINK_STATES.get(link.get("state"), ui.visible(link.get("state", "")))
    _note(f"● {ui.visible(link.get('name') or link['id'])}  ·  Sincronizada · {state}", "bold")
    if link["paused"]:
        answer = _ask(
            "El seguimiento de esta conversación está en pausa.",
            [("resume", "Reanudar el seguimiento"), (BACK, "Volver")],
        )
        if answer is BACK:
            return BACK
        service.unpause(link["id"])
    try:
        with _status("Comprobando los últimos turnos…"):
            service.poll(link["id"], force=True)
            link = service.status(link["id"])
    except BridgeError as exc:
        if exc.code not in {"sync_busy", "sync_store_invalid"}:
            raise
        _note("El seguimiento está guardando un turno ahora mismo; vuelve a intentarlo en unos segundos.")
        return BACK
    branch = None
    if link["state"] in {"conflict", "waiting", "error"}:
        if not link["heads"]:
            _note("Aún no hay un turno terminado que abrir. El seguimiento continúa en segundo plano.")
            _ensure_worker(bridge.data_dir)
            return None
        options = []
        for head in link["heads"]:
            profile_id = str(head.get("source_key", "")).split(":", 1)[0]
            source = tools.get(profile_id, ui.visible(profile_id))
            when = ui.relative_time(head.get("created_at"))
            reason = ui.REVISION_REASONS.get(head.get("reason"), ui.visible(head.get("reason", "")))
            options.append(
                (head["id"], " · ".join(part for part in (f"Desde {source}", when, reason) if part))
            )
        if len(options) > 1:
            message = "Hay más de una versión reciente. ¿Cuál quieres continuar? Las demás se conservan."
        else:
            # One version, but its origin is pending or failing: say what opening it means.
            message = (
                "El origen tiene cambios aún sin comprobar. "
                "¿Abrir el último turno terminado? Los cambios posteriores no se incluirán."
            )
            options = [(options[0][0], "Abrir el último turno terminado · " + options[0][1])]
        branch = _ask(message, options + [(BACK, "Volver")])
        if branch is BACK:
            return BACK
    targets = [profile for profile in profiles if profile.enabled and profile.environment == "windows"]
    target = _ask(
        "¿Con qué asistente quieres abrirla?",
        [(profile.id, ui.profile_label(profile, targets)) for profile in targets] + [(BACK, "Volver")],
    )
    if target is BACK:
        return BACK
    _ensure_worker(bridge.data_dir)
    cwd = None
    while True:
        try:
            service.open(link["id"], target, branch=branch, cwd=cwd, launch=True)
            return None
        except BridgeError as exc:
            if exc.code not in {"cwd_missing", "cwd_required"}:
                raise
            _note("La carpeta de trabajo de esta conversación no existe en este equipo.")
            cwd = _ask_folder("Carpeta de trabajo existente (vacío para volver): ")
            if cwd is None:
                return BACK


def prompt_profile(values: dict[str, Any]) -> dict[str, Any]:
    """Complete a profile from explicit terminal answers."""
    for field_name, label in (("id", "Identificador"), ("tool", "Herramienta"), ("home", "Carpeta de datos")):
        if not values.get(field_name):
            values[field_name] = prompt(f"{label}: ").strip()
    if not values.get("tool"):
        raise BridgeError("missing_required_arguments", "Indica la herramienta del perfil.")
    if values["tool"] not in {"claude", "opencode", "codex"}:
        raise BridgeError("invalid_tool", "La herramienta debe ser claude, opencode o codex.")
    if values.get("environment") == "wsl" and not values.get("distro"):
        values["distro"] = prompt("Distribución WSL: ").strip()
    if values.get("environment") == "wsl" and not values.get("helper"):
        values["helper"] = prompt("Python del helper en Linux: ").strip()
    return values
