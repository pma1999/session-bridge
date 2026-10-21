"""Command line interface for local session inspection and transfer."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from . import i18n, ui
from .catalog import is_subagent
from .errors import BridgeError
from .i18n import typer_chrome
from .models import Profile
from .sync_cli import sync_app

typer_chrome.install()

SCHEMA_VERSION = 1
# Help texts are catalog keys; typer_chrome renders them in the language in use when help is shown.
app = typer.Typer(
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=True,
    help="help.app",
    epilog="help.app.examples",
)
profile_app = typer.Typer(no_args_is_help=True, help="help.profile")
wsl_app = typer.Typer(no_args_is_help=True, help="help.wsl")
app.add_typer(profile_app, name="profile")
app.add_typer(wsl_app, name="wsl")
app.add_typer(sync_app, name="sync")

_state: dict[str, Any] = {"json": False, "no_color": False}
_console_out = Console(markup=False, highlight=False)
_console_err = Console(stderr=True, markup=False, highlight=False)


def _bridge():
    # Import at use time so help/completion remain usable while the service is assembled.
    from .service import Bridge

    return Bridge()


_visible = ui.visible


def _json_out(ok: bool, payload: Any) -> None:
    if ok:
        envelope = {"schema_version": SCHEMA_VERSION, "ok": True, "data": _jsonable(payload)}
    else:
        error = payload
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": {
                "code": error.code,
                "key": error.key,
                "message": error.message,
                "details": _jsonable(error.details),
            },
        }
    print(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


# The languages `sessionbridge language` accepts, from the catalogs there are.
LANGUAGE_CHOICES = "[" + "|".join((*i18n.available(), "auto")) + "]"
TRANSFER_STATES = ("prepared", "importing", "verifying", "completed", "recovery", "undone")


def _transfer_state(state: str) -> str:
    return i18n.t(f"transfer.state.{state}") if state in TRANSFER_STATES else _visible(state)


def _rows(rows: list[tuple[str, str]]) -> None:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column(overflow="fold")
    for label, text in rows:
        grid.add_row(label, text)
    _console_out.print(grid)


def _emit_link(value: dict) -> None:
    _console_out.print(f"● {_visible(value['name'])}  ·  {ui.link_state(value)}", style="bold")
    rows = [(i18n.t("field.id"), _visible(value["id"]))]
    endpoints = [endpoint for endpoint in value["endpoints"] if endpoint.get("enabled", True)]
    current = [endpoint for endpoint in endpoints if endpoint.get("role", "current") == "current"]
    names = [ui.endpoint_tool(value, endpoint) for endpoint in current]
    for endpoint, name in zip(current, names, strict=True):
        if names.count(name) > 1:
            name += f" ({_visible(endpoint.get('profile_id', ''))})"
        native_id = str(endpoint.get("key", "")).split(":", 1)[-1]
        detail = i18n.t("link.current_session", id=_visible(native_id))
        if endpoint.get("state") not in {None, "synced"}:
            detail += f" ({ui.endpoint_state(endpoint['state'])})"
        earlier = sum(
            1
            for other in endpoints
            if other.get("role") == "superseded" and other.get("profile_id") == endpoint.get("profile_id")
        )
        if earlier:
            detail += " · " + i18n.t("link.earlier_sessions", count=earlier)
        rows.append((name, detail))
    for head in value["heads"]:
        parts = [_visible(head["id"][:12]), i18n.t("link.from", origin=ui.head_origin(head, value))]
        parts.append(ui.relative_time(head.get("created_at")))
        if head["id"] == value.get("selected_head"):
            parts.append(i18n.t("link.chosen"))
        # sync select accepts a unique prefix, so the short form is enough to type.
        rows.append((i18n.t("cli.label.version"), " · ".join(part for part in parts if part)))
    if value.get("last_error"):
        rows.append((i18n.t("cli.label.diagnosis"), ui.code_text(value["last_error"])))
    if value.get("worker"):
        running = value["worker"].get("running")
        rows.append((i18n.t("cli.label.tracking"), i18n.t("worker.running" if running else "worker.stopped")))
    _rows(rows)
    if value["state"] == "conflict":
        sources = [str(head.get("source_key", "")).split(":", 1)[0] for head in value["heads"]]
        # The assistant names a continuation when it has only one; otherwise its version does.
        choices = [
            source if sources.count(source) == 1 else head["id"][:12]
            for source, head in zip(sources, value["heads"], strict=True)
        ]
        _console_out.print(i18n.t("link.conflict.choose"), style="dim")
        for source in choices:
            _console_out.print(
                # i18n: verbatim. A command to type.
                f"  sessionbridge sync select {_visible(value['id'])} {_visible(source)}",
                style="dim",
            )
        _console_out.print(i18n.t("link.conflict.kept"), style="dim")
    elif value.get("last_error"):
        _console_out.print(i18n.t("link.check_source"), style="dim")


WORKER_STATES = ("running", "unresponsive", "stopped", "stale", "changing")


def _emit_worker(value: dict) -> None:
    """The sync worker's status, as ``sync start``, ``stop`` and ``worker-status`` report it."""
    if _state["json"]:
        _json_out(True, value)
        return
    state = value.get("state")
    label = i18n.t(f"worker.state.{state}") if state in WORKER_STATES else _visible(state)
    if value.get("acknowledged"):
        label = i18n.t("worker.stopped_now")
    elif value.get("started"):
        label = i18n.t("worker.started_now")
    _console_out.print(label, style="bold")
    rows = []
    if value.get("pid"):
        rows.append((i18n.t("worker.label.pid"), _visible(value["pid"])))
    for key, name in (("started_at", "worker.label.started"), ("heartbeat", "worker.label.heartbeat")):
        if isinstance(value.get(key), int | float):
            moment = datetime.fromtimestamp(value[key], UTC).isoformat()
            rows.append((i18n.t(name), ui.relative_time(moment)))
    if value.get("last_error"):
        rows.append((i18n.t("cli.label.diagnosis"), ui.code_text(value["last_error"])))
    if rows:
        _rows(rows)


