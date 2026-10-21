# Shared implementation contract

Python 3.11+, Pydantic 2. Domain contracts are in `models.py`; public errors use
`BridgeError(code, key=None, /, **content_free_details)`, where `key` names the text in the
`i18n` catalogs and defaults to `error.<code>`. JSON CLI envelope is
`{"schema_version":1,"ok":true,"data":...}` or `ok:false,error:{code,key,message,details}`.
`code` and `key` are stable; `message` is rendered in the language in use and may change.

Text people read lives only in `src/session_bridge/i18n/<lang>.py` and is referred to by key.
What is stored or exchanged (archives, receipts, sync data, capability reasons, warnings) holds
codes and keys, never rendered prose. Text the destination model reads (the handoff context,
projection and reader markers) is English and is not localized, because it is part of stored
identities.

## Read adapters

Each of `readers/claude.py`, `readers/opencode.py`, `readers/codex.py` exports:

* `discover(profile: Profile) -> list[SessionRef]` metadata only, no bodies
* `read(profile: Profile, ref: SessionRef, *, snapshot: bool=False) -> Conversation`

Native reader selects active branch/retained context using Event.active; last active summary
is included with following active events by the parent projection. Unknown relationship is
`context_ambiguous=True` and never guessed. Read entire selected history for archival, preserving
unknown fields. Ignore indices as sole source of truth. Read SQLite mode=ro, query_only, WAL-aware.
Do not read credential/account/config tables. Partial records produce warnings or safe errors.
An active source raises `source_active` unless snapshot=True. If activity unknown mark a warning.
Fingerprint canonical captured content, excluding capture timestamp; detect file changes while reading.

## Native writer facade

`writers/registry.py` exports:

* `capability(profile: Profile) -> Capability`
* `allocate_id(profile: Profile) -> str`
* `write(request: WriteRequest) -> NativeReceipt`
* `verify(profile: Profile, receipt: NativeReceipt, messages: list[ProjectedMessage]) -> NativeReceipt`
* `inspect(profile: Profile, native_id: str) -> NativeReceipt | None` reconciliation
* `undo(profile: Profile, receipt: NativeReceipt) -> None` own unchanged inactive subtree only
* `resume_argv(profile: Profile, native_id: str, cwd: str) -> list[str]`

Unknown/unverified version refuses native writing; handoff always possible only on explicit choice.
Use local mock providers and isolated native homes to establish gates. Never use live authentication,
real model inference, current user source session writes or direct native SQLite writes. Native ID
is allocated before writing. Error may leave target: never blindly retry. Receipt fingerprint should
cover full native subtree, source metadata and idle state; inspect/verify have no inference.
CLI resume must launch exact target without new prompt/permissions. Unknown native undo can refuse
with a useful manual cleanup diagnostic rather than risk deletion. No root --model on OpenCode.

## Service facade consumed by CLI

Parent implements `service.Bridge(data_dir: Path|None=None)`:

* `profiles() -> list[Profile]`, `register_profile(Profile) -> Profile`
* `list_sessions(tool=None, profile=None, search=None, cwd=None, archived=None,
  parent=None, since=None, until=None) -> list[SessionRef]`
* `show(key: str, snapshot=False) -> Conversation`
* `preview(key: str, target: str, *, cwd=None, context='current', context_from=None,
  snapshot=False, mode='native', model=None) -> dict`
* `convert(key, target, *, cwd=None, context='current', context_from=None,
  snapshot=False, mode='native', model=None, new_copy=False) -> Transfer`
* `export(key, output: Path, *, snapshot=False, include_files: list[Path]|None=None) -> dict`
* `import_package(package: Path, target: str, **convert_options) -> Transfer`
* `resume(key: str, *, launch=True) -> dict` (launch=False returns exact argv)
* `doctor() -> dict`, `undo(transfer_id: str) -> dict`
* `transfers() -> list[Transfer]`
* `preview_package(package: Path, target: str, **convert_options) -> dict`
* `recover(transfer_id: str) -> Transfer` reconciles only, never blindly repeats an import

Transfer includes immutable target Profile snapshot and context_policy for recovery after profile
registration changes. archive_sha256 binds the archived capture to the own journal. These are
operational metadata; journal stores no conversation bodies.

Noninteractive native fallback must be `--mode handoff`. `--dry-run` uses preview and never writes.
The CLI, readers, writers and service are separate modules. Adapter changes require version-specific
fixtures and native evidence in addition to service-level tests; see native-compatibility.md.

## Local Windows synchronization (0.3.0)

The CLI adds `sync link/list/status/check/open/select/pause/resume/unlink/detach/export/backup/restore`,
plus `sync start/stop/worker-status/run`. `convert --sync [--sync-name NAME]` links a completed native
transfer. `--dry-run` remains read-only and never starts a worker. Sync rejects WSL before source
discovery/read. JSON envelopes stay version 1; synchronization status is metadata, never bodies.

