"""Commands for explicitly linked local conversations."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from . import i18n
from .i18n import typer_chrome

typer_chrome.install()

sync_app = typer.Typer(no_args_is_help=True, help="help.sync")


def _service():
    from .sync import SyncService

    return SyncService()


def _no_links() -> str:
    # Names the interactive option with the same words the menu shows.
    return i18n.t("sync.no_links", option=i18n.t("menu.convert_sync"))


def _emit(value, **options) -> None:
    from .cli import _emit as emit

    emit(value, **options)


def _emit_worker(value) -> None:
    from .cli import _emit_worker as emit

    emit(value)


def _daemon():
    from . import sync_daemon

    return sync_daemon


def _ensure_worker(data_dir: Path | None = None) -> dict:
    """Shared by link, resume and the converter's opt-in sync."""
    return _daemon().start(data_dir)


def _launch_allowed() -> bool:
    from .cli import _state

    return not _state["json"] and sys.stdin.isatty() and sys.stdout.isatty()


@sync_app.command("link", help="help.cmd.sync.link")
def link(
    key: str = typer.Argument(..., help="help.sync.link.key"),
    name: str | None = typer.Option(None, "--name", help="help.sync.link.name"),
    transfer: str | None = typer.Option(None, "--transfer", help="help.sync.link.transfer"),
    no_start: bool = typer.Option(False, "--no-start", help="help.sync.link.no_start"),
) -> None:
    "Link a session and turn on background sync."
    result = _service().link(key, name=name, transfer_id=transfer)
    if not no_start:
        result = {**result, "worker": _ensure_worker()}
    _emit(result)


@sync_app.command("list", help="help.cmd.sync.list")
def list_links() -> None:
    "Show the links and their state."
    _emit(_service().list(), empty=_no_links())


@sync_app.command("status", help="help.cmd.sync.status")
def link_status(link: str | None = typer.Argument(None, help="help.sync.arg.link")) -> None:
    "Show one link, or all of them if you give none."
    service = _service()
    _emit(service.status(link) if link is not None else service.list(), empty=_no_links())


@sync_app.command("check", help="help.cmd.sync.check")
def check(link: str | None = typer.Argument(None, help="help.sync.check.link")) -> None:
    "Capture new finished turns without opening any assistant."
    _emit(_service().poll(link))


@sync_app.command("open", help="help.cmd.sync.open")
def open_link(
    link: str = typer.Argument(...),
    target: str = typer.Option(..., "--to", help="help.sync.open.target"),
    branch: str | None = typer.Option(None, "--branch", help="help.sync.open.branch"),
    cwd: str | None = typer.Option(None, "--cwd", help="help.sync.open.cwd"),
    model: str | None = typer.Option(None, "--model", help="help.sync.open.model"),
    new_copy: bool = typer.Option(False, "--new-copy", help="help.sync.open.new_copy"),
) -> None:
    "Prepare the current revision; the assistant opens only from a terminal."
    _ensure_worker()
    options = {"branch": branch, "cwd": cwd, "model": model, "launch": _launch_allowed()}
    if new_copy:
        options["new_copy"] = True
    _emit(_service().open(link, target, **options))


@sync_app.command("select", help="help.cmd.sync.select")
def select(
    link: str = typer.Argument(...),
    revision: str = typer.Argument(..., help="help.sync.select.choice"),
) -> None:
    "Choose which continuation to follow; the others are kept."
    _emit(_service().select(link, revision))


@sync_app.command("pause", help="help.cmd.sync.pause")
def pause(link: str = typer.Argument(...)) -> None:
    "Pause capturing this link."
    _emit(_service().pause(link))


@sync_app.command("resume", help="help.cmd.sync.resume")
def resume(
    link: str = typer.Argument(...),
    no_start: bool = typer.Option(False, "--no-start", help="help.sync.resume.no_start"),
) -> None:
    "Resume capturing and start the worker if needed."
    result = _service().unpause(link)
    if not no_start:
        result = {**result, "worker": _ensure_worker()}
    _emit(result)


@sync_app.command("unlink", help="help.cmd.sync.unlink")
def unlink(link: str = typer.Argument(...)) -> None:
    "Unlink without deleting files or native sessions."
    _emit(_service().unlink(link))


@sync_app.command("export", help="help.cmd.sync.export")
def export(
    link: str = typer.Argument(...),
    output: Path = typer.Option(..., "--output", help="help.sync.export.output"),
    branch: str | None = typer.Option(None, "--branch", help="help.sync.export.branch"),
) -> None:
    "Export a revision to a portable package."
    _emit(_service().export(link, output, branch=branch))


@sync_app.command("backup", help="help.cmd.sync.backup")
def backup(output: Path = typer.Option(..., "--output", help="help.sync.backup.output")) -> None:
    "Create a verified backup of the sync store."
    _emit(_service().backup(output))


@sync_app.command("restore", help="help.cmd.sync.restore")
def restore(
    backup: Path = typer.Argument(..., help="help.sync.restore.backup"),
    into: Path = typer.Option(..., "--into", help="help.sync.restore.data_dir"),
) -> None:
    "Restore Session Bridge's own history; it does not configure or open the assistants."
    from .sync_store import SyncStore

    restored = SyncStore.restore(backup, into)
    _emit(
        {
            "path": str(restored.path),
            "schema_version": restored.SCHEMA_VERSION,
            "history_only": True,
        }
    )


@sync_app.command("detach", help="help.cmd.sync.detach")
def detach(link: str = typer.Argument(...), key: str = typer.Argument(...)) -> None:
    "Stop following an old source; its sessions, branches and files are kept."
    _emit(_service().detach(link, key))


@sync_app.command("start", help="help.cmd.sync.start")
def start() -> None:
    "Start the local worker and check that it responds."
    _emit_worker(_ensure_worker())


@sync_app.command("stop", help="help.cmd.sync.stop")
def stop() -> None:
    "Ask the worker to stop and wait for it to confirm."
    _emit_worker(_daemon().stop())


@sync_app.command("worker-status", help="help.cmd.sync.worker_status")
def worker_status() -> None:
    "Show the worker's identity and its last heartbeat."
    _emit_worker(_daemon().status())


@sync_app.command("run", help="help.cmd.sync.run")
def run(
    once: bool = typer.Option(False, "--once", help="help.sync.run.once"),
    interval: float = typer.Option(2.0, "--interval", help="help.sync.run.interval"),
) -> None:
    "Run capture in the foreground; Ctrl+C stops the worker."
    _emit(_daemon().run(once=once, interval=interval))