def _emit_transfer(value: Any) -> None:
    target = value.target
    destination = profile_name = _visible(value.target_profile)
    if target is not None:
        destination = f"{ui.tool_name(target.tool)} ({profile_name})"
    mark = "✓ " if value.state == "completed" else ""
    _console_out.print(f"{mark}{_transfer_state(value.state)} · {destination}", style="bold")
    fidelity = value.fidelity
    parts = [
        i18n.t("transfer.fidelity.preserved", count=len(fidelity.preserved)),
        i18n.t("transfer.fidelity.adapted", count=len(fidelity.adapted)),
        i18n.t("transfer.fidelity.archive_only", count=len(fidelity.archive_only)),
    ]
    if fidelity.missing:
        parts.append(i18n.t("transfer.fidelity.missing", count=len(fidelity.missing)))
    rows = []
    if value.native_id:
        rows.append((i18n.t("cli.label.session"), _visible(value.native_id)))
    rows += [
        (i18n.t("cli.label.folder"), _visible(value.cwd)),
        (i18n.t("cli.label.fidelity"), " · ".join(parts)),
        (i18n.t("cli.label.archive"), _visible(value.archive)),
        (i18n.t("cli.label.transfer"), _visible(value.id)),
    ]
    if value.mode == "handoff" and value.context_file:
        rows.append((i18n.t("cli.label.context"), _visible(value.context_file)))
    _rows(rows)
    if value.mode == "handoff":
        _console_out.print()
        _console_out.print(i18n.t("transfer.handoff.copy"))
        _console_out.print(_visible(value.initial_message, preserve_newlines=True))
    elif value.state == "completed":
        _console_out.print(i18n.t("transfer.resume_hint", id=_visible(value.id)), style="dim")
    elif value.state == "recovery":
        _console_out.print(i18n.t("transfer.recover_hint", id=_visible(value.id)), style="dim")


