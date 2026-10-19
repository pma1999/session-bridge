# Architecture and safety boundaries

`models.py` is the versioned interchange contract. Read adapters are separate from context
projection, portable archives, destination adapters, process launching and the own SQLite journal.
The CLI consumes the service facade; the same facade works without an interactive terminal.

Read discovery returns metadata and locators only. Native content is captured on demand. SQLite
sources use mode=ro/query_only transactions with WAL visibility; only allowlisted session tables
are queried. Native databases are never copied wholesale or restored. Codex rollout archives and
paginated projection coexist; Claude parent/leaf graphs and OpenCode mixed legacy/v2 formats need
native semantics. Unknown relationships require an explicit context selection.

The preservation archive and active-context projection have independent lifecycles. Compaction
does not erase archival history or revive discarded context. Foreign system/developer instructions,
approvals, signatures and unfinished tool calls are never installed as current destination authority.
Historical tool data is inert text. Native destination settings and its current permissions apply.

The `.sbridge` format is a ZIP with `manifest.json`, `conversation.json` and content-addressed
`attachments/<sha256>` regular members. Version, exact member set, hashes, uncompressed size, ratio,
member count and attachment references are checked before extraction. Duplicate members/JSON keys,
links, absolute/drive/backslash/traversal paths and encrypted entries are rejected. Extraction never
uses foreign filenames or local paths from a package. SHA-256 detects corruption, not publisher
authenticity; packages have no executable hooks. Limits are explicit errors, never truncation.

The own journal uses schema version 1, unique operation identities and an OS lock released on crash.
Native writes proceed prepared → importing → verifying → completed. Errors enter recovery. Capture
hash, destination profile, context policy and new native ID stay bound to the operation. A native
exit code alone cannot complete an operation. Source capture files remain unchanged.

Windows Codex uses an exact-version legacy rollout encoder with distinct UI and model-context
records. Native app-server read/name operations hydrate/index/verify the new history without a
model turn. The transport has bounded responses and a method allowlist; server action requests
are declined. UUID allocation and exclusive publication retain the existing recovery contract.
The supported external-agent importer was evaluated but its random-ID publication gap and text
normalization would weaken those invariants. No bridge journal migration is required for 0.2.0.

Package publication uses a private temporary file, flush and exclusive hard-link publication on the
same filesystem. Existing outputs are never replaced. All deletions are limited to verified owned
objects; native cascade deletion is refused without a race-safe protocol. Future own-schema versions
must add backup/restore evidence before migration; unknown newer journal versions are blocked today.

The WSL transport is one bounded, validated JSON request/response over fixed argv and stdin/stdout.
There is no shell expression, SQL-over-UNC or use of session text as a command. Stopped distributions
are visible but not contacted until an explicit start/install. The helper is a separate rootless venv.

## Extending compatibility

Add a reader or native-version adapter, version-specific fixtures and isolated native probes. A
native capability requires durable history, actual visible history, cold exact-ID resume and captured
model-visible context. Keep unverified formats disabled. Test a new client release before enabling
its gate; do not widen version comparisons to a major/minor range based on similarity. A provider
mock establishes local client behavior, not an external service guarantee.

## Opt-in local synchronization (0.3.0)

`sync_capture.py` selects native completed checkpoints without treating file silence as completion.
`sync.py` owns the logical conversation graph, endpoint/profile identity, capture reconciliation and
on-demand immutable native materialization. The existing transfer service accepts a preplanned
operation ID so the sync intention precedes the native journal/publication boundary. Recovery checks
both journals rather than repeating a native effect. Source snapshots and canonical destination
messages remain distinct. Pending/invalid observations cannot overwrite the last verified head.

`sync_store.py` owns separate schema-1 `sync.sqlite3`; one-off `bridge.sqlite3` stays unchanged.
Immutable revisions refer to hashed capture/context manifests and compressed content-addressed
items. Chunked references share history prefixes, including order and legitimate duplicate text.
Limits and hashes are checked before exposing restored content; packed-blob length is bounded before
fetching/decompression. SQLite snapshots, immediate transactions and a process-owned operation lock
protect updates. A failed write cannot publish staged in-memory heads. Backups/restore validate
private files before exclusive publication and never restore a native database over later work.

`sync_daemon.py` serializes managed start/stop with a control lock, and uses an independent lifetime
OS lock plus generation token as worker identity. Receipts/heartbeats are diagnostics, not authority.
Stop is cooperative and exact-generation; neither stale PIDs nor foreign processes are killed.
The worker only captures linked Windows endpoints. It never materializes destinations or invokes a
provider. The interactive client releases the sync operation lock before blocking on native resume.
No login task, registry entry or WSL helper change is installed. CLI entry points and keyboard flow
reuse the existing Typer/Rich/prompt_toolkit facilities without additional dependencies.

Concurrent continuations preserve heads and require explicit selection. Missing/changed-profile
endpoints preserve their last revision; explicitly retiring a native endpoint does not delete its
session or archive. `sync open` verifies current source context and materialization identity before
reuse. Old native tabs/IDs are immutable snapshots, not mutable cross-client views. See the
[synchronization guide](synchronization.md) for operational and history-only backup limits.
