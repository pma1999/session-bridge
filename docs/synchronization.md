# Local conversation synchronization

Session Bridge 0.3.0 adds opt-in synchronization across the verified Windows installations of
Claude Code, OpenCode and Codex. It follows conversations, not working-tree files or native windows.
The existing exact-version native compatibility gates still apply; see [native compatibility](native-compatibility.md).
WSL synchronization is disabled. No WSL distribution or helper is started or upgraded by this feature.

## Start and switch tools

Find exact profile/session IDs with `profile list` and `list`. Choose one of these entry points:

```text
sessionbridge convert claude-win:SESSION_ID --to opencode-win --sync --sync-name "My project"
sessionbridge sync link claude-win:SESSION_ID --name "My project"
sessionbridge sync link claude-win:SESSION_ID --name "My project" --transfer tr_VERIFIED_TRANSFER_ID
sessionbridge sync open "My project" --to codex-win
sessionbridge sync open "My project" --to opencode-win --cwd "C:\Projects\app"
```

The first command links the source and its verified conversion. `sync link` alone follows the named
source; `--transfer` additionally attaches an existing, owned, verified native conversion of that
source and its exact archived context. It never guesses that independently existing sessions belong
together. Source arguments must use the complete `PROFILE:SESSION_ID` identity.

Run `sessionbridge` in a terminal for searchable keyboard selection of linked conversations, target
tool and any required branch. In interactive text mode, opening launches the exact native session
without sending a message or granting permissions. JSON/noninteractive mode returns the exact
`argv` and environment instead of launching a client. `--model` selects a destination model only
when supported by its existing adapter; synchronization itself performs no inference.

`sync open` reconciles linked sources first. It reuses a verified destination already at that
revision, folder and model. Otherwise it creates a **new** native session with the current context,
archives its full source capture and records the mapping. An old destination is never rewritten.
`--new-copy` explicitly creates a further copy while retaining old mappings and uncertain attempts.
Use it deliberately, rather than as a blind response to an uncertain import.

The logical conversation name stays the same; native IDs can change. Use Session Bridge to open
the latest revision. A native tab already open on an older ID does not refresh or switch itself.
`sessionbridge resume` also follows a linked session when given its **exact qualified session ID**
or exact transfer ID. Abbreviated/ambiguous identifiers retain the ordinary one-off behavior.

## Capture and fidelity

The local worker reads only explicitly linked Windows sessions. Native source stores remain
read-only. It uses native completion evidence, not a period of filesystem silence, to publish a
checkpoint. A running/incomplete turn waits; the last verified context remains available as an
explicit snapshot. Changed files are reconciled on a two-second default interval, with a full
repair check at least every thirty seconds when polling is not busy. This is eventual capture,
not a guaranteed real-time latency bound.

Each revision contains the current retained context: original compaction/summary and following
active turns. Full available native captures, pending events, unknown fields and related subagents
are archived separately. A compaction or rewind creates a revision without erasing earlier archive
data or automatically reviving discarded messages. Repeated identical messages keep distinct event
identities. Re-reading an unchanged materialization does not create an echo revision.

Foreign tool calls/results become closed historical observations. Internal instructions, provider
signatures, reasoning and approvals stay archival where available; they do not acquire destination
authority. Unsupported attachments retain the existing fidelity report and archival behavior.
Native settings and permissions apply when you continue. No files, Git checkpoints, subprocesses,
approvals, credentials or running subagents are synchronized or recreated.

If a source is missing, malformed, changing while read, has an ambiguous branch, or its registered
installation changes, its last valid revision is retained and the problem is reported. It is not
silently replaced with an older or partial context. A native version upgrade still requires fresh
compatibility verification before writing destinations.

## Branches and status

```text
sessionbridge sync list
sessionbridge sync status "My project"
sessionbridge sync check "My project"
sessionbridge sync select "My project" rv_REVISION_ID
sessionbridge sync open "My project" --to claude-win --branch rv_REVISION_ID
```

Continuing two native copies from the same revision produces separate retained heads. There is no
automatic merge and no last-writer-wins overwrite. `sync select` chooses the head to follow without
deleting other branches. A new continuation from a stale copy again requires an explicit choice.
`--branch` selects an existing head for this open only; it also explicitly accepts opening that
verified snapshot while another source has a pending turn or an error. Status shows head IDs,
origin, reason, selected head and endpoint health without conversation bodies.

States are `synced`, `waiting`, `conflict`, `error`, `paused` and `unlinked`. `synced` means the
last observed completed histories have a selected or unique head; it does not claim every native
window has been updated. `waiting` means a first checkpoint or a native turn is pending.

```text
sessionbridge sync pause "My project"
sessionbridge sync resume "My project"
sessionbridge sync detach "My project" opencode-win:OLD_SESSION_ID
sessionbridge sync unlink "My project"
```

