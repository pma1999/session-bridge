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

from .errors import BridgeError
from .models import Profile
from .sync_cli import sync_app

SCHEMA_VERSION = 1
app = typer.Typer(
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=True,
    help="Intercambia sesiones entre asistentes locales.",
)
profile_app = typer.Typer(no_args_is_help=True, help="Gestiona perfiles locales.")
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


def _visible(value: Any, *, preserve_newlines: bool = False) -> str:
    """Make terminal controls inert while preserving ordinary Unicode text."""
    text = str(value)
    return "".join(
        ch
        if preserve_newlines and ch == "\n"
        else f"\\x{ord(ch):02x}"
        if ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F
        else ch
        for ch in text
    )


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


def _emit(value: Any, *, body: bool = False) -> None:
    if _state["json"]:
        _json_out(True, value)
        return
    if value.__class__.__name__ == "Conversation" and hasattr(value, "events"):
        source = value.source
        _console_out.print(f"Origen: {_visible(source.key)}")
        _console_out.print(f"Título: {_visible(source.title.strip())}")
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
        states = {
            "synced": "Al día",
            "waiting": "Esperando un turno terminado",
            "conflict": "Ramas pendientes de elección",
            "paused": "En pausa",
            "unlinked": "Desvinculada; historial conservado",
            "error": "Origen no verificable",
        }
        _console_out.print(f"Conversación: {_visible(value['name'])} ({_visible(value['id'])})")
        _console_out.print(f"Estado: {states.get(value['state'], _visible(value['state']))}")
        for head in value["heads"]:
            chosen = " [seleccionada]" if head["id"] == value.get("selected_head") else ""
            _console_out.print(
                f"Rama: {_visible(head['id'])}{chosen}; origen: {_visible(head['source_key'])}"
            )
        for endpoint in value["endpoints"]:
            _console_out.print(f"Sesión: {_visible(endpoint['key'])}; estado: {_visible(endpoint['state'])}")
        if value.get("last_error"):
            _console_out.print(
                f"Diagnóstico: {_visible(value['last_error'])}; usa sync check o elige una rama explícita."
            )
        if value["state"] == "conflict":
            _console_out.print("Elige con sync select CONVERSACIÓN REVISIÓN; se conservarán las demás ramas.")
        if value.get("worker"):
            _console_out.print(
                "Seguimiento local iniciado." if value["worker"].get("running") else "Seguimiento detenido."
            )
        return
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, dict) and "heads" in item for item in value)
    ):
        for item in value:
            _emit(item)
        return
    if value.__class__.__name__ == "Transfer":
        states = {
            "prepared": "Traspaso preparado",
            "completed": "Conversión verificada",
            "recovery": "Pendiente de recuperación",
            "undone": "Deshecho",
        }
        _console_out.print(f"{states.get(value.state, value.state)}: {_visible(value.id)}")
        _console_out.print(f"Destino: {_visible(value.target_profile)}; carpeta: {_visible(value.cwd)}")
        if value.native_id:
            _console_out.print(f"Sesión nativa: {_visible(value.native_id)}")
        _console_out.print(f"Archivo completo: {_visible(value.archive)}")
        _console_out.print(
            f"Conservados: {len(value.fidelity.preserved)}; adaptados: {len(value.fidelity.adapted)}; "
            f"solo en archivo: {len(value.fidelity.archive_only)}; ausentes: {len(value.fidelity.missing)}"
        )
        if value.mode == "handoff":
            _console_out.print(f"Contexto: {_visible(value.context_file)}")
            _console_out.print("Copia este mensaje en una conversación nueva; tú realizarás el primer envío:")
            _console_out.print(_visible(value.initial_message, preserve_newlines=True))
        return
    if isinstance(value, list) and not value:
        _console_out.print("No hay resultados.")
        return
    if isinstance(value, list) and all(item.__class__.__name__ == "SessionRef" for item in value):
        table = Table(show_header=True, header_style="bold", expand=True)
        for column in ("ID", "Herramienta", "Título", "Carpeta", "Actualizada", "Archivo", "Activa"):
            table.add_column(column, overflow="ellipsis")
        for ref in value:
            table.add_row(
                _visible(ref.key),
                ref.tool,
                _visible(ref.title.strip()),
                _visible(ref.cwd),
                _visible(ref.updated_at or ""),
                "sí" if ref.archived else "no",
                "sí" if ref.active else "no" if ref.active is False else "desconocido",
            )
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
) -> None:
    """Intercambia sesiones locales con una vista previa auditable."""
    global _console_out, _console_err
    _state["json"] = json_output
    _state["no_color"] = no_color or bool(os.environ.get("NO_COLOR"))
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

        _emit(run_home(_bridge()))


