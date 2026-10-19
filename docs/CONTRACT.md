# Shared implementation contract

Python 3.11+, Pydantic 2. Domain contracts are in `models.py`; public errors use
`BridgeError(code, Spanish message, **content_free_details)`. JSON CLI envelope is
`{"schema_version":1,"ok":true,"data":...}` or `ok:false,error:{code,message,details}`.

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