`SyncService` in `sync.py` accepts `data_dir` or a Bridge and exposes `link`, `list`, `status`, `poll`,
`open`, `select`, `pause`, `unpause`, `unlink`, `detach`, `lookup`, `export` and `backup`.
`open` accepts explicit branch/cwd/model, launch flag and deliberate new_copy; old native sessions
remain immutable. `lookup` redirects only exact linked native/transfer identities. Profile drift,
pending turns and ambiguous heads require explicit resolution/snapshot selection. Restore is a
separate `SyncStore.restore(backup, empty_root)` operation; it does not initialize Bridge or a worker.

`SyncStore` owns separate schema 1 with links, immutable revisions, captures, contexts and bounded
hashed blobs. Checkpoint projection and complete source capture have independent identities.
`Bridge._convert` can accept an internal preplanned operation_id; ordinary public convert identity
is unchanged. The sync intention and bridge operation journal bind retries to a single native
publication. A history-only backup excludes the bridge journal and refuses unfinished intentions.
The [synchronization guide](synchronization.md) defines command behavior and operational limits.

## WSL

Fixed JSON protocol v1, stdin/stdout only over wsl.exe --distribution NAME --exec PYTHON -m
session_bridge.helper. Ops discover/read/capability/write/verify/inspect/undo/resume_argv use same
Pydantic contracts. Validate allowlisted op, args and environment profiles; helper never executes
session text. The Windows service routes remote operations through `wsl.call(profile, op, data)`.
Windows profiles() includes detected stopped distros as diagnostic only, not auto-started profiles.
`wsl.install(distro, wheel_path)` is explicit helper bootstrap, rootless, dedicated venv; no shell
interpolation or agents installation. `wsl.status()` returns available distro name/running/version.
CLI `profile add` and `wsl install/status` use these functions. Linux CLI can use local profiles
in its own environment, with no wsl.exe requirement. Data home LOCALAPPDATA or XDG_DATA_HOME.

## Updates (0.8.0)

The updater always runs in the version being replaced, so what it relies on below is fixed: a
release that breaks it cannot be installed by the versions already out. `tests/test_updater.py`
pins these names.

- Release lookup: `GET https://api.github.com/repos/pma1999/session-bridge/releases/latest`
  (conditional with `If-None-Match`). Only a final `vX.Y.Z` tag whose page and assets live under
  `https://github.com/pma1999/session-bridge/releases/` is considered. Requests start only at
  `api.github.com` or `github.com` over HTTPS; a redirect may lead only to an HTTPS
  `*.githubusercontent.com` host.
- Release assets: `session-bridge-X.Y.Z-portable.zip`, whose members all lie under
  `session-bridge/`, and `SHA256SUMS` with a `<sha256>  <name>` line for that ZIP. The ZIP must match
  `SHA256SUMS`, and the API's `digest` of each asset when present. The ZIP's
  `session-bridge/pyproject.toml` must declare `X.Y.Z`.
- Handoff: `powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File
  <project>\scripts\install.ps1 -Python <base interpreter> -CommandDirectory <folder> [-Runtime <folder>]`,
  output in `<data>\logs\update-X.Y.Z.log`. The receipt
  `%LOCALAPPDATA%\SessionBridge\installation.json` (`project`, `runtime`, `command`, `sha256`)
  decides which installation is in use: the update succeeded when it names the new folder, whatever
  the exit code (a worker that fails to restart after the switch is only a warning, exit code 0).
  A receipt without `runtime` means `<project>\.venv`. Before changing anything, the updater checks
  under `update.lock` that the receipt still names the installation that offered the update; one
  updated from another window meanwhile gets `already_updated` and nothing is touched. The installer
  runs without a console, is not abandoned on Ctrl+C, and on its 15-minute timeout is stopped with
  everything it started; a command left renamed aside is then put back.
- Folders: release ZIPs go to `%LOCALAPPDATA%\SessionBridge\app\X.Y.Z`; Git clones get their new
  runtime in `%LOCALAPPDATA%\SessionBridge\runtime\X.Y.Z`. Each carries `.sessionbridge-managed`;
  only such folders are ever replaced or deleted, never the one the receipt names. Cleanup keeps
  the one in use and the one before it, and a folder that cannot be renamed (another window runs
  from it) is kept for a later start. A failed check waits an hour before the next automatic one.
- Git clones: updated only on `main` following `origin/main` of this repository, with no
  uncommitted changes and no commits of their own, by `git fetch` of the tag and
  `git merge --ff-only vX.Y.Z`; a failed installation resets the clone to where it was.
- State: `<data>\update.json` (last check, ETag, latest release, skipped version, postponement) and
  the `update_check` setting in `<data>\settings.json`. `SESSIONBRIDGE_NO_UPDATE_CHECK=1` disables
  the check at start. Only the start screen checks and asks; other commands never do.
- Error codes: `update_unavailable`, `update_integrity`, `update_failed`, `update_busy`,
  `update_not_installable`, `update_needs_yes`.
