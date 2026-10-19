# Session Bridge

[![CI](https://github.com/pma1999/session-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/pma1999/session-bridge/actions/workflows/ci.yml) [![MIT License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Session Bridge is a local command-line tool for inspecting, archiving, and continuing coding-agent conversations across Claude Code, OpenCode, and Codex. It keeps a complete `.sbridge` archive of each transfer and can create native history for specifically verified client versions. An explicit handoff mode prepares a context file and a message for you to send yourself.

Session Bridge is an independent community project. It is not affiliated with, endorsed by, or supported by Anthropic, OpenAI, OpenCode, or their maintainers. Native storage formats can change; the compatibility table below describes the versions actually exercised.

## What it does and does not do

- Runs locally and does not send conversations to a Session Bridge service.
- Does not call a model when listing, inspecting, converting, exporting, or opening a transfer. Continuing a conversation is done by the native client under that client's settings.
- Preserves the source conversation and stores a separate transfer archive. A native destination is a new session; an already open client tab does not refresh automatically.
- Can optionally follow explicitly linked, completed conversation checkpoints on Windows. Sync is local and opt-in: it does not synchronize files, Git state, credentials, permissions, processes, or running subagents.
- Does not infer native compatibility from a product name. Unknown native client versions refuse native writes. Choose the explicit `handoff` mode when a native version is not enabled.

## Requirements and installation

Python 3.11 or newer is required. Download the portable ZIP from the [latest release](https://github.com/pma1999/session-bridge/releases/latest), or clone the repository:

```powershell
git clone https://github.com/pma1999/session-bridge.git
cd session-bridge
powershell -File scripts/install.ps1 -Python python
sessionbridge --help
sessionbridge doctor
```

The installer creates a project-local virtual environment and installs the `sessionbridge` command in your user command directory. Keep the project folder in place. Close interactive Session Bridge windows before installing or upgrading: Windows can lock their executable. The installer stops and restarts its managed synchronization worker when needed. Python dependencies are pinned; initial installation needs network access. The installer does not install or upgrade coding clients and refuses to replace an unmanaged command.

Each GitHub release includes the portable ZIP, a wheel, and SHA-256 checksum files. Session Bridge is distributed through GitHub Releases; no PyPI release is published.

Before the first transfer, make your own backup of any native client data you cannot replace. Session Bridge writes new native sessions for the exact versions listed below, but these clients own their local databases and formats. Its operation journal and synchronization data are stored separately under `%LOCALAPPDATA%\SessionBridge` on Windows, or `$XDG_DATA_HOME/session-bridge` on Linux. Set `SESSIONBRIDGE_HOME` to use an isolated data directory.

## Quick start

The interactive selector and `list` show sessions from the directory where you start Session Bridge. Directory matching is exact after path normalization: sibling projects and nested directories are separate scopes. Sessions without a known working directory appear only in the global scope. Explicit session IDs remain usable from any directory.

Type part of a title, path, or native ID to search. Search ignores case and accents, accepts partial words and keywords in any order, and replaces the complete search text when accepting a completion. Use Tab and the arrow keys to select; ambiguous matches require a more specific query or an explicit selection. The selector includes **Buscar en todos los proyectos** to change scope, and the equivalent CLI option is `--all-projects`.

```text
sessionbridge
sessionbridge --all-projects
sessionbridge list --from claude --search "my project"
sessionbridge list --all-projects --search "my project"
sessionbridge list --cwd "C:\Projects\app"
sessionbridge show claude-win:SESSION_ID
sessionbridge convert claude-win:SESSION_ID --to opencode-win --dry-run
sessionbridge convert claude-win:SESSION_ID --to opencode-win --resume
sessionbridge convert claude-win:SESSION_ID --to codex-win --mode handoff
sessionbridge export codex-win:SESSION_ID --output conversation.sbridge
sessionbridge import conversation.sbridge --to claude-win --cwd "C:\Projects\app"
sessionbridge transfers
sessionbridge recover TRANSFER_ID
```

Use `sessionbridge profile list` to see the installation IDs available on your machine. Add `--json` for the versioned machine-readable output. JSON and other non-interactive resume commands return launch arguments; invoke them yourself. Interactive prompts and CLI messages are currently in Spanish; code and technical documentation are in English.

## Native compatibility

Native writes are enabled only when storage, visible history, cold resume, and model-visible continuation have all been observed for that exact version. The evidence uses local mock providers for continuation checks; it does not promise behavior or billing for external providers.

| Platform | Client version | Native writes |
| --- | --- | --- |
| Windows x64 | Claude Code 2.1.302 | Enabled |
| Windows x64 | OpenCode 2.0.29 | Enabled; maximum 9,362 messages |
| Windows x64 | Codex CLI 0.159.0 | Enabled; maximum 128 MiB per rollout |
| Linux/WSL | Any | Not enabled; explicit handoff is available |

All six cross-client directions among the three listed Windows versions were exercised. Other versions and platforms have not been verified for native writes and are refused by the compatibility gate. See [native compatibility evidence](docs/native-compatibility.md) for the boundaries and [verification record](docs/verification.md) for scope and limits.

## Optional Windows synchronization

Synchronization is off until you explicitly link a session. It follows completed checkpoints and creates immutable native copies when you open a selected revision in another supported client. If copies continue separately, both branches remain available and you choose which one to open. Existing native tabs stay on the version they already loaded.

```text
sessionbridge sync link claude-win:SESSION_ID --name "My project"
sessionbridge sync status "My project"
sessionbridge sync open "My project" --to codex-win
```

Sync runs on Windows only. It does not install a login task, and it does not invoke or update the WSL helper. Read the [synchronization guide](docs/synchronization.md) before linking conversations; synchronization data and backups contain private conversation history.

## Project information

- [Contributing and development](CONTRIBUTING.md)
- [Security vulnerability reporting](SECURITY.md)
- [Code of conduct](CODE_OF_CONDUCT.md)
- [Changelog](CHANGELOG.md)
- [MIT License](LICENSE)
- [Architecture and data handling](docs/architecture.md)
