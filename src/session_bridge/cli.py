"""Command line interface for local session inspection and transfer."""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from . import ui
from .catalog import is_subagent
from .errors import BridgeError
from .models import Profile
from .sync_cli import sync_app

SCHEMA_VERSION = 1
EXAMPLES = """Ejemplos:

  sessionbridge                         Busca una conversación y continúala en otro asistente.

  sessionbridge list --search "login"   Lista las conversaciones de esta carpeta que coinciden.

  sessionbridge convert ID --to codex-win --dry-run   Muestra qué se llevaría, sin escribir nada.

  sessionbridge sync list               Muestra las conversaciones sincronizadas."""
app = typer.Typer(
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=True,
    help=(
        "Continúa conversaciones de Claude Code, Codex y OpenCode en cualquiera de ellos. "
        "Sin comando, abre un buscador: elige una conversación y continúala en otro asistente."
    ),
    epilog=EXAMPLES,
)
profile_app = typer.Typer(no_args_is_help=True, help="Gestiona los perfiles de cada asistente.")
wsl_app = typer.Typer(no_args_is_help=True, help="Consulta o prepara WSL explícitamente.")
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


TRANSFER_STATES = {
    "prepared": "Traspaso preparado",
    "importing": "Importando",
    "verifying": "Verificando",
    "completed": "Conversión verificada",
    "recovery": "Pendiente de recuperación",
    "undone": "Deshecho",
}


def _rows(rows: list[tuple[str, str]]) -> None:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column(overflow="fold")
    for label, text in rows:
        grid.add_row(label, text)
    _console_out.print(grid)


def _emit_link(value: dict) -> None:
    _console_out.print(f"● {_visible(value['name'])}  ·  {ui.link_state(value)}", style="bold")
    rows = [("ID", _visible(value["id"]))]
    endpoints = [endpoint for endpoint in value["endpoints"] if endpoint.get("enabled", True)]
    current = [endpoint for endpoint in endpoints if endpoint.get("role", "current") == "current"]
    names = [ui.endpoint_tool(value, endpoint) for endpoint in current]
    for endpoint, name in zip(current, names, strict=True):
        if names.count(name) > 1:
            name += f" ({_visible(endpoint.get('profile_id', ''))})"
        native_id = str(endpoint.get("key", "")).split(":", 1)[-1]
        detail = f"sesión actual {_visible(native_id)}"
        if endpoint.get("state") not in {None, "synced"}:
            detail += f" ({_visible(endpoint['state'])})"
        earlier = sum(
            1
            for other in endpoints
            if other.get("role") == "superseded" and other.get("profile_id") == endpoint.get("profile_id")
        )
        if earlier:
            detail += f" · +{earlier} {'sesión anterior' if earlier == 1 else 'sesiones anteriores'}"
        rows.append((name, detail))
    for head in value["heads"]:
        chosen = " · elegida" if head["id"] == value.get("selected_head") else ""
        parts = [_visible(head["id"][:12]), "desde " + ui.head_origin(head, value)]
        parts.append(ui.relative_time(head.get("created_at")))
        # sync select accepts a unique prefix, so the short form is enough to type.
        rows.append(("Versión", " · ".join(part for part in parts if part) + chosen))
    if value.get("last_error"):
        rows.append(("Diagnóstico", _visible(value["last_error"])))
    if value.get("worker"):
        rows.append(("Seguimiento", "en marcha" if value["worker"].get("running") else "detenido"))
    _rows(rows)
    if value["state"] == "conflict":
        sources = [str(head.get("source_key", "")).split(":", 1)[0] for head in value["heads"]]
        # The assistant names a continuation when it has only one; otherwise its version does.
        choices = [
            source if sources.count(source) == 1 else head["id"][:12]
            for source, head in zip(sources, value["heads"], strict=True)
        ]
        _console_out.print(
            "Seguiste en más de un sitio. Abre sessionbridge para elegir con qué continuar, o usa:",
            style="dim",
        )
        for source in choices:
            _console_out.print(
                f"  sessionbridge sync select {_visible(value['id'])} {_visible(source)}", style="dim"
            )
        _console_out.print("Las demás continuaciones se conservan.", style="dim")
    elif value.get("last_error"):
        _console_out.print("Comprueba el origen con: sessionbridge sync check", style="dim")


