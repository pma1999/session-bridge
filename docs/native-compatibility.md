# Native compatibility evidence

Native history requires **all four** observations: durable storage after the importing
process exits; a native history surface that presents the imported user/assistant
messages; cold resume of the exact ID in the selected working directory; and both
markers in the next request captured by a local mock provider. A success exit status
or a synthetic JSONL that our own parser accepts does not establish those claims.

The gate in `writers/registry.py` is deliberately closed until the complete test
passes for an exact installed version. Unknown versions refuse native conversion.
Handoff is available only when explicitly selected by the caller.

| Destination | Version investigated | Native gate | Current evidence |
| --- | --- | --- | --- |
| Claude Code, Windows x64 | 2.1.304 | Enabled | All four gates passed: the probe recorded persistence, cold resume and continuation to a local mock; Unicode native TUI history was confirmed by inspection. 2.1.302 passed the same gates and is no longer the enabled version. |
| OpenCode, Windows x64 | 2.0.29 | Enabled, maximum 9,362 messages | All four gates passed, including native mini TUI and exact cold export. |
| Codex CLI, Windows x64 | 0.159.0 | Enabled, maximum 128 MiB rollout | All four gates passed with native legacy UI events and response items, including the native TUI. |
| Linux/WSL destination | Any | Disabled | Native writing has not been verified on this platform. Readers and helper are exercised; explicit handoff is available. |

Verified on 2026-10-19. Four cross-tool native directions also passed the actual Session Bridge
CLI with exact resume argv and unchanged source hashes. The Windows follow-up additionally verified
Claude/OpenCode to Codex, completing the six native directions. All six explicit handoff directions remain available.

OpenCode's measured boundary: 9,362 messages imported with exact cold readback in 5.495 s;
9,363 failed without creating a session (`SQLite query expected 5 values, received 65541`).
The 10,000-message native import failed in 2.464 s (`expected 4464 values, received 70000`).
The capability refuses above 9,362 before launch/write; it never truncates or splits history.
Export/full handoff remain available. Reproduce with `--capacity-counts 9362 9363`.

Codex accepted and persisted injected Unicode markers and included both in the next local mock
Responses request after cold resume. However, `thread/items/list` returned `data: []`,
`nextCursor: null` before and after restart; the paginated thread had no turns. This concrete
presentation failure ruled out injection as the destination strategy. The current native adapter
creates both UI events and model-visible response items, instead of shipping that incomplete path.

## Reproduction and isolation

`scripts/probe_native.py TOOL --client ABSOLUTE_EXECUTABLE` creates a fresh directory
strictly below `work/writers/`, never overwrites an existing directory, and stores a
`report.json` with the version, commands, gate outcomes and local mock requests.
It does not change the user's actual home/profile environment. Child process env
is built from a whitelist of Windows execution essentials; inherited credentials,
provider settings, hooks and plugins are excluded. HOME, USERPROFILE and app-data values
remain unchanged. XDG/client-specific roots and temporary paths use the run directory. No real profile,
credential or source session is copied. Mock providers bind only to `127.0.0.1` on
an OS-allocated port and use a fake local-only token. The script closes its own
server and the exact app-server process it created. It never kills global processes.

All client probes, including help/version inspection, require isolated client-specific roots. Claude
used dedicated CLAUDE_CONFIG_DIR and --bare; OpenCode used dedicated XDG roots, its exact-version
OPENCODE_TEST_HOME override and disabled project/default-plugin/model-fetch/update paths. Codex
used dedicated CODEX_HOME/CODEX_SQLITE_HOME, file credential stores, a local provider requiring
no OpenAI authentication, an empty project and a read-only sandbox. Host managed-config preflights
passed. Every server action/approval request was refused; the mock emitted text only. External
proxy traffic went to an unavailable loopback port. Real source sessions and credentials were
not used in the native compatibility probes.

## Destination contracts

### Claude Code

The private transcript format is not a published import API. The encoder creates a
fresh UUID, a deterministic new parent-UUID chain, text-only user/assistant records,
and an explicit imported provenance marker. No foreign system messages, permissions,
tool calls, thinking signatures, hooks or provider state are replayed. Model/usage
fields are synthetic compatibility metadata, not a claim that Claude generated the
imported answer. The title uses a native `custom-title` row. The JSONL is published
exclusively and atomically through the existing temp/fsync/hardlink helper.

The project directory encoder refuses names over 200 characters because the native
long-name hash has not been verified. An explicitly configured destination process
`CLAUDE_CODE_PROJECT_DIR_NAME` is accepted only as a bounded, separator-free name.
Resume uses the exact UUID and the caller launches it in the recorded cwd.

Official CLI source: [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference).
Installed `--help` confirms `--resume`, `--bare`, strict MCP configuration and
`--setting-sources`; `--bare` skips hooks, plugin sync, keychain reads and context
auto-discovery. The isolated probe uses an empty tool set and only the local mock.

### OpenCode

The 2.0.29 import schema is `{info, messages}` with `Session.Info` and
`SessionMessage.Info`, not the legacy `{info, messages:[{info,parts}]}` shape.
User entries use `type:user,text`. Assistant entries use `type:assistant,agent,
model,content,time.completed`; the import implementation filters unsettled
assistants out. Only newly constructed text messages and operation provenance
metadata are imported. Session permissions, foreign state, parent links, forks,
instructions and provider state are not copied.