def _emit(value: Any, *, body: bool = False, empty: str | None = None, folders: bool = True) -> None:
    if _state["json"]:
        _json_out(True, value)
        return
    if value.__class__.__name__ == "Conversation" and hasattr(value, "events"):
        source = value.source
        _console_out.print(i18n.t("cli.show.source", value=_visible(source.key)))
        _console_out.print(i18n.t("cli.show.title", value=_visible(source.title.strip())))
        from . import history

        variants = history.describe(value)
        if variants:
            # Editing an earlier message left other versions; each event says whether it is current.
            _console_out.print(i18n.t("cli.show.variants", count=len(variants)))
            for info in variants:
                _console_out.print(f"  {info.id}  {ui.variant_label(info)}")
        for event in value.events:
            where = i18n.t("cli.show.in_context" if event.active else "cli.show.archive_only")
            _console_out.print(
                i18n.t(
                    "cli.show.event",
                    role=_visible(event.role),
                    id=_visible(event.id),
                    kind=_visible(event.kind),
                    where=where,
                )
            )
            _console_out.print(_visible(event.text, preserve_newlines=True))
        for warning in value.warnings:
            _console_out.print(i18n.t("cli.warning", text=ui.warning_text(warning)))
        return
    if isinstance(value, dict) and value.get("cancelled") is True:
        _console_out.print(i18n.t("error.cancelled"))
        return
    if isinstance(value, dict) and "transfer" in value and "resume" in value:
        _emit(value["transfer"])
        if "sync" in value:
            _emit(value["sync"])
        _emit(value["resume"])
        return
    if isinstance(value, dict) and "transfer" in value and "sync" in value:
        _emit(value["transfer"])
        _emit(value["sync"])
        return
    if isinstance(value, dict) and "heads" in value and "endpoints" in value and "name" in value:
        _emit_link(value)
        return
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, dict) and "heads" in item for item in value)
    ):
        for number, item in enumerate(value):
            if number:
                _console_out.print()
            _emit_link(item)
        return
    if value.__class__.__name__ == "Transfer":
        _emit_transfer(value)
        return
    if isinstance(value, list) and not value:
        _console_out.print(empty or i18n.t("cli.no_results"))
        return
    if isinstance(value, list) and all(item.__class__.__name__ == "SessionRef" for item in value):
        rows = []
        for ref in value:
            name = ui.title(ref)
            if is_subagent(ref):
                titled = bool(" ".join(str(ref.title or "").split()))
                name = (
                    i18n.t("cli.session.subagent_named", name=name)
                    if titled
                    else i18n.t("cli.session.subagent")
                )
            if ref.archived:
                name = i18n.t("cli.session.archived", name=name)
            when = ui.relative_time(ref.updated_at or ref.created_at)
            rows.append((name, ui.tool_name(ref.tool), when, ui.folder_name(ref.cwd), _visible(ref.key)))
        # The ID is what commands take, so it is never cut. A table needs room for it and
        # for a readable title; a narrow terminal gets one short entry per conversation.
        needed = max(len(row[4]) for row in rows) + 11 + 11 + 30 + (22 if folders else 0) + 8
        if _console_out.width < needed:
            for name, tool, when, folder, key in rows:
                _console_out.print(name, style="bold", overflow="ellipsis", no_wrap=True)
                meta = [tool, when] + ([folder] if folders and folder else [])
                _console_out.print("  " + " · ".join(part for part in meta if part), style="dim")
                _console_out.print("  " + key, overflow="fold")
            return
        table = Table(show_header=True, header_style="bold", expand=True, box=None, pad_edge=False)
        table.add_column(i18n.t("cli.column.title"), ratio=1, min_width=24, no_wrap=True, overflow="ellipsis")
        table.add_column(i18n.t("cli.column.assistant"), no_wrap=True)
        table.add_column(i18n.t("cli.column.updated"), no_wrap=True)
        if folders:
            table.add_column(i18n.t("cli.column.folder"), max_width=22, no_wrap=True, overflow="ellipsis")
        table.add_column(i18n.t("field.id"), no_wrap=True)
        for name, tool, when, folder, key in rows:
            table.add_row(*(name, tool, when, *([folder] if folders else []), key))
        _console_out.print(table)
        return
    if isinstance(value, list) and all(item.__class__.__name__ == "Transfer" for item in value):
        rows = [
            (
                _visible(transfer.id),
                _transfer_state(transfer.state),
                _visible(transfer.target_profile),
                ui.folder_name(transfer.cwd),
                ui.relative_time(transfer.created_at),
            )
            for transfer in value
        ]
        # undo, recover and resume take the transfer ID, so it is never cut either.
        widths = [max(len(row[index]) for row in rows) for index in range(5)]
        if _console_out.width < sum(widths) + 8 + 10:
            for identifier, state, target, folder, when in rows:
                _console_out.print(identifier, overflow="fold")
                meta = (state, target, folder, when)
                _console_out.print("  " + " · ".join(part for part in meta if part), style="dim")
            return
        table = Table(show_header=True, header_style="bold", expand=True, box=None, pad_edge=False)
        table.add_column(i18n.t("cli.column.transfer"), no_wrap=True)
        table.add_column(i18n.t("cli.column.state"), no_wrap=True)
        table.add_column(i18n.t("cli.column.target"), no_wrap=True)
        table.add_column(i18n.t("cli.column.folder"), no_wrap=True, overflow="ellipsis")
        table.add_column(i18n.t("cli.column.date"), no_wrap=True)
        for row in rows:
            table.add_row(*row)
        _console_out.print(table)
        return
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        for key, item in value.items():
            label = ui.field_label(key)
            if isinstance(item, (dict, list)):
                _console_out.print(f"{label}:")
                _console_out.print(
                    _visible(
                        json.dumps(_readable(_jsonable(item)), ensure_ascii=False, indent=2),
                        preserve_newlines=True,
                    )
                )
            else:
                _console_out.print(f"{label}: {_visible(ui.value_text(item))}")
    elif isinstance(value, list):
        for item in value:
            _console_out.print(_visible(json.dumps(_readable(_jsonable(item)), ensure_ascii=False)))
    else:
        _console_out.print(_visible(ui.value_text(value)))