def _emit_transfer(value: Any) -> None:
    target = value.target
    destination = profile_name = _visible(value.target_profile)
    if target is not None:
        destination = f"{ui.tool_name(target.tool)} ({profile_name})"
    mark = "✓ " if value.state == "completed" else ""
    _console_out.print(f"{mark}{TRANSFER_STATES.get(value.state, value.state)} · {destination}", style="bold")
    fidelity = value.fidelity
    parts = [
        f"{len(fidelity.preserved)} conservados",
        f"{len(fidelity.adapted)} adaptados",
        f"{len(fidelity.archive_only)} solo en el archivo",
    ]
    if fidelity.missing:
        parts.append(f"{len(fidelity.missing)} ausentes")
    rows = []
    if value.native_id:
        rows.append(("Sesión", _visible(value.native_id)))
    rows += [
        ("Carpeta", _visible(value.cwd)),
        ("Fidelidad", " · ".join(parts)),
        ("Archivo", _visible(value.archive)),
        ("Traspaso", _visible(value.id)),
    ]
    if value.mode == "handoff" and value.context_file:
        rows.append(("Contexto", _visible(value.context_file)))
    _rows(rows)
    if value.mode == "handoff":
        _console_out.print()
        _console_out.print("Copia este mensaje en una conversación nueva; el primer envío lo haces tú:")
        _console_out.print(_visible(value.initial_message, preserve_newlines=True))
    elif value.state == "completed":
        _console_out.print(
            f"Ábrela cuando quieras con: sessionbridge resume {_visible(value.id)}", style="dim"
        )
    elif value.state == "recovery":
        _console_out.print(f"Recupérala con: sessionbridge recover {_visible(value.id)}", style="dim")


