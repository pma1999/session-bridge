# Synchronization

Synchronization keeps one conversation available in every assistant you use. Link a conversation once, and Session Bridge follows its finished turns in the background; whenever you switch assistant, the session you open carries everything said so far, wherever it was said.

It is **opt-in**, **Windows only** and **local**: nothing is followed until you link a conversation, and nothing leaves your computer.

**Contents**

- [What it does and does not do](#what-it-does-and-does-not-do)
- [Linking a conversation](#linking-a-conversation)
- [Switching assistants](#switching-assistants)
- [When you continue in two places](#when-you-continue-in-two-places)
- [Status at a glance](#status-at-a-glance)
- [Pausing, detaching and unlinking](#pausing-detaching-and-unlinking)
- [The background worker](#the-background-worker)
- [Exports, backups and restores](#exports-backups-and-restores)
- [When something goes wrong](#when-something-goes-wrong)
- [How capture works](#how-capture-works)
- [Storage and JSON output](#storage-and-json-output)

## What it does and does not do

**It does**

- follow the conversations you link, in the [verified Windows versions](native-compatibility.md) of Claude Code, Codex and OpenCode;
- record each finished turn as a new revision, keeping every earlier one;
- open the conversation in any of those assistants with the latest turns;
- keep both continuations, and ask you, when you continued in two places.

**It does not**

- synchronize files, Git state, credentials, permissions, approvals, running processes or running subagents;
- change an existing native session: switching creates a new one when needed (see [why](#why-a-new-session));
- refresh a tab you already have open in an assistant;
- run on Linux or WSL, start a WSL distribution, or update the WSL helper;
- run a model, contact a server, or link conversations across computers.

## Linking a conversation

On the start screen, choose a conversation, then the assistant to continue in, and pick **Convert and keep it synchronized**. Or, from the command line:

```powershell
sessionbridge convert claude-win:SESSION_ID --to opencode-win --sync --sync-name "Checkout redesign"
sessionbridge sync link claude-win:SESSION_ID --name "Checkout redesign"
sessionbridge sync link claude-win:SESSION_ID --name "Checkout redesign" --transfer tr_TRANSFER_ID
```

- `convert --sync` converts the conversation and links the source with the new session.
- `sync link` starts following a session on its own.
- `sync link --transfer` also attaches a verified conversion you made earlier, with its archived context.

The source must be the full `PROFILE:SESSION_ID`; `sessionbridge list` and `profile list` show them. Session Bridge never assumes two sessions you created separately belong together.

Linking starts the [background worker](#the-background-worker). Add `--no-start` to start it later.

A synchronized conversation follows the version of the source its assistant would resume. If you edit an earlier message there, the next revision carries the edited history. That is why a conversion made with another version (`--variant`, or **Choose another version**) cannot be kept synchronized: `--sync` cannot be combined with `--variant`, and `sync link` refuses such a conversion with `sync_variant_unsupported`.

## Switching assistants

Open a linked conversation from the start screen (marked **●**), or with:

```powershell
sessionbridge sync open "Checkout redesign" --to codex-win
sessionbridge sync open "Checkout redesign" --to opencode-win --cwd "C:\Projects\acme-shop"
```

Session Bridge first collects any new finished turns. Then each assistant has **one current session** for the conversation, the one Session Bridge opened or created for it last:

- **If that session is already up to date**, in the same folder and with the same model, it is reopened as it is. This includes your original session: going back to the assistant you started in, with no new turns elsewhere, reopens it.
- **Otherwise a new native session is created** with the current context. Its full source is archived, and it becomes that assistant's current session.
- **The previous session stays exactly as it was** and becomes an *earlier session*. It is still watched: if you type in it, for example in a tab you left open, that work is captured as a continuation of its own and you are asked which one to follow.

Before opening, the start screen says which of the two will happen. Opening starts the assistant on that exact session without sending a message or granting any permission. With `--json`, or without a terminal, it returns the exact command line and environment instead of starting the assistant. `--model` picks a model in the target when its assistant supports it. `--new-copy` deliberately creates one more copy, keeping every earlier one.

The conversation's name stays the same while native session IDs change, so always switch through Session Bridge. `sessionbridge resume` also follows a linked conversation when you give it the exact full session ID or transfer ID of one of its sessions; an abbreviated ID resumes that session as it is.

### Why a new session

None of the supported assistants offers a way to add turns to an existing session, and none lets another program tell whether a session is open in a running client: the desktop apps serve every conversation from one process. Writing into a session that a client has open would leave history its model never saw, or split its record chain, silently. An open tab would not show the new turns either, because clients do not reload a loaded session from disk. Session Bridge therefore never rewrites native sessions; the verified compatibility covers new sessions.

## When you continue in two places

If two sessions continue from the same revision, for example you typed in Claude Code and in Codex without switching through Session Bridge, both are kept as separate **continuations**. Nothing is merged and nothing is overwritten.

The start screen shows each one: where it was typed (an assistant, its original session or an earlier copy), how long ago, and its last request. It remembers your choice. The other continuations are kept, and new turns in one you did not choose ask again.

From the command line:

```powershell
sessionbridge sync status "Checkout redesign"
sessionbridge sync select "Checkout redesign" claude-win
sessionbridge sync select "Checkout redesign" rv_REVISION_ID
sessionbridge sync open "Checkout redesign" --to codex-win --branch rv_REVISION_ID
```

`sync select` takes a revision ID prefix, or the profile of the assistant you continued in when that assistant has one continuation. `--branch` takes the same values for one open only. It also explicitly accepts opening that verified snapshot while another source has a turn in progress or an error.

## Status at a glance

```powershell
sessionbridge sync list
sessionbridge sync status "Checkout redesign"
sessionbridge sync check "Checkout redesign"
```

`sync list` shows every linked conversation. `sync status` shows each assistant's current session and how many earlier ones it has, the continuations with their origin, the selected one, and the health of each source, without any conversation text. `sync check` collects new turns now.

| State | Means |
| --- | --- |
| `synced` | The last finished turns everywhere lead to one selected or unique continuation. It does not mean every open tab shows them. |
| `waiting` | The first turn, or a turn in progress, has not finished yet. |
| `conflict` | There are several continuations and none is selected. |
| `error` | A source could not be read. Its last good revision is kept. |
| `paused` | You paused it. |
| `unlinked` | You stopped following it. Its history is kept. |

## Pausing, detaching and unlinking

```powershell
sessionbridge sync pause "Checkout redesign"
sessionbridge sync resume "Checkout redesign"
sessionbridge sync detach "Checkout redesign" opencode-win:OLD_SESSION_ID
sessionbridge sync unlink "Checkout redesign"
```

- **Pause** stops collecting and opening until you **resume**. Nothing is lost. `sync resume --no-start` resumes without starting the worker.
- **Detach** stops following one session, for example after you deleted it in its assistant. Its revisions and archives stay. The last source cannot be detached; unlink the conversation instead.
- **Unlink** stops following the whole conversation. Its name and history stay available to `status`, `export` and `backup`. It is not a deletion and not a pause; to link the conversation again, choose a new name.

None of these deletes a native session.

## The background worker

A small hidden worker collects new turns. Linking, resuming or opening a conversation starts it; there is one per data folder, and it keeps running after the command that started it ends.

```powershell
sessionbridge sync worker-status
sessionbridge sync start
sessionbridge sync stop
sessionbridge sync run --once
sessionbridge sync run --interval 2
```

- It only reads the sessions you linked. It never writes native sessions or runs a model.
- It is not started with Windows: no login task, startup entry or client hook is installed. After a restart, open Session Bridge or run `sync start`; anything said in the meantime is collected then.
- `stop` asks the worker to stop and waits for it to confirm. A process is never killed by its PID; a busy or unresponsive worker is diagnosed instead.
- `run` runs it in the foreground: `--once` for a single pass, or `--interval SECONDS` (0.1 to 3600) until Ctrl+C.
- Errors are stored as codes, never as conversation text.

The Windows installer stops a running worker before replacing its code and starts it again on the new version; if the worker cannot be stopped, the code is not replaced. An update installed in a new folder stops the worker through the installation it replaces.

## Exports, backups and restores

```powershell
sessionbridge sync export "Checkout redesign" --output checkout.sbridge
sessionbridge sync backup --output sync-history.sqlite3
sessionbridge sync restore sync-history.sqlite3 --into "D:\SessionBridge-Restored"
```

**Export** writes one continuation of a conversation to a `.sbridge` archive, with its context and its complete capture. Choose the continuation with `--branch` when there are several. `sessionbridge import` turns the archive into a new one-off session anywhere.

**Backup** copies the whole synchronization store, with every conversation, revision and mapping, using SQLite's consistent backup. It never replaces an existing file, and refuses while a session is still being written: finish or recover it first. The checksum it prints detects a damaged backup; it does not prove who made it.

A backup is **history only**. It does not include the transfer journal (`bridge.sqlite3`), your profiles, transfer archives outside the synchronization store, native sessions, credentials or project files.

**Restore** needs a new or empty folder, never overwrites anything, and does not touch the installation in use or start a worker. To look at a restored store, set `SESSIONBRIDGE_HOME` to that folder for the commands you run. Automatic collection needs the original assistant installations; without them, inspect and export the continuations, or open one explicitly with `sync open … --branch REV --to PROFILE --cwd PATH`. Paths and profiles from another computer are never silently rebound.

Backups and restored stores contain private conversation history.

## When something goes wrong

A source that is missing, damaged, changing while it is read, has an ambiguous branch, or whose installation changed, keeps its last good revision, and the problem is reported in `sync status`. Session Bridge never replaces it with an older or partial context.

| Code | What to do |
| --- | --- |
| `sync_not_current` | A source has a turn in progress or an error. Wait for the turn, or open a known revision with `--branch`. |
| `sync_conflict` | There are several continuations. Choose one with `sync select`, or `--branch` for one open. |
| `sync_paused` | Run `sync resume`. |
| `sync_profile_changed` | The assistant's installation changed since linking. A new version needs verification before native writing; detach the old session if you no longer use it. |
| `sync_target_advanced` | The target was continued while a session was being recovered. The new revision is kept; check `sync status` and open again. |
| `sync_recovery_required` | A session was not fully written. Run `sessionbridge transfers`, then `recover`, and open again. |
| `sync_worker_unresponsive` | Run `sync worker-status`. A busy worker is left alone. |
| `sync_variant_unsupported` | The conversion used another version of the conversation. Convert again without `--variant` to synchronize. |
| `sync_windows_only` | Synchronization works with Windows profiles only. |

Writing a new session is recorded before it happens, so a retry continues the same operation and never creates a second session by accident. An interrupted one must be recovered before opening again or making a backup. `--new-copy` keeps, rather than deletes, an earlier uncertain attempt.

## How capture works

The worker uses each assistant's own record that a turn finished, not a period of silence in its files. A turn in progress waits, and the last verified revision stays available as an explicit snapshot. Changed files are checked every two seconds by default, with a full check at least every thirty seconds when the worker is not busy. This is eventual: there is no guaranteed delay.

Each revision holds the current context: the latest compaction summary and the turns after it. Full captures, pending events, unknown fields and related subagents are archived separately. A compaction or a rewind creates a revision without erasing earlier ones or reviving discarded messages. Repeated identical messages keep their own identities, and re-reading an unchanged session creates no revision.

Tool calls and results from another assistant become closed history. Instructions, provider signatures, reasoning and approvals stay in the archive and never gain authority in the target. The target's own settings and permissions apply when you continue.

Earlier sessions are checked when their own file changes, and fully at least every five minutes, so activity elsewhere in a shared database does not re-read them.

## Storage and JSON output

Synchronization uses its own store, `sync.sqlite3` (schema 1), separate from the transfer journal. Revisions are immutable and form a graph. Captured events, raw records, attachments and messages are compressed and addressed by SHA-256, and long histories share their common beginning. Integrity, depth and size limits are checked on both write and read: one native item may be up to 64 MiB, and a capture with its context up to 256 MiB. Larger data fails with an error; nothing is truncated.

All `sync` commands use the same JSON envelope (version 1) as the rest of the CLI. `status` and `list` return metadata only; each source also carries `tool`, `role` (`current`, `superseded` or `detached`) and `origin` (`original` or `copy`). `check` returns the number of new revisions, link states and error codes. `open` returns the link, revision, transfer and native IDs, the exact launch command, whether it `launched`, and whether an explicit snapshot was chosen. `restore` returns the restored path, the store's schema version and `history_only: true`.