def _readable(value: Any) -> Any:
    """Nested data shown as text: a ``reason`` that is a catalog key (a capability's) reads as its text."""
    if isinstance(value, dict):
        return {
            key: i18n.text(item)
            if key == "reason" and i18n.is_key(item) and i18n.has(item, i18n.DEFAULT)
            else _readable(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_readable(item) for item in value]
    return value


def _global_flags_anywhere(args: list[str]) -> list[str]:
    """Typer globals are normally before commands; normalize the output and language flags."""
    globals_: list[str] = []
    rest: list[str] = []
    items = iter(args)
    for arg in items:
        if arg == "--":
            rest += [arg, *items]
            break
        if arg in ("--json", "--no-color") or arg.startswith("--lang="):
            globals_.append(arg)
        elif arg == "--lang":
            globals_ += [arg, next(items, "")]
        else:
            rest.append(arg)
    return globals_ + rest


def _show_version(value: bool) -> None:
    if value:
        from . import __version__

        if "--json" in sys.argv[1:]:
            _json_out(True, {"version": __version__})
        else:
            Console(markup=False, highlight=False).print(i18n.t("cli.version", version=__version__))
        raise typer.Exit()


def _use_language(value: str | None) -> str | None:
    # Eager, so `--lang en --help` already shows the help in English.
    if value:
        i18n.use(value)
    return value


@app.callback()
def root(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="help.option.json"),
    no_color: bool = typer.Option(False, "--no-color", help="help.option.no_color"),
    all_projects: bool = typer.Option(False, "--all-projects", help="help.option.all_projects_home"),
    lang: str | None = typer.Option(
        None,
        "--lang",
        metavar="LANG",  # i18n: verbatim. A placeholder, like the option names.
        help="help.option.lang",
        is_eager=True,
        callback=_use_language,
    ),
    version: bool = typer.Option(
        False, "--version", help="help.option.version", is_eager=True, callback=_show_version
    ),
) -> None:
    """Continue conversations in another assistant; with no command, open the conversation search."""
    global _console_out, _console_err
    _state["json"] = json_output
    _state["no_color"] = no_color or bool(os.environ.get("NO_COLOR"))
    _state["all_projects"] = all_projects
    _console_out = Console(markup=False, highlight=False, no_color=_state["no_color"])
    _console_err = Console(stderr=True, markup=False, highlight=False, no_color=_state["no_color"])
    if ctx.invoked_subcommand is None:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise BridgeError(
                "interactive_tty_required",
            )
        if _state["json"]:
            raise BridgeError("interactive_json_unsupported")
        from . import update_flow
        from .interactive import run_home

        check = update_flow.start_check()
        result = run_home(_bridge(), cwd=None if all_projects else str(Path.cwd()), update=check)
        if isinstance(result, update_flow.Relaunch):
            raise typer.Exit(update_flow.relaunch(result, sys.argv[1:]))
        if result is not None:
            _emit(result)


