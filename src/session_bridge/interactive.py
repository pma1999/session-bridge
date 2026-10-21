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

from . import i18n, ui
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
# Group identities; what they read as is the catalog's (home.group.<identity>).
HERE, ELSEWHERE = "here", "elsewhere"


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
        noun: str,
        query: str = "",
        group_label=None,
    ):
        # placeholder, empty ({query}) and noun ({count}) are catalog keys.
        self.choices = choices
        self.heading = heading
        self.empty = empty
        self.noun = noun
        self.group_label = group_label or (lambda group: group)
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
                AfterInput(lambda: "" if self.buffer.text else i18n.t(placeholder), style="class:muted")
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
        message = i18n.t(self.empty, query=ui.visible(query))
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
                fragments.append(("class:group", "  " + _fit(self.group_label(value), width - 2)))
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
            i18n.t("picker.matches", shown=shown, total=total)
            if self.buffer.text.strip()
            else i18n.t(self.noun, count=total)
        )
        width = self._size().columns - 1
        # Drop the hints a reader needs least when the terminal is narrow.
        for hints in ("picker.hints.full", "picker.hints.short", "picker.hints.minimal", None):
            text = f"  {count}" + (" · " + i18n.t(hints) if hints else "")
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
    placeholder: str = "picker.placeholder",
    empty: str = "picker.empty",
    noun: str = "picker.count.option",
    query: str = "",
    group_label=None,
) -> tuple[Any, str]:
    """Search-as-you-type list: returns the chosen value (None on Esc) and the final query.

    The texts are catalog keys: ``empty`` gets the ``query`` and ``noun`` the ``count``."""
    return _Picker(
        choices,
        heading=heading,
        placeholder=placeholder,
        empty=empty,
        noun=noun,
        query=query,
        group_label=group_label,
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


def _columns() -> int:
    try:
        from prompt_toolkit.application.current import get_app_session

        return get_app_session().output.get_size().columns
    except Exception:  # noqa: BLE001 - a menu must render even without a measurable terminal
        return 80


# The menu indents each label by about ten columns ("   ›  N. ") and never wraps it.
MENU_INDENT = 10


def _ask(message: str, options: list[tuple[Any, str]], *, default: Any = None) -> Any:
    """Arrow-key menu; Esc goes back one step. Text is fitted to the terminal, never cut mid-word."""
    width = max(20, _columns() - 1)
    # Addresses and paths stay whole (a hyphen is not a break), so they can still be copied or clicked.
    wrapped = (textwrap.wrap(line, width, break_on_hyphens=False, break_long_words=False) for line in message.split("\n"))
    message = "\n".join("\n".join(lines or [""]) for lines in wrapped)
    options = [(value, _fit(label, width - MENU_INDENT)) for value, label in options]
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
    suffix = i18n.t("confirm.default_yes" if default else "confirm.default_no")
    # The words of the language in use, and the English ones, which every keyboard user knows.
    yes = set(i18n.t("confirm.yes_words").split()) | {"y", "yes"}
    no = set(i18n.t("confirm.no_words").split()) | {"n", "no"}
    while True:
        answer = prompt(f"{message} {suffix}: ").strip().casefold()
        if not answer:
            return default
        if answer in yes:
            return True
        if answer in no:
            return False
        print(i18n.t("confirm.retry"))


# ---------------------------------------------------------------------------
# Home: every conversation in one search


def _session_detail(ref, *, folder: bool) -> str:
    parts = [ui.tool_name(ref.tool), ui.relative_time(ref.updated_at or ref.created_at)]
    if ref.archived:
        parts.append(i18n.t("home.archived"))
    if folder and ref.cwd:
        parts.append(ui.folder_name(ref.cwd))
    return " · ".join(part for part in parts if part)


def _link_cwds(link) -> list[str]:
    return [
        endpoint.get("cwd") or "" for endpoint in link.get("endpoints", []) if endpoint.get("enabled", True)
    ]


def _link_summary(link) -> str:
    """Where the conversation was last continued, or what it is waiting for."""
    head = ui.chosen_head(link)
    if link.get("state") == "synced" and head:
        when = ui.relative_time(head.get("created_at"))
        return " · ".join(
            part for part in (i18n.t("home.last_in", origin=ui.head_origin(head, link)), when) if part
        )
    return ui.link_state(link)


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
        detail = i18n.t("home.synced") + " · " + _link_summary(link)
        folders = [path for path in _link_cwds(link) if path]
        if where == ELSEWHERE and folders:
            detail += " · " + ui.folder_name(folders[0])
        keys = " ".join(str(endpoint.get("key", "")) for endpoint in link.get("endpoints", []))
        groups[where].append(
            Choice(
                value=("link", link),
                label=ui.visible(link.get("name") or link["id"]),
                detail=detail,
                brief=i18n.t("home.synced"),
                marker="●",
                # Typing "synced" finds these in any language.
                search=f"{link.get('name', '')} {' '.join(folders)} {link['id']} {keys} {_synced_words()}",
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


def _synced_words() -> str:
    return " ".join(i18n.t("home.synced", lang=lang) for lang in i18n.available())


def _group_label(group: str) -> str:
    return i18n.t(f"home.group.{group}") if group in (HERE, ELSEWHERE) else group


def run_home(bridge: Any, *, cwd: str | None = None, update: Any = None) -> Any:
    """Find any conversation, then convert it or open its synchronized copy.

    ``update`` is a running update check; when the user installs the update it offers, the
    result is an ``update_flow.Relaunch`` for the caller to open the new version with.
    """
    with _colour_session():
        return _home(bridge, cwd, update)


def _home(bridge: Any, cwd: str | None, update: Any = None) -> Any:
    from . import update_flow
    from .sync import SyncService

    notices = []
    with _status(i18n.t("home.searching")):
        sessions = bridge.list_sessions(cwd=None)
        try:
            service = SyncService(bridge=bridge)
            links = service.list()
        except BridgeError:
            # Everything else stays usable while the synchronization store is busy or damaged.
            service, links = None, []
            notices.append(i18n.t("home.links_unavailable"))
    # Conversations loaded while the update check ran; its answer is asked about before the search.
    relaunch = update_flow.at_start(update, notices)
    if relaunch is not None:
        return relaunch
    profiles = list(bridge.profiles())
    choices = home_choices(sessions, links, cwd)
    heading = [
        ("class:title", "Session Bridge"),
        ("class:muted", "  ·  " + (ui.visible(cwd) if cwd else i18n.t("home.all_projects"))),
    ]
    for warning in getattr(bridge, "discovery_errors", []):
        notices.append(ui.discovery_error(warning))
    for message in notices:
        heading.append(("", "\n" + i18n.t("cli.warning", text=ui.visible(message))))
    if not choices:
        _note(i18n.t("home.nothing_found"))
        _note(i18n.t("home.check_profiles"), "dim")
        return None
    query = ""
    while True:
        picked, query = pick(
            choices,
            heading=heading,
            placeholder="home.placeholder",
            empty="home.empty",
            noun="picker.count.conversation",
            query=query,
            group_label=_group_label,
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
        return i18n.t("summary.context.chosen_start")
    return i18n.t("summary.context.all" if context == "all" else "summary.context.current")


def _variants(preview) -> list[Any]:
    from .history import VariantInfo

    return [VariantInfo(**item) for item in preview.get("variants") or []]


def _pick_variant(variants, chosen) -> Any:
    """The version to take; None is the one the source would resume."""
    options = [(None if info.current else info.id, ui.variant_label(info)) for info in variants]
    answer = _ask(i18n.t("variant.question"), options + [(BACK, i18n.t("menu.back"))])
    return chosen if answer is BACK else answer


def _pick_start(conversation) -> str | None:
    speakers = {"user": i18n.t("start.you"), "assistant": i18n.t("start.assistant")}
    choices = [
        Choice(
            value=event.id,
            label=" ".join(ui.visible(event.text).split()) or i18n.t("start.no_text"),
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
        heading=[("class:title", i18n.t("start.question"))],
        noun="picker.count.message",
        empty="start.empty",
    )
    return value


def _summary(ref, target, preview, *, context: str, context_from: str | None, mode: str) -> None:
    from rich.table import Table

    fidelity = preview.get("fidelity", {})
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row(i18n.t("summary.source"), f"{ui.title(ref)} · {ui.tool_name(ref.tool)}")
    grid.add_row(i18n.t("summary.target"), f"{target} · {ui.visible(preview.get('cwd') or ref.cwd)}")
    attachments = preview.get("attachments", 0)
    history = [
        _context_label(context, context_from),
        i18n.t("summary.messages", count=preview.get("message_count", 0)),
    ]
    if attachments:
        history.append(i18n.t("summary.attachments", count=attachments))
    grid.add_row(i18n.t("summary.history"), " · ".join(history))
    variants = _variants(preview)
    if variants:
        chosen = next(
            (info for info in variants if info.id == preview.get("variant")),
            next(info for info in variants if info.current),
        )
        what = " · " + i18n.quote(ui._excerpt(chosen.prompt)) if chosen.prompt else ""
        which = "summary.variant.current" if chosen.current else "summary.variant.other"
        grid.add_row(i18n.t("summary.variant"), i18n.t(which, total=len(variants)) + what)
    subagents = preview.get("subagents", 0)
    if subagents:
        grid.add_row(i18n.t("summary.subagents"), i18n.t("summary.subagents.detail", count=subagents))
    grid.add_row(
        i18n.t("summary.format"),
        i18n.t("summary.format.native" if mode == "native" else "summary.format.handoff"),
    )
    preserved, adapted, archived, missing = (
        len(fidelity.get(key, [])) for key in ("preserved", "adapted", "archive_only", "missing")
    )
    parts = [
        i18n.t("transfer.fidelity.preserved", count=preserved),
        i18n.t("transfer.fidelity.adapted", count=adapted),
        i18n.t("transfer.fidelity.archive_only", count=archived),
    ]
    if missing:
        parts.append(i18n.t("transfer.fidelity.missing", count=missing))
    grid.add_row(i18n.t("cli.label.fidelity"), " · ".join(parts))
    _out().print()
    _out().print(grid)
    if adapted or archived:
        _note(i18n.t("summary.adapted_note"), "dim")
    if "source_activity_unknown" in fidelity.get("warnings", []):
        _note(i18n.t("summary.activity_unknown_note"), "dim")


def _preview(bridge, ref, conversation, target_profile, state) -> Any:
    """Preview, asking for a folder or a manual handoff only when the target needs it."""
    name = ui.tool_name(target_profile.tool)
    while True:
        try:
            with _status(i18n.t("preview.preparing")):
                preview = bridge.preview(
                    ref.key,
                    target_profile.id,
                    cwd=state["cwd"],
                    context=state["context"],
                    context_from=state["context_from"],
                    snapshot=state["snapshot"],
                    mode=state["mode"],
                    variant=state["variant"],
                )
        except BridgeError as exc:
            if exc.code not in {"cwd_missing", "cwd_required"}:
                raise
            folder = state["cwd"] or conversation.source.cwd
            _note(
                i18n.t("preview.folder_missing", folder=ui.visible(folder))
                if folder and exc.code == "cwd_missing"
                else i18n.t("preview.folder_unknown"),
            )
            state["cwd"] = _ask_folder(i18n.t("preview.folder_prompt", name=name) + " ", folder or "")
            if state["cwd"] is None:
                return BACK
            continue
        if state["mode"] == "native" and preview.get("native_available") is False:
            reason = preview.get("capability", {}).get("reason") or "capability.unverified_version"
            answer = _ask(
                i18n.t("preview.native_unavailable", name=name, reason=ui.visible(i18n.text(reason))),
                [("handoff", i18n.t("preview.prepare_handoff")), (BACK, i18n.t("menu.back"))],
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
            return i18n.t("sync_name.too_long", limit=MAX_LINK_NAME)
        if name.casefold() in taken:
            return i18n.t("sync_name.taken")
        return None

    title = " ".join(ref.title.split())
    default = title if title and problem(title) is None else ""
    while True:
        answer = _ask_text(i18n.t("sync_name.prompt") + " ", default)
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
        _note(i18n.t("convert.no_targets"))
        return BACK
    target_id = _ask(
        i18n.t("convert.where"),
        [(profile.id, ui.profile_label(profile, profiles)) for profile in targets]
        + [(BACK, i18n.t("menu.back_to_search"))],
    )
    if target_id is BACK:
        return BACK
    target_profile = next(profile for profile in targets if profile.id == target_id)
    target = ui.profile_label(target_profile, profiles)
    state = {
        "cwd": None,
        "context": "current",
        "context_from": None,
        "snapshot": False,
        "mode": "native",
        "variant": None,
    }
    if ref.active is True:
        answer = _ask(
            i18n.t("convert.open_now"),
            [(True, i18n.t("convert.snapshot")), (BACK, i18n.t("menu.back"))],
        )
        if answer is BACK:
            return BACK
        state["snapshot"] = True
    with _status(i18n.t("convert.reading")):
        conversation = bridge.show(ref.key, snapshot=state["snapshot"])
    if conversation.context_ambiguous:
        answer = _ask(
            i18n.t("convert.ambiguous"),
            [
                ("all", i18n.t("menu.take_all")),
                ("pick", i18n.t("menu.pick_start")),
                (BACK, i18n.t("menu.back")),
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
        options: list[tuple[Any, str]] = [("convert", i18n.t("menu.convert"))]
        variants = _variants(preview)
        syncable = state["mode"] == "native" and windows.get(ref.profile_id) and windows.get(target_id)
        if syncable and state["variant"] is None:
            options.append(("sync", i18n.t("menu.convert_sync.detail", option=i18n.t("menu.convert_sync"))))
        elif syncable:
            _note(i18n.t("convert.sync_follows_current", tool=ui.tool_name(ref.tool)), "dim")
        if variants and not conversation.context_ambiguous:
            options.append(("variant", i18n.t("menu.other_variant", count=len(variants))))
        if not conversation.context_ambiguous:
            toggle = "menu.take_all" if state["context"] == "current" else "menu.take_current"
            options.append(("toggle", i18n.t(toggle)))
        options.append((BACK, i18n.t("menu.back_to_search")))
        action = _ask(i18n.t("convert.what_next"), options)
        if action is BACK:
            return BACK
        if action == "toggle":
            state["context"] = "all" if state["context"] == "current" else "current"
            continue
        if action == "variant":
            state["variant"] = _pick_variant(variants, state["variant"])
            continue
        name = None
        if action == "sync":
            # Settle the name before anything is written, so linking cannot fail after converting.
            name = _sync_name(bridge, ref)
            if name is BACK:
                continue
        break
    options = {key: state[key] for key in ("cwd", "context", "context_from", "snapshot", "mode", "variant")}
    with _status(i18n.t("convert.converting")):
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
        answer = _ask(
            i18n.t("convert.open_question", target=target),
            [("open", i18n.t("convert.open_in", target=target)), ("done", i18n.t("menu.finish"))],
        )
        if answer == "open":
            _resume(bridge, transfer.id)
    return None


# ---------------------------------------------------------------------------
# Opening a synchronized conversation


def _open_link(bridge, service, link, profiles) -> Any:
    from .sync_cli import _ensure_worker

    name = ui.visible(link.get("name") or link["id"])
    _note(f"● {name}  ·  {i18n.t('home.synced')} · {ui.link_state(link)}", "bold")
    if link["paused"]:
        answer = _ask(
            i18n.t("open.paused"),
            [("resume", i18n.t("open.resume_tracking")), (BACK, i18n.t("menu.back"))],
        )
        if answer is BACK:
            return BACK
        service.unpause(link["id"])
    try:
        with _status(i18n.t("open.checking")):
            service.poll(link["id"], force=True)
            link = service.status(link["id"])
    except BridgeError as exc:
        if exc.code not in {"sync_busy", "sync_store_invalid"}:
            raise
        _note(i18n.t("open.busy"))
        return BACK
    branch = None
    if link["state"] in {"conflict", "waiting", "error"}:
        if not link["heads"]:
            _note(i18n.t("open.no_turn_yet"))
            _ensure_worker(bridge.data_dir)
            return None
        branch = _pick_continuation(service, link)
        if branch is BACK:
            return BACK
    targets = [profile for profile in profiles if profile.enabled and profile.environment == "windows"]
    target = _ask(
        i18n.t("open.which_assistant"),
        [(profile.id, ui.profile_label(profile, targets)) for profile in targets]
        + [(BACK, i18n.t("menu.back"))],
    )
    if target is BACK:
        return BACK
    _ensure_worker(bridge.data_dir)
    cwd = None
    while True:
        try:
            _note(_opening_note(service.plan_open(link["id"], target, branch=branch, cwd=cwd), link), "dim")
            service.open(link["id"], target, branch=branch, cwd=cwd, launch=True)
            return None
        except BridgeError as exc:
            if exc.code not in {"cwd_missing", "cwd_required"}:
                raise
            _note(i18n.t("open.folder_missing"))
            cwd = _ask_folder(i18n.t("open.folder_prompt") + " ")
            if cwd is None:
                return BACK


def _pick_continuation(service, link) -> Any:
    """Choose which continuation to follow, in words; a real choice is remembered."""
    heads = link["heads"]
    if len(heads) == 1:
        # One version, but its origin is pending or failing: say what opening it means.
        head = heads[0]
        detail = " · ".join(
            part
            for part in (
                i18n.t("continuation.from", origin=ui.head_origin(head, link)),
                ui.relative_time(head.get("created_at")),
                ui.revision_reason(head.get("reason")),
            )
            if part
        )
        return _ask(
            i18n.t("continuation.pending"),
            [(head["id"], i18n.t("continuation.open_last") + " · " + detail), (BACK, i18n.t("menu.back"))],
        )
    previews = service.head_previews(link["id"])
    options = []
    for head in heads:
        preview = previews.get(head["id"])
        parts = (
            ui.capitalized(ui.head_origin(head, link)),
            ui.relative_time(head.get("created_at")),
            i18n.quote(preview) if preview else "",
        )
        options.append((head["id"], " · ".join(part for part in parts if part)))
    question = "continuation.question_two" if len(heads) == 2 else "continuation.question_many"
    chosen = _ask(i18n.t(question), options + [(BACK, i18n.t("menu.back"))])
    if chosen is not BACK and link["state"] == "conflict":
        service.select(link["id"], chosen)
    return chosen


def _opening_note(plan, link) -> str:
    tool = ui.tool_name(plan["tool"])
    if plan["reuse"]:
        return i18n.t("opening.reuse", tool=tool)
    source = ui.endpoint_tool(link, ui._endpoint(link, plan["source_key"]), plan["source_key"])
    note = (
        i18n.t("opening.new", tool=tool)
        if source == tool
        else i18n.t("opening.new_from", tool=tool, source=source)
    )
    if any(
        endpoint.get("profile_id") == plan["profile_id"] and endpoint.get("enabled", True)
        for endpoint in link.get("endpoints") or []
    ):
        note += " " + i18n.t("opening.earlier_kept")
    return note


def prompt_profile(values: dict[str, Any]) -> dict[str, Any]:
    """Complete a profile from explicit terminal answers."""
    for field_name in ("id", "tool", "home"):
        if not values.get(field_name):
            values[field_name] = prompt(i18n.t(f"profile_prompt.{field_name}") + ": ").strip()
    if not values.get("tool"):
        raise BridgeError("missing_required_arguments", "error.missing_required_arguments.tool")
    if values["tool"] not in {"claude", "opencode", "codex"}:
        raise BridgeError("invalid_tool")
    if values.get("environment") == "wsl" and not values.get("distro"):
        values["distro"] = prompt(i18n.t("profile_prompt.distro") + ": ").strip()
    if values.get("environment") == "wsl" and not values.get("helper"):
        values["helper"] = prompt(i18n.t("profile_prompt.helper") + ": ").strip()
    return values
