"""Spanish commands for explicitly linked local conversations."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

sync_app = typer.Typer(no_args_is_help=True, help="Mantiene conversaciones sincronizadas entre asistentes.")


def _service():
    from .sync import SyncService

    return SyncService()


NO_LINKS = (
    "No hay conversaciones sincronizadas. Ejecuta sessionbridge, elige una conversación "
    "y «Convertir y mantenerla sincronizada»."
)


def _emit(value, **options) -> None:
    from .cli import _emit as emit

    emit(value, **options)


def _daemon():
    from . import sync_daemon

    return sync_daemon


def _ensure_worker(data_dir: Path | None = None) -> dict:
    """Shared by link, resume and the converter's opt-in sync."""
    return _daemon().start(data_dir)


def _launch_allowed() -> bool:
    from .cli import _state

    return not _state["json"] and sys.stdin.isatty() and sys.stdout.isatty()


@sync_app.command("link")
def link(
    key: str = typer.Argument(..., help="Sesión de origen: perfil:identificador."),
    name: str | None = typer.Option(None, "--name", help="Nombre del vínculo."),
    transfer: str | None = typer.Option(None, "--transfer", help="Traspaso nativo ya verificado."),
    no_start: bool = typer.Option(False, "--no-start", help="Vincula sin iniciar el trabajador."),
) -> None:
    """Vincula una sesión y activa la sincronización en segundo plano."""
    result = _service().link(key, name=name, transfer_id=transfer)
    if not no_start:
        result = {**result, "worker": _ensure_worker()}
    _emit(result)


@sync_app.command("list")
def list_links() -> None:
    """Muestra los vínculos y su estado."""
    _emit(_service().list(), empty=NO_LINKS)


@sync_app.command("status")
def link_status(link: str | None = typer.Argument(None, help="Nombre o identificador del vínculo.")) -> None:
    """Consulta un vínculo, o todos si no indicas ninguno."""
    service = _service()
    _emit(service.status(link) if link is not None else service.list(), empty=NO_LINKS)


@sync_app.command("check")
def check(link: str | None = typer.Argument(None, help="Vínculo; omitido comprueba todos.")) -> None:
    """Captura los nuevos turnos completos sin abrir asistentes."""
    _emit(_service().poll(link))


@sync_app.command("open")
def open_link(
    link: str = typer.Argument(...),
    target: str = typer.Option(..., "--to", help="Perfil nativo de destino."),
    branch: str | None = typer.Option(
        None, "--branch", help="Continuación a abrir si hay varias: versión o perfil del asistente."
    ),
    cwd: str | None = typer.Option(None, "--cwd", help="Carpeta de trabajo del asistente."),
    model: str | None = typer.Option(None, "--model", help="Modelo del asistente al abrirse."),
    new_copy: bool = typer.Option(
        False, "--new-copy", help="Crea expresamente otra copia y conserva los intentos anteriores."
    ),
) -> None:
    """Prepara la revisión vigente; abre el asistente solo desde una terminal."""
    _ensure_worker()
    options = {"branch": branch, "cwd": cwd, "model": model, "launch": _launch_allowed()}
    if new_copy:
        options["new_copy"] = True
    _emit(_service().open(link, target, **options))


@sync_app.command("select")
def select(
    link: str = typer.Argument(...),
    revision: str = typer.Argument(
        ..., help="Versión (o su inicio) o perfil del asistente donde continuaste, p. ej. claude-win."
    ),
) -> None:
    """Elige con qué continuación seguir; las demás se conservan."""
    _emit(_service().select(link, revision))


@sync_app.command("pause")
def pause(link: str = typer.Argument(...)) -> None:
    """Pausa la captura de este vínculo."""
    _emit(_service().pause(link))


@sync_app.command("resume")
def resume(
    link: str = typer.Argument(...),
    no_start: bool = typer.Option(False, "--no-start", help="Reanuda sin iniciar el trabajador."),
) -> None:
    """Reanuda la captura e inicia el trabajador si hace falta."""
    result = _service().unpause(link)
    if not no_start:
        result = {**result, "worker": _ensure_worker()}
    _emit(result)


@sync_app.command("unlink")
def unlink(link: str = typer.Argument(...)) -> None:
    """Desvincula sin borrar archivos ni sesiones nativas."""
    _emit(_service().unlink(link))


@sync_app.command("export")
def export(
    link: str = typer.Argument(...),
    output: Path = typer.Option(..., "--output", help="Paquete de salida nuevo."),
    branch: str | None = typer.Option(
        None, "--branch", help="Continuación a exportar si hay varias: versión o perfil."
    ),
) -> None:
    """Exporta una revisión a un paquete portable."""
    _emit(_service().export(link, output, branch=branch))


@sync_app.command("backup")
def backup(output: Path = typer.Option(..., "--output", help="Copia de seguridad nueva.")) -> None:
    """Crea una copia verificada del archivo de sincronización."""
    _emit(_service().backup(output))


@sync_app.command("restore")
def restore(
    backup: Path = typer.Argument(..., help="Copia del archivo de sincronización."),
    into: Path = typer.Option(..., "--into", help="Carpeta de datos nueva o vacía."),
) -> None:
    """Restaura el historial propio; no configura ni abre los asistentes."""
    from .sync_store import SyncStore

    restored = SyncStore.restore(backup, into)
    _emit(
        {
            "path": str(restored.path),
            "schema_version": restored.SCHEMA_VERSION,
            "history_only": True,
        }
    )


@sync_app.command("detach")
def detach(link: str = typer.Argument(...), key: str = typer.Argument(...)) -> None:
    """Deja de seguir un origen antiguo; conserva sus sesiones, ramas y archivos."""
    _emit(_service().detach(link, key))


@sync_app.command("start")
def start() -> None:
    """Inicia el trabajador local y verifica que responde."""
    _emit(_ensure_worker())


@sync_app.command("stop")
def stop() -> None:
    """Solicita la parada y espera su confirmación."""
    _emit(_daemon().stop())


@sync_app.command("worker-status")
def worker_status() -> None:
    """Consulta la identidad y la última señal del trabajador."""
    _emit(_daemon().status())


@sync_app.command("run")
def run(
    once: bool = typer.Option(False, "--once", help="Comprueba una vez y termina."),
    interval: float = typer.Option(2.0, "--interval", help="Intervalo en segundos, entre 0,1 y 3600."),
) -> None:
    """Ejecuta la captura en primer plano; Ctrl+C termina el trabajador."""
    _emit(_daemon().run(once=once, interval=interval))