@app.command("list", help="help.cmd.list")
def list_sessions(
    tool: str | None = typer.Option(None, "--tool", "--from", help="help.list.tool"),
    profile: str | None = typer.Option(None, "--profile", help="help.list.profile"),
    search: str | None = typer.Option(None, "--search", help="help.list.search"),
    cwd: str | None = typer.Option(None, "--cwd", help="help.list.cwd"),
    all_projects: bool = typer.Option(False, "--all-projects", help="help.list.all_projects"),
    archived: bool | None = typer.Option(None, "--archived/--not-archived", help="help.list.archived"),
    parent: str | None = typer.Option(None, "--parent", help="help.list.parent"),
    since: str | None = typer.Option(None, "--since", help="help.list.since"),
    until: str | None = typer.Option(None, "--until", help="help.list.until"),
) -> None:
    "List conversations (metadata only, without reading the messages)."
    all_projects = all_projects or _state.get("all_projects", False)
    if all_projects and cwd is not None:
        raise BridgeError("invalid_arguments", "error.invalid_arguments.cwd_or_all_projects")
    cwd = cwd if cwd is not None else None if all_projects else str(Path.cwd())
    for value in (since, until):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError:
                raise BridgeError("invalid_date") from None
    bridge = _bridge()
    result = bridge.list_sessions(
        tool=tool,
        profile=profile,
        search=search,
        cwd=cwd,
        archived=archived,
        parent=parent,
        since=since,
        until=until,
    )
    for warning in getattr(bridge, "discovery_errors", []):
        text = ui.discovery_error(warning)
        profile_id = warning.get("profile") if isinstance(warning, dict) else None
        if profile_id:
            text = i18n.t("cli.warning.profile", text=text, profile=_visible(profile_id))
        _console_err.print(i18n.t("cli.warning", text=text))
    hint = [] if all_projects else [i18n.t("cli.list.all_projects_hint")]
    _emit(result, empty=" ".join([i18n.t("cli.list.empty"), *hint]), folders=cwd is None)
    if result and not _state["json"]:
        if all_projects or cwd is None:
            count = i18n.t("cli.list.count", count=len(result))
        else:
            count = i18n.t("cli.list.count_in", count=len(result), cwd=_visible(cwd))
        _console_err.print(" ".join([count, *hint]), style="dim")


@app.command(help="help.cmd.show")
def show(
    key: str = typer.Argument(..., help="help.arg.session"),
    snapshot: bool = typer.Option(False, "--snapshot", help="help.option.snapshot"),
) -> None:
    "Show every message of a session."
    _emit(_bridge().show(key, snapshot=snapshot), body=True)


@app.command(help="help.cmd.convert")
def convert(
    key: str = typer.Argument(..., help="help.arg.source_session"),
    target: str = typer.Option(..., "--to", help="help.option.to"),
    cwd: str | None = typer.Option(None, help="help.convert.cwd"),
    context: str = typer.Option("current", help="help.option.context"),
    context_from: str | None = typer.Option(None, "--context-from", help="help.option.context_from"),
    snapshot: bool = typer.Option(False, "--snapshot", help="help.option.snapshot"),
    mode: str = typer.Option("native", help="help.option.mode"),
    model: str | None = typer.Option(None, help="help.option.model"),
    new_copy: bool = typer.Option(False, "--new-copy", help="help.option.new_copy"),
    dry_run: bool = typer.Option(False, "--dry-run", help="help.option.dry_run"),
    variant: str | None = typer.Option(None, "--variant", help="help.option.variant"),
    resume_after: bool = typer.Option(False, "--resume", help="help.convert.resume"),
    sync_after: bool = typer.Option(False, "--sync", help="help.convert.sync"),
    sync_name: str | None = typer.Option(None, "--sync-name", help="help.convert.sync_name"),
) -> None:
    "Continue a session in another assistant; --dry-run only shows the preview."
    bridge = _bridge()
    options = {
        "cwd": cwd,
        "context": context,
        "context_from": context_from,
        "snapshot": snapshot,
        "mode": mode,
        "model": model,
        "variant": variant,
    }
    if dry_run and resume_after:
        raise BridgeError("invalid_options", "error.invalid_options.resume_dry_run")
    if (sync_after and mode != "native") or (sync_name and not sync_after):
        raise BridgeError("invalid_options", "error.invalid_options.sync_native")
    if sync_after and variant:
        raise BridgeError("invalid_options", "error.invalid_options.sync_variant")
    if dry_run:
        _emit(bridge.preview(key, target, **options))
        return
    transfer = bridge.convert(key, target, new_copy=new_copy, **options)
    if sync_after:
        from .sync import SyncService
        from .sync_cli import _ensure_worker, _launch_allowed

        service = SyncService(bridge=bridge)
        linked = service.link(key, name=sync_name, transfer_id=transfer.id)
        linked["worker"] = _ensure_worker(bridge.data_dir)
        if resume_after:
            _emit(
                {
                    "transfer": transfer,
                    "sync": linked,
                    "resume": service.open(
                        linked["id"], target, cwd=cwd, model=model, launch=_launch_allowed()
                    ),
                }
            )
        else:
            _emit({"transfer": transfer, "sync": linked})
        return
    if resume_after:
        _emit({"transfer": transfer, "resume": _resume(bridge, transfer.id)})
    else:
        _emit(transfer)