@app.command("list")
def list_sessions(
    tool: str | None = typer.Option(None, "--tool", "--from", help="Filtra por herramienta de origen."),
    profile: str | None = typer.Option(None, "--profile"),
    search: str | None = typer.Option(None, "--search"),
    cwd: str | None = typer.Option(None, "--cwd"),
    archived: bool | None = typer.Option(None, "--archived/--not-archived"),
    parent: str | None = typer.Option(None, "--parent"),
    since: str | None = typer.Option(None, "--since", help="Fecha ISO AAAA-MM-DD."),
    until: str | None = typer.Option(None, "--until", help="Fecha ISO AAAA-MM-DD."),
) -> None:
    """Lista metadatos de sesiones; no incluye cuerpos de mensajes."""
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
        _console_err.print(f"Aviso de catálogo: {_visible(warning)}")
    _emit(result)


@app.command()
def show(
    key: str = typer.Argument(..., help="ID profile:native de la sesión."),
    snapshot: bool = typer.Option(False, "--snapshot"),
) -> None:
    """Muestra el contenido completo de una sesión seleccionada deliberadamente."""
    _emit(_bridge().show(key, snapshot=snapshot), body=True)


@app.command()
def convert(
    key: str = typer.Argument(...),
    target: str = typer.Option(..., "--to"),
    cwd: str | None = typer.Option(None),
    context: str = typer.Option("current"),
    context_from: str | None = typer.Option(None, "--context-from"),
    snapshot: bool = typer.Option(False, "--snapshot"),
    mode: str = typer.Option("native", help="native o handoff; el handoff siempre es explícito."),
    model: str | None = typer.Option(None),
    new_copy: bool = typer.Option(False, "--new-copy"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    resume_after: bool = typer.Option(
        False, "--resume", help="Reanuda al finalizar; requiere conversión nativa verificada."
    ),
    sync_after: bool = typer.Option(
        False, "--sync", help="Mantiene esta conversación vinculada automáticamente."
    ),
    sync_name: str | None = typer.Option(None, "--sync-name", help="Nombre de la conversación vinculada."),
) -> None:
    """Transfiere una sesión; --dry-run solo genera una vista previa."""
    bridge = _bridge()
    options = {
        "cwd": cwd,
        "context": context,
        "context_from": context_from,
        "snapshot": snapshot,
        "mode": mode,
        "model": model,
    }
    if dry_run and resume_after:
        raise BridgeError("invalid_options", "--resume no se combina con --dry-run.")
    if (sync_after and mode != "native") or (sync_name and not sync_after):
        raise BridgeError("invalid_options", "--sync requiere modo native; --sync-name requiere --sync.")
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
    key: str,
    output: Path = typer.Option(..., "--output"),
    snapshot: bool = typer.Option(False, "--snapshot"),
    include_file: list[Path] = typer.Option([], "--include-file"),
) -> None:
    """Exporta un paquete de sesión."""
    _emit(_bridge().export(key, output, snapshot=snapshot, include_files=include_file or None))


@app.command("import")
def import_package(
    package: Path,
    target: str = typer.Option(..., "--to"),
    cwd: str | None = typer.Option(None),
    context: str = typer.Option("current"),
    context_from: str | None = typer.Option(None, "--context-from"),
    snapshot: bool = typer.Option(False, "--snapshot"),
    mode: str = typer.Option("native"),
    model: str | None = typer.Option(None),
    new_copy: bool = typer.Option(False, "--new-copy"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Importa un paquete con opciones de destino explícitas."""
    bridge = _bridge()
    options = {
        "cwd": cwd,
        "context": context,
        "context_from": context_from,
        "snapshot": snapshot,
        "mode": mode,
        "model": model,
        "new_copy": new_copy,
    }
    if dry_run:
        _emit(bridge.preview_package(package, target, **options))
        return
    _emit(bridge.import_package(package, target, **options))


@app.command()
def resume(key: str) -> None:
    """Reanuda la sesión con el comando nativo exacto, sin enviar un mensaje nuevo."""
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
    """Comprueba lectores y escritores disponibles."""
    _emit(_bridge().doctor())


@app.command()
def undo(transfer_id: str) -> None:
    """Revierte una transferencia verificada cuando sigue siendo segura."""
    _emit(_bridge().undo(transfer_id))


@app.command()
def recover(transfer_id: str) -> None:
    """Recupera una transferencia pendiente desde su archivo verificado."""
    _emit(_bridge().recover(transfer_id))


@app.command()
def transfers() -> None:
    """Lista transferencias preparadas o completadas."""
    _emit(_bridge().transfers())


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