Native import is through the installed CLI with `session import FILE --directory
DIR --standalone`. Readback/reconciliation do not launch OpenCode: they use a
read-only, query-only, WAL-aware SQLite transaction over allowlisted session-owned
tables. The fingerprint covers the selected session, every child recursively,
messages, pending/inbox rows and instruction rows. Credentials/config/account tables
are never queried. No native database write is performed by Session Bridge.

The official import bulk-inserts all settled messages in one statement. The probe
therefore includes 10,000 messages with a bounded timeout and cold export. A native
size/SQL-variable limit must be reported as an error; history must never be truncated
to make an import pass. Root resume uses positional cwd and exact `--session ID`;
there is no root `--model` flag. Existence is checked before launch because native
OpenCode may create a session for an unknown ID.

Versioned primary sources:

- [Transfer data schema](https://raw.githubusercontent.com/anomalyco/opencode/v2.0.29/packages/schema/src/session-transfer.ts)
- [Session schema](https://raw.githubusercontent.com/anomalyco/opencode/v2.0.29/packages/schema/src/session.ts)
- [Message schema](https://raw.githubusercontent.com/anomalyco/opencode/v2.0.29/packages/schema/src/session-message.ts)
- [Native import/export implementation](https://raw.githubusercontent.com/anomalyco/opencode/v2.0.29/packages/core/src/session/transfer.ts)
- [Session-owned SQL schema](https://raw.githubusercontent.com/anomalyco/opencode/v2.0.29/packages/core/src/session/sql.ts)

### Codex

Windows x64 version 0.159.0 now has four-gate native evidence. A newly allocated UUID is committed
in the bridge journal before exclusively publishing a new legacy JSONL rollout. Each selected
message has its original exact user/assistant text, a native UI event and a model-visible Responses
item, inside a closed synthetic historical turn. Initial assistant summaries, consecutive roles,
whitespace and literal `<user_query>` wrappers survive unchanged. No foreign turn context, active
permission, tool request, system instruction, encrypted signature or source model setting is copied.
The destination's current settings apply; an explicit CLI model selection is applied when resumed.

Native `thread/read` with full legacy turns independently verifies UI history against the complete
projection; `thread/name/set` assigns the new session's title. The bounded app-server connection
only permits initialize/read/name and declines all server action requests. Conversion starts no
turn and makes no inference. Native indexing is performed by Codex; Session Bridge never writes
its SQLite tables. The selected profile sets both CODEX_HOME and CODEX_SQLITE_HOME. The original
rollout operation marker supports exact-ID crash reconciliation. Publication never overwrites a
file, and a post-publication RPC error leaves the complete target available for recovery.

The explicit resource boundary is 128 MiB per native rollout/read response. An observed native
10,000-message conversion plus a second complete UI read passed in 10.309 s (7,943,342 rollout
bytes). This synthetic sample does not establish a maximum provider context capacity.

The native TUI showed both imported Unicode messages after cold resume. Folder trust was limited
to the isolated probe profile. Windows sandbox setup was not installed, and no new user message
was submitted through the TUI.
The owned managed daemon was stopped with the native `app-server daemon stop` command for that
same isolated home; no global process termination or regular-profile trust change was performed.

The alternative supported SESSIONS importer was also verified. It creates visible legacy history,
but drops leading assistant messages, normalizes some strings and allocates a random target before
persisting its source mapping. Those fidelity and crash-recovery gaps motivated the guarded
private-format adapter, matching the existing Claude strategy. Unknown versions remain disabled.

Sources: [app-server injection](https://learn.chatgpt.com/docs/app-server#inject-items-into-a-thread),
[native migration](https://learn.chatgpt.com/docs/app-server#detect-and-import-external-agent-config).
The installed generated schema matches official source tag rust-v0.159.0, commit
`687a119f0fcaace47e1f1abcc77cec6c813fd6da`. Relevant source is
`external-agent-migration/src/sessions/export.rs` and
`app-server/src/external_agent_migration/session_importer.rs`. Native runtime observations,
not the source's unit tests alone, establish the enabled gate.

## Undo boundary

Native delete APIs can cascade into child sessions. Current native undo always
refuses when inactivity, ownership of all descendants and an atomic deletion guard
cannot be established. Changed fingerprints are diagnosed separately. This preserves
new work, active sessions and foreign descendants; the error identifies the exact
native ID for manual cleanup from the native client. This is a deliberate supported
refusal, not a claim that every import can be automatically undone.

## Local verification

The initial 0.1.0 focused writer suite had 23 passing tests. Observed meaningful
red baselines: four ID/unknown-version assertions, then two regressions for atomic
publication interruption and unverified long project names. All six pass after the
changes. Ruff passes for the owned adapters, probe and tests. These local checks do
not substitute for the four native runtime gates above. The 0.2.0 Windows Codex follow-up passed
150 integrated tests (one Windows symlink-privilege skip), Ruff and Python compilation. Its
independent review finding was reproduced and fixed before delivery.
See [verification](verification.md) for the native runtime evidence and review limits.
