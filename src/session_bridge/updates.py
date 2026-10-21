"""Whether a newer Session Bridge is published, and what the user decided about it.

GitHub is asked at most once a day; the answer, the user's "not now" and "skip this version" are
kept in ``update.json`` in the data folder. Nothing here prompts or installs anything.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__, jsonfile, releases, settings
from .installation import Installation
from .releases import Release, Version

CHECK_INTERVAL = 24 * 60 * 60
RETRY_INTERVAL = 60 * 60  # After a failed check (offline, GitHub unavailable).
SNOOZE = 24 * 60 * 60
NOTICE_INTERVAL = 24 * 60 * 60
SETTING = "update_check"
DISABLE_VARIABLE = "SESSIONBRIDGE_NO_UPDATE_CHECK"
UPDATED_FROM_VARIABLE = "SESSIONBRIDGE_UPDATED_FROM"


@dataclass(frozen=True)
class Offer:
    current: Version
    release: Release
    installation: Installation

    @property
    def latest(self) -> Version:
        return self.release.version


def current() -> Version:
    version = Version.parse(__version__)
    if version is None:  # The package version is always X.Y.Z; a test guards it.
        raise ValueError(__version__)
    return version


def state_path() -> Path:
    from .profiles import data_home

    return data_home() / "update.json"


def _state() -> dict:
    return jsonfile.read(state_path())


def _save(**changes) -> None:
    state = _state()
    for key, value in changes.items():
        if value is None:
            state.pop(key, None)
        else:
            state[key] = value
    jsonfile.write(state_path(), state)


def startup_check() -> bool:
    """Whether starting Session Bridge looks for updates: on unless the user or the environment says no."""
    if os.environ.get(DISABLE_VARIABLE, "").strip() not in ("", "0"):
        return False
    return settings.load().get(SETTING) is not False


def set_startup_check(enabled: bool) -> Path:
    # On is the default, so only "off" is stored.
    return settings.save(SETTING, None if enabled else False)


def _time(value) -> float | None:
    """A stored moment, or None when it was never stored (or is not a number)."""
    return float(value) if type(value) in (int, float) else None


def _within(value, now: float, interval: float) -> bool:
    moment = _time(value)
    return moment is not None and 0 <= now - moment < interval


def due(now: float | None = None) -> bool:
    now = time.time() if now is None else now
    state = _state()
    if _within(state.get("failed_at"), now, RETRY_INTERVAL):
        return False
    return not _within(state.get("checked_at"), now, CHECK_INTERVAL)


def failed(*, now: float | None = None) -> None:
    """A check could not reach GitHub; the next automatic one waits ``RETRY_INTERVAL``."""
    _save(failed_at=time.time() if now is None else now)


def refresh(*, now: float | None = None) -> Release | None:
    """Ask GitHub now and remember the answer. Raises BridgeError when GitHub cannot be asked."""
    state = _state()
    cached = Release.from_dict(state.get("release"))
    lookup = releases.latest(state.get("etag") if cached is not None else None)
    release = lookup.release if lookup.changed else cached
    _save(
        failed_at=None,
        checked_at=time.time() if now is None else now,
        etag=lookup.etag if release is not None else None,
        release=release.as_dict() if release is not None else None,
    )
    return release


def known() -> Release | None:
    """The latest release from the last successful check, without asking GitHub."""
    return Release.from_dict(_state().get("release"))


def offer(installation: Installation, release: Release | None = None) -> Offer | None:
    """The newer release, if there is one, whatever the user decided about it before."""
    release = release or known()
    if release is None or release.version <= current():
        return None
    return Offer(current(), release, installation)


def pending(installation: Installation, *, now: float | None = None) -> Offer | None:
    """The offer to show at start: newer, not skipped and not postponed."""
    found = offer(installation)
    if found is None:
        return None
    state = _state()
    now = time.time() if now is None else now
    if state.get("skipped") == str(found.latest):
        return None
    if now < (_time(state.get("snoozed_until")) or 0):
        return None
    if not installation.updatable and _within(state.get("noticed_at"), now, NOTICE_INTERVAL):
        return None
    return found


def snooze(*, now: float | None = None) -> None:
    _save(snoozed_until=(time.time() if now is None else now) + SNOOZE)


def skip(version: Version) -> None:
    _save(skipped=str(version), snoozed_until=None)


def noticed(*, now: float | None = None) -> None:
    """A notice was shown for an installation that cannot update itself; show it again tomorrow."""
    _save(noticed_at=time.time() if now is None else now)


def updated_from() -> Version | None:
    """The version this run replaced, when it was opened right after updating."""
    return Version.parse(os.environ.get(UPDATED_FROM_VARIABLE))
