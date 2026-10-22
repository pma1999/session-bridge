# Contributing

Thanks for helping improve Session Bridge. Issues and pull requests are welcome, whether they fix a typo, add a test or support a new client version.

Session Bridge writes into other programs' private storage, so the project is strict about evidence: a change should say what it does, and show how it was checked. This guide explains how to set up the project, how it is organized, and what a pull request needs.

**Contents**

- [Reporting a problem](#reporting-a-problem)
- [Setting up](#setting-up)
- [How the repository is organized](#how-the-repository-is-organized)
- [Project rules](#project-rules)
- [Texts and languages](#texts-and-languages)
- [Running the checks](#running-the-checks)
- [Generated documentation](#generated-documentation)
- [Common changes](#common-changes)
- [Pull requests](#pull-requests)

## Reporting a problem

Open an [issue](https://github.com/pma1999/session-bridge/issues) with:

- what you ran and what happened, including the error code (the word before the message, such as `source_active`);
- `sessionbridge --version` and the output of `sessionbridge doctor`;
- your Windows version and the versions of the assistants involved.

Do not attach conversation archives, `.sbridge` files, database files, credentials or personal paths. Use made-up examples. Report security problems privately, as described in the [security policy](SECURITY.md).

## Setting up

You need Python 3.11 or newer and Git. Windows is needed for native and synchronization work; most tests also run on Linux.

```powershell
git clone https://github.com/pma1999/session-bridge.git
cd session-bridge
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
```

`requirements.lock` and `requirements-dev.lock` pin every dependency. The editable install makes `sessionbridge` run your working copy. To keep your own Session Bridge data out of experiments, point `SESSIONBRIDGE_HOME` at a scratch folder; the tests already do this for themselves.

## How the repository is organized

| Path | Holds |
| --- | --- |
| `src/session_bridge/` | The application. [Architecture](docs/architecture.md) describes every module. |
| `src/session_bridge/readers/`, `writers/` | One reader and one writer per assistant, and the version gate (`writers/registry.py`). |
| `src/session_bridge/i18n/` | The English and Spanish catalogs and the language machinery. |
| `tests/` | The pytest suite. `conftest.py` isolates every test from your data, turns off update checks and runs in Spanish with strict texts. |
| `tests/fixtures/` | Synthetic native sessions used by reader tests. |
| `scripts/install.ps1`, `install.py` | The Windows installer (also the update handoff target) and a basic Linux installer. |
| `scripts/build_portable.py`, `verify_release.py` | Build and verify the release ZIP, wheel and checksums. |
| `scripts/build_docs.py` | Generates the command reference and screenshots. |
| `scripts/e2e_update.py` | End-to-end update test: installs one build and updates it to another in temporary folders. |
| `scripts/probe_native.py`, `probe_sync.py` | Isolated native-client probes for compatibility evidence. |
| `docs/` | User and contributor documentation. |

## Project rules

These rules come from the design; reviews check them.

- **Sources are read-only.** Readers never write, and never read configuration, account or credential data.
- **Compatibility is exact.** Native writing is enabled per exact client version, after the four gates in [maintaining](docs/maintaining.md#compatibility-changes). Never widen a version check because a new release looks similar.
- **Nothing is guessed or truncated.** Ambiguous history asks the user; oversized data fails with an error.
- **Effects are journaled and verified.** A native write is recorded before it happens and read back afterwards. An exit code is not proof.
- **Foreign authority stays history.** Instructions, approvals and tool calls from a source never become permissions in the target.
- **Errors are content-free.** Raise `BridgeError(code, key, **details)` with a stable code; never put conversation text in errors, logs or stored data.
- **No new dependencies without a reason.** Explain why the standard library and the existing dependencies are not enough.
- **The update contract is frozen.** Released versions update to new ones using the names in [the contract](docs/CONTRACT.md#updates); keep them working.

## Texts and languages

Session Bridge speaks English and Spanish, and every text a person reads comes from the catalogs in `src/session_bridge/i18n/`.

- Add each new text to **both** `en.py` and `es.py` under the same key, and use it with `i18n.t("your.key", ...)`. Never write user-facing text inline.
- Store and exchange codes and keys, never rendered text.
- The context sent to a target's model stays in English and is not translated.

`tests/test_i18n.py` and `tests/test_ui_text.py` enforce this: both catalogs have the same keys, parameters and plural forms; every key the code uses exists and every catalog text is used; no text reaches the screen outside the catalogs (checked with a pseudo-language); and error texts keep reading as they did. Spanish texts use the same plain, direct register as the English ones.

To add a language, add a catalog module next to `en.py` with `NAME`, `QUOTES`, `DECIMAL`, `plural()` and `MESSAGES`, and register it in `i18n/__init__.py`. The parity tests show what is missing.

## Running the checks

Run the focused tests while you work, then everything before you open a pull request:

```powershell
python -m pytest -q -p no:cacheprovider
python -m ruff check src tests scripts
python -m compileall -q src scripts
python scripts/build_docs.py --check
python scripts/build_portable.py
python scripts/verify_release.py
```

On Windows, one symlink test is skipped unless Windows grants the symlink privilege. `python scripts/e2e_update.py` runs the end-to-end update test; it installs into temporary folders and leaves your installation alone.

[CI](https://github.com/pma1999/session-bridge/actions/workflows/ci.yml) runs the suite on Windows and Ubuntu with Python 3.11 to 3.14, verifies the release package, and installs and updates Session Bridge on Windows. Tests of Windows adapters that run on Ubuntu use fixtures; they do not show that native writing works on Linux.

## Generated documentation

Two parts of the documentation are generated from the program and must not be edited by hand:

- `docs/cli-reference.md`, from the command tree and the English catalog;
- `docs/images/*.svg`, screenshots drawn by the real start screen with made-up conversations, in English and Spanish, light and dark.

After changing a command, an option, a help text or the start screen, regenerate them and commit the result:

```powershell
python scripts/build_docs.py
```

`tests/test_docs.py` fails when the command reference is out of date, when a documented command or option does not exist, when a link or an anchor is broken, or when `README.md` and `README.es.md` drift apart.

## Common changes

**Supporting a new client version.** Install the version in an isolated profile, run `scripts/probe_native.py` against a local mock provider, and record the four gates. Then enable that exact version in its writer, add version-specific fixtures and tests, and update [native compatibility](docs/native-compatibility.md), [verification](docs/verification.md), the README tables and the changelog. Never point a probe at your personal profile or a paid provider.

**Adding a command or option.** Add it in `cli.py` or `sync_cli.py` with catalog keys for its help, add both languages, cover it in `tests/test_cli.py`, document it in the user guide where it fits, and regenerate the reference.

**Changing the start screen.** `tests/test_interactive.py` drives the real prompts with keystrokes. Check the result at narrow and wide widths, in both languages, then regenerate the screenshots.

**Changing stored data.** The journal and the synchronization store refuse newer schema versions. A schema change needs a migration with backup and restore evidence, and must keep older versions from opening the new data.

## Pull requests

The [pull request template](.github/pull_request_template.md) asks for three things: the change, how it was verified, and its effect on compatibility and privacy. In particular:

- Keep a pull request to one change, and explain the behavior a user will see.
- Add or update tests, including the refusal and recovery cases.
- Say which checks you ran and what they showed. Separate unit tests and mocks from evidence observed with a real client.
- Update the user documentation and `CHANGELOG.md` when behavior or compatibility changes, and regenerate generated documentation.
- Do not claim that files, permissions, processes or native history were restored unless that was observed.

By contributing, you agree that your contribution is licensed under the [MIT License](LICENSE). Please follow the [code of conduct](CODE_OF_CONDUCT.md).