Pause is reversible and prevents capture/open until resumed. Detach retires one exact endpoint,
for example after deleting its old native session yourself; its branches and archives remain. The
last enabled endpoint cannot be detached: unlink the conversation instead. Unlink stops following
the whole conversation and retains its name/history for inspection and export. It is not deletion
or a synonym for pause. To create a separate link afterward, choose a new name.

## Worker lifecycle and installation

```text
sessionbridge sync start
sessionbridge sync worker-status
sessionbridge sync stop
sessionbridge sync run --once
sessionbridge sync run --interval 2
```

Linking, resuming capture or opening starts one managed, hidden worker per Session Bridge data
directory. It survives its launching CLI. `link --no-start` and `sync resume --no-start` defer that
start. The lifetime OS lock and generation token establish ownership; PIDs and old receipts alone
do not. Stop requests the exact generation and waits for acknowledgement. It never kills a PID.
After a crash, the OS releases the lock and a later start can reconcile from stored state.

No login task, startup registry entry or client hook is installed. After a reboot, run Session
Bridge or `sync start`; changes made while stopped are then reconciled. Foreground `run` supports
Ctrl+C and a bounded 0.1–3600 second interval. Poll failures store content-free codes, not exception
text or conversation bodies. A busy/unresponsive worker is diagnosed rather than forcibly killed.

The Windows installer checks an existing managed command before replacing files, stops a running
managed worker cooperatively, installs and verifies the runtime, then starts a new generation only
if the worker was running beforehand. Stop failure blocks code replacement. A failed update attempts
to restart the installed runtime and reports failure; repair it before `sync start` if necessary.
It does not change global environment variables, native client settings or WSL installations.

## Recovery, export and backups

```text
sessionbridge transfers --json
sessionbridge recover tr_TRANSFER_ID
sessionbridge sync open "My project" --to codex-win
sessionbridge sync export "My project" --output conversation.sbridge
sessionbridge sync backup --output sync-history.sqlite3
sessionbridge sync restore sync-history.sqlite3 --into "D:\SessionBridge-Restored"
```

A native materialization intention and operation ID are persisted before publication. Retries
reconcile that operation and its exact allocated native ID through the existing transfer journal;
an exit code alone never proves completion. If the target was continued during recovery, the new
revision is preserved and diagnosed before opening. An uncertain operation must be recovered and
reopened before backing up; backups refuse pending intentions. An explicit further copy preserves,
rather than deletes, any earlier uncertain attempt.

`sync export` produces the existing integrity-checked `.sbridge` format for a selected head, including
its canonical context and complete available capture. `--branch` resolves multiple heads explicitly.
Ordinary `import` can turn that package into a new one-off native session in another installation.

`sync backup` uses SQLite's consistent backup API and validates the private temporary output before
exclusive publication; it never replaces an existing output. The backup contains the sync graph,
all captured history, contexts, blobs and mappings. It contains private conversation data. The
returned SHA-256 detects corruption; it does not authenticate the backup's author.

This is a **history-only backup**. It does not include `bridge.sqlite3`, registered profiles, transfer
journals/archives outside the sync store, native client sessions, credentials or project files.
Restore requires a new or empty directory, refuses links/nonempty targets and never overwrites an
existing store. It does not change the active installation or start a worker. To inspect the restored
archive, set `SESSIONBRIDGE_HOME` to that directory for the command process. Original endpoint
installations must still match for automatic capture. If they are unavailable, inspect heads and
export a selected head, or explicitly `sync open ... --branch REV --to LOCAL_PROFILE --cwd PATH`.
A retained native mapping without the old transfer journal is reused only when its exact operation
marker and current context can be verified. Different-machine paths/profile identities are not
silently rebound.

`sync unlink` and `sync detach` never remove native sessions. Ordinary `undo` retains its existing
strict checks and may refuse native deletion if inactivity/descendant safety cannot be established.
Retire a removed endpoint explicitly before expecting error-free synchronization to continue.

## Storage and extension contracts

`sync.sqlite3` schema 1 is separate from the unchanged one-off journal. Immutable revisions form a
parent graph. Capture events, raw records, attachments and canonical messages are SHA-256-addressed
compressed blobs; chunked references reuse long history prefixes. Integrity, related-depth and size
limits are enforced on both write and read. A single native item is limited to 64 MiB and the aggregate
capture/context budget to 256 MiB. Oversized data fails explicitly; nothing is silently truncated.

All synchronization commands preserve the CLI's JSON envelope version 1. `status`/`list` are metadata
only; `check` returns changed revision count, link statuses and content-free errors. `open` additionally
returns link/revision/transfer/native IDs, exact native launch data, `launched` and whether an explicit
snapshot was chosen. `restore` returns the restored path, own schema version and `history_only: true`.
Source/target profiles are pinned, and unknown native versions do not inherit compatibility by name.
There is no network synchronization server, remote publication or automatic cross-PC linking.
