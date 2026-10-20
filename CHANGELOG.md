# Changelog

This file records notable user-facing changes. Version numbers refer to the project package.

## 0.5.0

- A synchronized conversation now has one current session per assistant: the one Session Bridge opened or created for it last. Switching assistants reopens that session when it is already up to date, including the original session you started in, instead of creating another copy. Before, going back to an assistant always created a new session, even when nothing had changed.
- When the conversation has new turns, a new native session is still created, because no supported client lets another program add turns to a session it may have open. The previous one stays as it was and becomes an earlier session. It is still watched, so anything typed in it is captured as its own continuation.
- Before opening, the interactive flow says whether it will reopen your session or create a new one, and that typing in the previous one would start another continuation.
- When you continued in more than one place, the choice names where each continuation was typed (an assistant, its original session or an earlier copy), how long ago and its last request, and is remembered. `sync select` and `--branch` also accept the profile of the assistant you continued in, such as `claude-win`.
- New turns in a continuation that was not chosen now ask for a choice again. Before, the conversation stayed "Al día" and those turns were silently left out of what opened.
- `sync list` and `sync status` show each assistant's current session and how many earlier ones it has, where each version came from, and the exact `sync select` commands when there is a choice to make. In JSON, endpoints also report `tool`, `role` and `origin`.
- Earlier sessions are checked when their own file changes and fully at least every five minutes, so activity in a shared Codex or OpenCode database no longer re-reads every accumulated copy.

## 0.4.0

- Redesigned the interactive start screen as one live search over every conversation: sessions from Claude Code, Codex and OpenCode and synchronized conversations (marked ●) in one list. Conversations from the current folder come first, under "En esta carpeta", followed by "En otros proyectos", so a search never misses a conversation hidden behind a scope switch.
- Results update as you type; the arrow keys, Tab, Page Up/Down, Enter and Esc work throughout, and going back keeps your search. Rows show the assistant, a relative date and, for other projects, the folder name; narrow terminals get a compact layout.
- Subagent and helper threads (Claude Code subagents, OpenCode child sessions, Codex spawned agents and guardian reviews) are no longer listed as separate conversations in the interactive search.
- Converting a session now takes two choices by default: where to continue it and what to do from a summary of the origin, destination, history, format and fidelity. From the summary you can convert, convert and keep it synchronized, or switch between the current context and the full history; after a native conversion you can open it right away.
- A missing working folder asks for one with folder completion, an unsupported target version explains why and offers the manual handoff, and an ambiguous context start asks for an explicit choice.
- Opening a synchronized conversation offers its versions with their origin and age when there is more than one, and only lists assistants that can open it.
- The start screen no longer fails while the synchronization worker is busy: opening a current synchronization store no longer writes to it, and if synchronized conversations still cannot be read, the rest of the search stays available with a notice.
- `list` shows titles, assistant names and relative dates, never cuts the session ID, drops the folder column when it lists a single folder, switches to a compact layout on narrow terminals and says how many results it found and how to widen the search. `transfers` is a table, and synchronized conversations show short version IDs.
- Every command and option has Spanish help text, and `sessionbridge --help` includes examples.
- Empty results explain what to do next.
- Requires prompt-toolkit 3.0.52 or later.
- Claude Code sessions now resolve the active branch the way Claude Code does when it resumes. Parallel tool results, interrupted attempts that were retried, metadata records and image notes beside tool results no longer cause a false ambiguity or drop the final reply. API error notices are left out. Prompts, task notifications and subagent reports delivered mid-turn are kept once.
- Codex threads whose history is split across rollout segments, or forked from another thread, now include the earlier history they continue. Before, only the last file was read, and a conversation could lose its beginning. When that earlier history is missing, damaged or stops short, the context is marked ambiguous instead of silently starting later.
- Messages between Codex agents are attributed correctly: an agent's own messages are its output, and messages from other agents reach it as input. A payload the source provider encrypted is marked as unavailable instead of being left empty.
- Paginated Codex threads whose history Codex had not yet indexed are no longer marked ambiguous when their rollout exists; Codex resumes them from that rollout.
- The current context no longer revives a message discarded by compaction when its retained copy shares its ID.
- OpenCode sessions stored in both the legacy and v2 tables are read as one conversation instead of being marked ambiguous. A compaction appears once, with its retained recent conversation, and the current context starts there.

## 0.3.2

- Scoped the interactive selector and session list to the launch directory by default, with an explicit global search option.
- Fixed multiword title completion replacing only the last word and producing an invalid selection.
- Unified title, path and ID search across the selector and CLI: accent/case-insensitive keywords, partial words and stable phrase ranking.
- Applied the same directory scope to linked conversations and added a visible scope switch, cancellation and empty-scope guidance.
- Fixed Codex sessions recorded with Windows extended-length (`\\?\`) paths being refused with `source_path_invalid`; out-of-profile paths remain refused.
- Fixed conversion and sync of Codex sessions with subagents failing with `source_not_found` when a subagent's history lives only in its rollout file.
- Stopped Codex retained-context items from each embedding a full copy of their compaction record, which made large sessions grow quadratically and exceed package limits.
- Raised the package member limit to 1 GiB, the package total to 2 GiB and the sync capture limit to 1 GiB for very large sessions; import ratio and entry checks are unchanged.
- Native conversions on Windows now target the plain working directory instead of a `\\?\` path, so Claude Code finds the converted session in its project folder.
- Codex `verified_answer` records (answers to the model's questions) no longer make the context ambiguous; the current context again starts at the last compaction instead of forcing the full history.
- Enabled native writes for Claude Code 2.1.304 on Windows x64 after it passed the four isolated native gates; 2.1.302 is no longer the enabled version.

## 0.3.1

- Added portable ZIP and wheel GitHub release assets with SHA-256 checksums and included project community documentation in the release bundle.
- Added automated Windows and Linux CI coverage across Python 3.11–3.14 and a release artifact verifier.
- Improved Windows installer compatibility by removing its dependency on `Get-FileHash` and corrected quoting for legacy native-client arguments.
- Added opt-in local Windows synchronization for explicitly linked completed conversation checkpoints.
- Added immutable native copies, retained branch selection, recovery, export, and history-only backup and restore.
- Added compatibility gates for Claude Code 2.1.302, OpenCode 2.0.29, and Codex CLI 0.159.0 on Windows x64, covering all six cross-client directions.
- Kept synchronization local and Windows-only; the WSL helper is not invoked or updated by sync.

## 0.3.0

- Added automatic local Windows conversation synchronization, immutable revisions, branch selection, worker controls, and history-only backup and restore.
- Added native continuation for the six directions among the verified Windows client versions.

## 0.2.0

- Added native Codex CLI continuation on Windows x64 for the verified Codex version, completing the four then-verified cross-client directions.
- Added safer native operation recovery and bounded rollout handling.

## 0.1.0

- Introduced local conversation discovery, inspection, archive, transfer, export, import, recovery, and explicit handoff workflows for Claude Code, OpenCode, and Codex.
- Added Windows and WSL/Linux readers and the separately managed WSL helper.