@app.command(help="help.cmd.export")
def export(
    key: str = typer.Argument(..., help="help.arg.session"),
    output: Path = typer.Option(..., "--output", help="help.export.output"),
    snapshot: bool = typer.Option(False, "--snapshot", help="help.option.snapshot"),
    include_file: list[Path] = typer.Option([], "--include-file", help="help.export.include_file"),
) -> None:
    "Export a session to a portable package."
    _emit(_bridge().export(key, output, snapshot=snapshot, include_files=include_file or None))


@app.command("import", help="help.cmd.import")
def import_package(
    package: Path = typer.Argument(..., help="help.import.package"),
    target: str = typer.Option(..., "--to", help="help.option.to"),
    cwd: str | None = typer.Option(None, help="help.import.cwd"),
    context: str = typer.Option("current", help="help.option.context"),
    context_from: str | None = typer.Option(None, "--context-from", help="help.option.context_from"),
    snapshot: bool = typer.Option(False, "--snapshot", help="help.import.snapshot"),
    mode: str = typer.Option("native", help="help.option.mode"),
    model: str | None = typer.Option(None, help="help.option.model"),
    new_copy: bool = typer.Option(False, "--new-copy", help="help.option.new_copy"),
    dry_run: bool = typer.Option(False, "--dry-run", help="help.option.dry_run"),
    variant: str | None = typer.Option(None, "--variant", help="help.option.variant"),
) -> None:
    "Import a portable package into an assistant."
    bridge = _bridge()
    options = {
        "cwd": cwd,
        "context": context,
        "context_from": context_from,
        "snapshot": snapshot,
        "mode": mode,
        "model": model,
        "new_copy": new_copy,
        "variant": variant,
    }
    if dry_run:
        _emit(bridge.preview_package(package, target, **options))
        return
    _emit(bridge.import_package(package, target, **options))


@app.command(help="help.cmd.resume")
def resume(
    key: str = typer.Argument(..., help="help.resume.key"),
) -> None:
    "Open the session in its assistant, without sending any message."
    _emit(_resume(_bridge(), key))


def _resume(bridge: Any, key: str) -> dict[str, Any]:
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not _state["json"]
    data_dir = getattr(bridge, "data_dir", None)
    if data_dir is not None and (Path(data_dir) / "sync.sqlite3").is_file():
        from .sync import SyncService
        from .sync_cli import _ensure_worker

        service = SyncService(bridge=bridge)
        linked = service.lookup(key)
        if linked:
            _ensure_worker(bridge.data_dir)
            return service.open(linked[0], linked[1], launch=interactive)
    result = bridge.resume(key, launch=interactive)
    if not interactive and result.get("mode") != "handoff":
        result["launched"] = False
        result["reason"] = i18n.t("cli.resume.no_terminal")
    return result


@app.command(help="help.cmd.doctor")
def doctor() -> None:
    "Check which assistants can be read and written."
    _emit(_bridge().doctor())


@app.command(help="help.cmd.undo")
def undo(transfer_id: str = typer.Argument(..., help="help.arg.transfer")) -> None:
    "Undo a conversion while it is still safe to."
    _emit(_bridge().undo(transfer_id))


@app.command(help="help.cmd.recover")
def recover(transfer_id: str = typer.Argument(..., help="help.arg.transfer")) -> None:
    "Complete an interrupted transfer from its verified archive."
    _emit(_bridge().recover(transfer_id))


@app.command(help="help.cmd.transfers")
def transfers() -> None:
    "List the transfers made."
    _emit(_bridge().transfers(), empty=i18n.t("cli.transfers.empty"))