def _emit(value: Any, *, body: bool = False, empty: str | None = None, folders: bool = True) -> None:
    if _state["json"]:
        _json_out(True, value)
        return
    if value.__class__.__name__ == "Conversation" and hasattr(value, "events"):
        source = value.source
        _console_out.print(f"Origen: {_visible(source.key)}")
        _console_out.print(f"Título: {_visible(source.title.strip())}")
        from . import history

        variants = history.describe(value)
        if variants:
            # Editing an earlier message left other versions; "vigente" below is the current one.
            _console_out.print(f"Variantes ({len(variants)}); elige otra con --variant:")
            for info in variants:
                _console_out.print(f"  {info.id}  {ui.variant_label(info)}")
        for event in value.events:
            _console_out.print(
                f"[{_visible(event.role)}] ID: {_visible(event.id)} "
                f"({_visible(event.kind)}, {'vigente' if event.active else 'archivo'})"
            )
            _console_out.print(_visible(event.text, preserve_newlines=True))
        for warning in value.warnings:
            _console_out.print(f"Aviso: {_visible(warning)}")
        return
    if isinstance(value, dict) and value.get("cancelled") is True:
        _console_out.print("Operación cancelada.")
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
        _console_out.print(empty or "No hay resultados.")
        return
    if isinstance(value, list) and all(item.__class__.__name__ == "SessionRef" for item in value):
        rows = []
        for ref in value:
            name = ui.title(ref)
            if is_subagent(ref):
                name = "Subagente" if name == "Sin título" else f"{name} (subagente)"
            if ref.archived:
                name += " (archivada)"
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
        table.add_column("Título", ratio=1, min_width=24, no_wrap=True, overflow="ellipsis")
        table.add_column("Asistente", no_wrap=True)
        table.add_column("Actualizada", no_wrap=True)
        if folders:
            table.add_column("Carpeta", max_width=22, no_wrap=True, overflow="ellipsis")
        table.add_column("ID", no_wrap=True)
        for name, tool, when, folder, key in rows:
            table.add_row(*(name, tool, when, *([folder] if folders else []), key))
        _console_out.print(table)
        return
    if isinstance(value, list) and all(item.__class__.__name__ == "Transfer" for item in value):
        rows = [
            (
                _visible(transfer.id),
                TRANSFER_STATES.get(transfer.state, _visible(transfer.state)),
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
        table.add_column("Traspaso", no_wrap=True)
        table.add_column("Estado", no_wrap=True)
        table.add_column("Destino", no_wrap=True)
        table.add_column("Carpeta", no_wrap=True, overflow="ellipsis")
        table.add_column("Fecha", no_wrap=True)
        for row in rows:
            table.add_row(*row)
        _console_out.print(table)
        return
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                _console_out.print(f"{_visible(key)}:")
                _console_out.print(
                    _visible(
                        json.dumps(_jsonable(item), ensure_ascii=False, indent=2), preserve_newlines=True
                    )
                )
            else:
                _console_out.print(f"{_visible(key)}: {_visible(item)}")
    elif isinstance(value, list):
        for item in value:
            _console_out.print(_visible(json.dumps(_jsonable(item), ensure_ascii=False)))
    else:
        _console_out.print(_visible(value))


def _global_flags_anywhere(args: list[str]) -> list[str]:
    """Typer globals are normally before commands; normalize the two output flags."""
    globals_: list[str] = []
    rest: list[str] = []
    for arg in args:
        if arg in ("--json", "--no-color"):
            globals_.append(arg)
        else:
            rest.append(arg)
    return globals_ + rest


@app.callback()
def root(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Devuelve una respuesta JSON versionada."),
    no_color: bool = typer.Option(False, "--no-color", help="Desactiva el color de terminal."),
    all_projects: bool = typer.Option(
        False, "--all-projects", help="Busca en todas las carpetas, sin destacar la actual."
    ),
) -> None:
    """Continúa conversaciones de Claude Code, Codex y OpenCode en cualquiera de ellos.

    Sin comando, abre un buscador de conversaciones: elige una y continúala en otro asistente.
    """
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
                "Sin terminal interactiva, indica un comando; prueba `sessionbridge list`.",
            )
        if _state["json"]:
            raise BridgeError("interactive_json_unsupported", "El asistente interactivo requiere modo texto.")
        from .interactive import run_home

        result = run_home(_bridge(), cwd=None if all_projects else str(Path.cwd()))
        if result is not None:
            _emit(result)


@app.command("list")
def list_sessions(
    tool: str | None = typer.Option(
        None, "--tool", "--from", help="Solo un asistente: claude, codex u opencode."
    ),
    profile: str | None = typer.Option(None, "--profile", help="Solo un perfil, por ejemplo codex-win."),
    search: str | None = typer.Option(
        None, "--search", help="Palabras del título, la carpeta o el ID; sin distinguir acentos."
    ),
    cwd: str | None = typer.Option(None, "--cwd", help="Carpeta de trabajo; por defecto, la actual."),
    all_projects: bool = typer.Option(False, "--all-projects", help="Lista sesiones de todas las carpetas."),
    archived: bool | None = typer.Option(
        None, "--archived/--not-archived", help="Solo archivadas, o solo sin archivar."
    ),
    parent: str | None = typer.Option(
        None, "--parent", help="Solo subagentes o bifurcaciones de esta sesión."
    ),
    since: str | None = typer.Option(None, "--since", help="Actualizadas desde esta fecha (AAAA-MM-DD)."),
    until: str | None = typer.Option(None, "--until", help="Actualizadas hasta esta fecha (AAAA-MM-DD)."),
) -> None:
    """Lista conversaciones (solo metadatos, sin leer los mensajes)."""
    all_projects = all_projects or _state.get("all_projects", False)
    if all_projects and cwd is not None:
        raise BridgeError("invalid_arguments", "Elige --cwd o --all-projects, no ambos.")
    cwd = cwd if cwd is not None else None if all_projects else str(Path.cwd())
    for value in (since, until):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError:
                raise BridgeError("invalid_date", "Las fechas deben tener formato AAAA-MM-DD.") from None
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
        message = warning.get("message", warning) if isinstance(warning, dict) else warning
        profile_id = warning.get("profile") if isinstance(warning, dict) else None
        _console_err.print(
            f"Aviso: {_visible(message)}" + (f" ({_visible(profile_id)})" if profile_id else "")
        )
    hint = "" if all_projects else " Usa --all-projects para buscar en todas las carpetas."
    _emit(result, empty="No hay conversaciones que coincidan." + hint, folders=cwd is None)
    if result and not _state["json"]:
        count = f"{len(result)} {'conversación' if len(result) == 1 else 'conversaciones'}"
        where = "" if all_projects or cwd is None else f" en {_visible(cwd)}"
        _console_err.print(f"{count}{where}.{hint}", style="dim")


@app.command()
def show(
    key: str = typer.Argument(..., help="ID de la sesión (perfil:id, o un prefijo único)."),
    snapshot: bool = typer.Option(
        False, "--snapshot", help="Lee una instantánea aunque la sesión siga abierta."
    ),
) -> None:
    """Muestra todos los mensajes de una sesión."""
    _emit(_bridge().show(key, snapshot=snapshot), body=True)


@app.command()
def convert(
    key: str = typer.Argument(..., help="ID de la sesión de origen (perfil:id, o un prefijo único)."),
    target: str = typer.Option(..., "--to", help="Perfil de destino, por ejemplo codex-win."),
    cwd: str | None = typer.Option(
        None, help="Carpeta de trabajo en el destino; por defecto, la del origen."
    ),
    context: str = typer.Option(
        "current", help="current: desde la última compactación; all: historial completo."
    ),
    context_from: str | None = typer.Option(
        None, "--context-from", help="ID del mensaje desde el que empieza el contexto."
    ),
    snapshot: bool = typer.Option(
        False, "--snapshot", help="Lee una instantánea aunque la sesión siga abierta."
    ),
    mode: str = typer.Option("native", help="native o handoff; el handoff siempre es explícito."),
    model: str | None = typer.Option(None, help="Modelo con el que abrir el destino."),
    new_copy: bool = typer.Option(False, "--new-copy", help="Crea otra copia aunque ya exista una."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Muestra la vista previa sin escribir nada."),
    variant: str | None = typer.Option(
        None, "--variant", help="Variante que llevar si editaste mensajes; por defecto, la actual."
    ),
    resume_after: bool = typer.Option(
        False, "--resume", help="Reanuda al finalizar; requiere conversión nativa verificada."
    ),
    sync_after: bool = typer.Option(
        False, "--sync", help="Mantiene esta conversación vinculada automáticamente."
    ),
    sync_name: str | None = typer.Option(None, "--sync-name", help="Nombre de la conversación vinculada."),
) -> None:
    """Continúa una sesión en otro asistente; --dry-run solo muestra la vista previa."""
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
        raise BridgeError("invalid_options", "--resume no se combina con --dry-run.")
    if (sync_after and mode != "native") or (sync_name and not sync_after):
        raise BridgeError("invalid_options", "--sync requiere modo native; --sync-name requiere --sync.")
    if sync_after and variant:
        raise BridgeError(
            "invalid_options", "--sync sigue la variante actual del origen; no admite --variant."
        )
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


@app.command()
def export(
    key: str = typer.Argument(..., help="ID de la sesión (perfil:id, o un prefijo único)."),
    output: Path = typer.Option(..., "--output", help="Paquete que se creará."),
    snapshot: bool = typer.Option(
        False, "--snapshot", help="Lee una instantánea aunque la sesión siga abierta."
    ),
    include_file: list[Path] = typer.Option(
        [], "--include-file", help="Archivo adicional que incluir; repetible."
    ),
) -> None:
    """Exporta una sesión a un paquete portable."""
    _emit(_bridge().export(key, output, snapshot=snapshot, include_files=include_file or None))


@app.command("import")
def import_package(
    package: Path = typer.Argument(..., help="Paquete exportado con sessionbridge export."),
    target: str = typer.Option(..., "--to", help="Perfil de destino, por ejemplo codex-win."),
    cwd: str | None = typer.Option(None, help="Carpeta de trabajo en el destino."),
    context: str = typer.Option(
        "current", help="current: desde la última compactación; all: historial completo."
    ),
    context_from: str | None = typer.Option(
        None, "--context-from", help="ID del mensaje desde el que empieza el contexto."
    ),
    snapshot: bool = typer.Option(
        False, "--snapshot", help="Sin efecto en paquetes; se acepta por compatibilidad."
    ),
    mode: str = typer.Option("native", help="native o handoff; el handoff siempre es explícito."),
    model: str | None = typer.Option(None, help="Modelo con el que abrir el destino."),
    new_copy: bool = typer.Option(False, "--new-copy", help="Crea otra copia aunque ya exista una."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Muestra la vista previa sin escribir nada."),
    variant: str | None = typer.Option(
        None, "--variant", help="Variante que llevar si editaste mensajes; por defecto, la actual."
    ),
) -> None:
    """Importa un paquete portable en un asistente."""
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


@app.command()
def resume(
    key: str = typer.Argument(
        ..., help="ID de la sesión, de un traspaso o de una conversación sincronizada."
    ),
) -> None:
    """Abre la sesión en su asistente, sin enviar ningún mensaje."""
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
        result["reason"] = (
            "Use una terminal interactiva para iniciar el cliente; argv contiene el comando exacto."
        )
    return result


@app.command()
def doctor() -> None:
    """Comprueba qué asistentes se pueden leer y escribir."""
    _emit(_bridge().doctor())


@app.command()
def undo(transfer_id: str = typer.Argument(..., help="ID del traspaso (tr_…).")) -> None:
    """Deshace una conversión mientras siga siendo seguro."""
    _emit(_bridge().undo(transfer_id))


@app.command()
def recover(transfer_id: str = typer.Argument(..., help="ID del traspaso (tr_…).")) -> None:
    """Completa un traspaso interrumpido desde su archivo verificado."""
    _emit(_bridge().recover(transfer_id))


@app.command()
def transfers() -> None:
    """Lista los traspasos realizados."""
    _emit(
        _bridge().transfers(),
        empty="Aún no hay traspasos. Ejecuta sessionbridge para convertir una conversación.",
    )


@profile_app.command("list")
def profile_list() -> None:
    """Lista los perfiles configurados."""
    _emit(_bridge().profiles())


@profile_app.command("add")
def profile_add(
    id: str | None = typer.Option(None, "--id"),
    tool: str | None = typer.Option(None, "--tool"),
    home: str | None = typer.Option(None, "--home"),
    environment: str = typer.Option("windows", "--environment"),
    distro: str | None = typer.Option(None, "--distro"),
    executable: str | None = typer.Option(None, "--executable"),
    helper: str | None = typer.Option(None, "--helper"),
    version: str | None = typer.Option(None, "--version"),
    machine: str = typer.Option("local", "--machine"),
) -> None:
    """Añade un perfil local o WSL; requiere argumentos en modo no interactivo."""
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
            "Indica --id, --tool y --home en modo no interactivo.",
            missing=missing,
        )
    try:
        profile_value = Profile(**values)
    except ValidationError:
        raise BridgeError(
            "invalid_profile", "Los datos del perfil no cumplen el formato requerido."
        ) from None
    _emit(_bridge().register_profile(profile_value))


@wsl_app.command("status")
def wsl_status() -> None:
    """Consulta distribuciones disponibles sin iniciarlas."""
    from .wsl import status

    _emit(status())


@wsl_app.command("install")
def wsl_install(
    distro: str = typer.Option(..., "--distro"),
    wheel: Path = typer.Option(..., "--wheel", exists=True, file_okay=True, dir_okay=False),
) -> None:
    """Instala el helper en una distribución elegida explícitamente."""
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


@wsl_app.command("start")
def wsl_start(distro: str = typer.Option(..., "--distro")) -> None:
    """Inicia una distribución elegida explícitamente."""
    from .wsl import start

    _emit(start(distro))


def main() -> None:
    """Entry point with one stable error boundary for both text and JSON use."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = _global_flags_anywhere(sys.argv[1:])
    try:
        app(args=args, standalone_mode=False)
    except (KeyboardInterrupt, EOFError):
        error = BridgeError("cancelled", "Operación cancelada.")
        if _state["json"] or "--json" in args:
            _json_out(False, error)
        else:
            _console_err.print(error.message)
        raise SystemExit(130) from None
    except BridgeError as exc:
        if _state["json"] or "--json" in args:
            _json_out(False, exc)
        else:
            _console_err.print(f"Error [{_visible(exc.code)}]: {_visible(exc.message)}")
            if exc.details:
                _console_err.print(_visible(json.dumps(_jsonable(exc.details), ensure_ascii=False, indent=2)))
        raise SystemExit(2)
    except Exception as exc:  # noqa: BLE001 - the CLI boundary must never leak a traceback
        if isinstance(exc, typer.Exit):
            raise SystemExit(exc.exit_code) from None
        if exc.__class__.__name__ in {
            "ClickException",
            "UsageError",
            "BadParameter",
            "NoSuchOption",
            "MissingParameter",
        }:
            error = BridgeError("invalid_arguments", "Argumentos no válidos; consulta --help.")
            if _state["json"] or "--json" in args:
                _json_out(False, error)
            else:
                _console_err.print(f"Error [{error.code}]: {error.message}")
            raise SystemExit(2) from None
        error = BridgeError("internal_error", "No se pudo completar la operación.")
        if _state["json"] or "--json" in args:
            _json_out(False, error)
        else:
            _console_err.print(f"Error [{error.code}]: {error.message}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
