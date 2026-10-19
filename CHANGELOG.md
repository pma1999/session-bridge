# Changelog

This file records notable user-facing changes. Version numbers refer to the project package.

## 0.3.2

- Scoped the interactive selector and session list to the launch directory by default, with an explicit global search option.
- Fixed multiword title completion replacing only the last word and producing an invalid selection.
- Unified title, path and ID search across the selector and CLI: accent/case-insensitive keywords, partial words and stable phrase ranking.
- Applied the same directory scope to linked conversations and added a visible scope switch, cancellation and empty-scope guidance.
- Fixed Codex sessions recorded with Windows extended-length (`\\?\`) paths being refused with `source_path_invalid`; out-of-profile paths remain refused.

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