@profile_app.command("list", help="help.cmd.profile.list")
def profile_list() -> None:
    "List the configured profiles."
    _emit(_bridge().profiles())


@profile_app.command("add", help="help.cmd.profile.add")
def profile_add(
    id: str | None = typer.Option(None, "--id", help="help.profile.add.id"),
    tool: str | None = typer.Option(None, "--tool", help="help.profile.add.tool"),
    home: str | None = typer.Option(None, "--home", help="help.profile.add.home"),
    environment: str = typer.Option("windows", "--environment", help="help.profile.add.environment"),
    distro: str | None = typer.Option(None, "--distro", help="help.option.distro"),
    executable: str | None = typer.Option(None, "--executable", help="help.profile.add.executable"),
    helper: str | None = typer.Option(None, "--helper", help="help.profile.add.helper"),
    version: str | None = typer.Option(None, "--version", help="help.profile.add.version"),
    machine: str = typer.Option("local", "--machine", help="help.profile.add.machine"),
) -> None:
    "Add a local or WSL profile; needs arguments in non-interactive mode."
    values: dict[str, Any] = {
        "id": id,
        "tool": tool,
        "home": home,
        "environment": environment,
        "distro": distro,
        "executable": executable,
        "helper": helper,
        "version": version,
        "machine": machine,
    }
    missing = [name for name in ("id", "tool", "home") if not values[name]]
    if missing and sys.stdin.isatty() and sys.stdout.isatty():
        from .interactive import prompt_profile

        values = prompt_profile(values)
    elif missing:
        raise BridgeError(
            "missing_required_arguments",
            "error.missing_required_arguments.profile_fields",
            missing=missing,
        )
    try:
        profile_value = Profile(**values)
    except ValidationError:
        raise BridgeError("invalid_profile", "error.invalid_profile.format") from None
    _emit(_bridge().register_profile(profile_value))


@wsl_app.command("status", help="help.cmd.wsl.status")
def wsl_status() -> None:
    "Show the available distributions without starting them."
    from .wsl import status

    _emit(status())


@wsl_app.command("install", help="help.cmd.wsl.install")
def wsl_install(
    distro: str = typer.Option(..., "--distro", help="help.option.distro"),
    wheel: Path = typer.Option(
        ..., "--wheel", exists=True, file_okay=True, dir_okay=False, help="help.wsl.install.wheel"
    ),
) -> None:
    "Install the helper in an explicitly chosen distribution."
    from .wsl import install

    result = install(distro, wheel)
    bridge = _bridge()
    registered = []
    for profile_data in result.get("profiles", []):
        registered.append(
            bridge.register_profile(Profile.model_validate(profile_data)).model_dump(mode="json")
        )
    result["profiles"] = registered
    _emit(result)


@wsl_app.command("start", help="help.cmd.wsl.start")
def wsl_start(distro: str = typer.Option(..., "--distro", help="help.option.distro")) -> None:
    "Start an explicitly chosen distribution."
    from .wsl import start

    _emit(start(distro))


@app.command(help="help.cmd.language")
def language(
    choice: str | None = typer.Argument(None, metavar=LANGUAGE_CHOICES, help="help.language.choice"),
) -> None:
    "Show or change the language; auto goes back to the system language."
    from .i18n import settings

    if choice is not None:
        value = choice.strip().casefold()
        settings.save_language(None if value == "auto" else i18n.validated(value))
        # This run follows the new choice too, unless --lang chose this run's language explicitly.
        if i18n.source() != "option":
            lang, source = i18n.resolve()
            i18n.use(lang, source=source)
    lang, source = i18n.current(), i18n.source()
    if _state["json"]:
        _json_out(True, {"language": lang, "source": source, "available": list(i18n.available())})
        return
    _console_out.print(i18n.t("language.current", name=i18n.name(lang), code=lang), style="bold")
    _console_out.print(i18n.t(f"language.source.{source}"), style="dim")
    others = [f"{code} ({i18n.name(code)})" for code in i18n.available() if code != lang]
    _console_out.print(i18n.t("language.change", choices=i18n.join(others)), style="dim")


UPDATE_SWITCH = "on|off"  # i18n: verbatim. The values the option takes, like the option names.


