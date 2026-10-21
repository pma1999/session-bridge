"""What the user sees of updates: the offer at start, the progress while installing, the reopening.

The check runs in the background while the start screen loads its conversations, so looking for
an update adds no noticeable wait; if GitHub is slow, the last known answer is used instead.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import i18n, installation, ui, updates
from .errors import BridgeError
from .installation import Installation

STARTUP_WAIT = 2.0  # Longest the start screen waits for GitHub, counted from launch.
STEPS = {
    "download": "update.step.download",
    "git": "update.step.git",
    "verify": "update.step.verify",
    "install": "update.step.install",
}


@dataclass(frozen=True)
class Relaunch:
    """The start screen hands this back when the new version should open in place of this one."""

    command: Path
    version: str


class StartupCheck:
    """Look for an update in the background; never raises, never blocks longer than asked."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.installation: Installation | None = None
        self._detected = threading.Event()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name="update-check", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        from . import updater

        try:
            self.installation = installation.detect()
            self._detected.set()
            updater.prune(self.installation)
            if updates.due():
                try:
                    updates.refresh()
                except BridgeError:
                    # Offline or GitHub unavailable: wait an hour instead of retrying at every start.
                    _remember(updates.failed)
        except Exception:  # noqa: BLE001 - an update check must never get in the way of starting
            pass
        finally:
            self._done.set()

    def result(self) -> updates.Offer | None:
        """The offer to show now, from this check if it finished in time or else from the last one."""
        self._done.wait(max(0.0, STARTUP_WAIT - (time.monotonic() - self.started)))
        if not self._detected.is_set() or self.installation is None:
            return None  # Not even known how this copy was installed: ask again next time.
        try:
            return updates.pending(self.installation)
        except Exception:  # noqa: BLE001
            return None


def start_check() -> StartupCheck | None:
    return StartupCheck() if updates.startup_check() else None


def at_start(check: StartupCheck | None, notices: list[str]) -> Relaunch | None:
    """Offer the update, if any; a notice line is added when this installation cannot update itself."""
    found = updates.updated_from()
    # Read once: programs this run starts, such as the sync worker, must not inherit it.
    os.environ.pop(updates.UPDATED_FROM_VARIABLE, None)
    if found is not None:
        notices.append(i18n.t("update.updated_from", version=str(found)))
    offer = check.result() if check is not None else None
    if offer is None:
        return None
    if not offer.installation.updatable:
        notices.append(notice(offer))
        _remember(updates.noticed)
        return None
    return _offer(offer)


def _remember(record, *args) -> None:
    """Save the user's answer; a data folder that cannot be written must not stop the start screen."""
    try:
        record(*args)
    except OSError:
        pass


def notice(offer: updates.Offer) -> str:
    """One line saying a newer version exists and why it is not offered for installation."""
    latest, place = str(offer.latest), offer.installation
    if place.kind == installation.CHECKOUT and place.blocker:
        reason = BridgeError("update_not_installable", f"error.update_not_installable.{place.blocker}").message
        return i18n.t("update.notice.blocked", latest=latest, reason=reason)
    return i18n.t("update.notice.manual", latest=latest, url=ui.visible(offer.release.page_url))


def describe(offer: updates.Offer) -> str:
    lines = [i18n.t("update.available", latest=str(offer.latest), current=str(offer.current))]
    place = offer.installation
    if place.kind == installation.CHECKOUT:
        lines.append(i18n.t("update.checkout_note", folder=ui.visible(str(place.project)), tag=offer.latest.tag))
    lines.append(i18n.t("update.notes", url=ui.visible(offer.release.page_url)))
    return "\n".join(lines)


def _offer(offer: updates.Offer) -> Relaunch | None:
    from .interactive import BACK, _ask, _note

    answer = _ask(
        describe(offer),
        [
            ("now", i18n.t("update.choice.now")),
            ("later", i18n.t("update.choice.later")),
            ("skip", i18n.t("update.choice.skip")),
        ],
        default="now",
    )
    if answer == "skip":
        _remember(updates.skip, offer.latest)
        return None
    if answer in ("later", BACK, None):
        _remember(updates.snooze)
        return None
    try:
        command = install(offer)
    except BridgeError as error:
        from .cli import _error_line

        _note(_error_line(error), "bold")
        _note(i18n.t("update.failed_continue", version=str(offer.current)), "dim")
        return None
    _note(i18n.t("update.done_opening", version=str(offer.latest)), "bold")
    return Relaunch(command, str(offer.current))


def install(offer: updates.Offer, *, quiet: bool = False) -> Path:
    """Install with a spinner naming each step, unless ``quiet``; returns the new command."""
    import contextlib

    from . import updater
    from .interactive import _status

    spinner = contextlib.nullcontext() if quiet else _status(i18n.t("update.step.verify"))
    with spinner as status:

        def progress(step: str, **values) -> None:
            if status is not None and hasattr(status, "update") and step in STEPS:
                status.update(i18n.t(STEPS[step], **values))

        return updater.apply(offer, progress)


def relaunch(target: Relaunch, args: list[str]) -> int:
    """Run the new version with the same arguments in this terminal, and return its exit code."""
    env = {**os.environ, updates.UPDATED_FROM_VARIABLE: target.version}
    process = subprocess.Popen([str(target.command), *args], env=env)
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            # Ctrl+C reaches both processes; the new version handles it, this one keeps waiting.
            continue