@app.command(help="help.cmd.update")
def update(
    check: bool = typer.Option(False, "--check", help="help.update.check"),
    yes: bool = typer.Option(False, "--yes", help="help.update.yes"),
    startup_check: str | None = typer.Option(
        None, "--startup-check", metavar=UPDATE_SWITCH, help="help.update.startup_check"
    ),
) -> None:
    "Look for a newer published version now and install it."
    from . import installation, update_flow, updates

    if startup_check is not None:
        value = startup_check.strip().casefold()
        if value not in ("on", "off"):
            raise BridgeError("invalid_arguments")
        updates.set_startup_check(value == "on")
        if _state["json"]:
            _json_out(True, {"startup_check": value == "on"})
        else:
            _console_out.print(i18n.t("update.startup_check.on" if value == "on" else "update.startup_check.off"))
        return
    place = installation.detect()
    with _spinner(i18n.t("update.checking")):
        release = updates.refresh()
    offer = updates.offer(place, release)
    report = {
        "current": str(updates.current()),
        "latest": str(release.version) if release else None,
        "available": offer is not None,
        "installable": offer is not None and place.updatable,
        "kind": place.kind,
        "blocker": place.blocker,
        "page_url": release.page_url if release else None,
        "startup_check": updates.startup_check(),
    }
    if offer is None:
        if _state["json"]:
            _json_out(True, report)
        else:
            _console_out.print(i18n.t("update.up_to_date", version=str(updates.current())), style="bold")
        return
    if not _state["json"]:
        _console_out.print(update_flow.describe(offer), style="bold")
    if not place.updatable:
        if _state["json"]:
            _json_out(True, report)
        else:
            _console_out.print(update_flow.notice(offer), style="dim")
        return
    if check:
        if _state["json"]:
            _json_out(True, report)
        else:
            _console_out.print(i18n.t("update.how_to_install"), style="dim")
        return
    if not yes:
        if _state["json"] or not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise BridgeError("update_needs_yes")
        from .interactive import confirm

        if not confirm(i18n.t("update.confirm", latest=str(offer.latest)), default=True):
            return
    command = update_flow.install(offer, quiet=_state["json"])
    if _state["json"]:
        _json_out(True, {**report, "updated": True, "command": str(command)})
        return
    _console_out.print(i18n.t("update.done", version=str(offer.latest)), style="bold")


def _spinner(message: str):
    import contextlib

    if _state["json"] or not _console_err.is_terminal:
        return contextlib.nullcontext()
    return _console_err.status(message)


def _error_line(error: BridgeError) -> str:
    return i18n.t("cli.error", code=_visible(error.code), message=_visible(error.message))


def main() -> None:
    """Entry point with one stable error boundary for both text and JSON use."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = _global_flags_anywhere(sys.argv[1:])
    try:
        # Before Typer runs, so help, errors and every text use the chosen language from the start.
        i18n.bootstrap(args)
        # Without standalone mode, Click returns a typer.Exit code instead of raising it.
        code = app(args=args, standalone_mode=False)
        if isinstance(code, int) and code:
            raise SystemExit(code)
    except (KeyboardInterrupt, EOFError):
        error = BridgeError("cancelled")
        if _state["json"] or "--json" in args:
            _json_out(False, error)
        else:
            _console_err.print(error.message)
        raise SystemExit(130) from None
    except BridgeError as exc:
        if _state["json"] or "--json" in args:
            _json_out(False, exc)
        else:
            _console_err.print(_error_line(exc))
            if exc.details:
                _console_err.print(
                    _visible(
                        json.dumps(_jsonable(exc.details), ensure_ascii=False, indent=2),
                        preserve_newlines=True,
                    )
                )
        raise SystemExit(2)
    except Exception as exc:  # noqa: BLE001 - the CLI boundary must never leak a traceback
        if isinstance(exc, typer.Exit):
            raise SystemExit(exc.exit_code) from None
        if exc.__class__.__name__ == "NoArgsIsHelpError":
            # A command group run on its own has already printed its help; that is the whole answer.
            raise SystemExit(0) from None
        if exc.__class__.__name__ in {
            "ClickException",
            "UsageError",
            "BadParameter",
            "NoSuchOption",
            "MissingParameter",
        }:
            error = BridgeError("invalid_arguments")
            if _state["json"] or "--json" in args:
                _json_out(False, error)
            else:
                _console_err.print(_error_line(error))
            raise SystemExit(2) from None
        error = BridgeError("internal_error")
        if _state["json"] or "--json" in args:
            _json_out(False, error)
        else:
            _console_err.print(_error_line(error))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
