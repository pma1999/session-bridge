# Maintaining Session Bridge

This guide is for maintainers enabling a new client version or publishing a release. Every claim in the documentation and release notes must be tied to the exact project and client versions that were exercised.

## Compatibility changes

Newer client versions with the same major number are written to without a release: their format is checked on every transfer (see [native compatibility](native-compatibility.md)). The *Client watch* workflow runs `scripts/watch_clients.py` every week against the newest Claude Code, Codex and OpenCode and opens an issue when an unattended gate fails or the gate refuses the version. Run it by hand on Windows to check a specific version:

```powershell
python scripts/watch_clients.py --version claude=2.1.308
```

A release is needed only when that check fails. Before adding a version to `registry.VERIFIED`, verify all four gates on that exact platform and version:

1. **Persistence.** The imported session is still there after the importing process exits.
2. **Presentation.** The client's own history shows the imported user and assistant messages.
3. **Cold resume.** Starting the client fresh opens the exact session ID in the intended working folder.
4. **Model context.** The next request to a local mock provider contains the imported context.

Use isolated client profiles, synthetic sessions and a local mock provider (`scripts/probe_native.py`). Never use a personal profile or a paid provider as a probe target. Versions older than the oldest verified one, other major versions and unparseable versions must keep refusing native writes. Record the tested platform and version pairs, the limits found, and anything left unverified in [native compatibility](native-compatibility.md) and [verification](verification.md).

For synchronization changes, verify that only explicitly linked sources are captured, unfinished turns do not become checkpoints, continuations remain available, backups contain synchronization history only, and synchronization does not invoke or update the WSL helper (`scripts/probe_sync.py`).

## Local checks

From a clean checkout with the development dependencies installed:

```powershell
python -m pytest -q -p no:cacheprovider
python -m ruff check src tests scripts
python -m compileall -q src scripts
python scripts/build_docs.py --check
python scripts/build_portable.py
python scripts/verify_release.py
python scripts/e2e_update.py
```

`build_portable.py` builds the wheel and the portable ZIP for the current version, even when older wheels remain in `dist`. `verify_release.py` checks the ZIP's members and CRCs, that its sources match the checkout byte for byte, the wheel's contents and metadata, the license, and the SHA-256 manifests, and prints the version, member count and checksum. `e2e_update.py` builds two versions, installs the older one in temporary folders and updates it, from a ZIP and from a Git clone.

## Publishing a release

Session Bridge is distributed through GitHub Releases only. Releases are what installed copies update to, so a broken release reaches users on their next start: publish only from a verified commit.

1. **Start from the current `main`.** Fetch, and release from its latest commit, not an older planned one.
2. **Choose the version.** Raise the minor version (`0.8.0` → `0.9.0`) when users get something new to notice, such as a command, a flow, a language or a newly supported client version. Raise the patch version (`0.8.0` → `0.8.1`) for a fix or refinement with nothing new to learn.
3. **Bump it everywhere.** `pyproject.toml` and `src/session_bridge/__init__.py`, the version and exact test count in [verification](verification.md), and the changelog: work merged since the last release sits under `## Unreleased`, which becomes `## X.Y.Z`.
4. **Run the local checks** above.
5. **Commit and push to `main`.** Never force-push.
6. **Wait for green CI on that exact commit**, both CI and CodeQL. If anything was committed after the last local build, build again.
7. **Tag the verified commit** with an annotated tag and push only the tag:

   ```powershell
   git tag -a vX.Y.Z <sha> -m "Session Bridge X.Y.Z"
   git push origin refs/tags/vX.Y.Z:refs/tags/vX.Y.Z
   ```

8. **Create the release** with the four files the updater expects:

   ```powershell
   gh release create vX.Y.Z --verify-tag --latest --title "Session Bridge X.Y.Z" --notes-file release-notes.md dist/session-bridge-X.Y.Z-portable.zip dist/session_bridge-X.Y.Z-py3-none-any.whl dist/session-bridge-X.Y.Z-portable.zip.sha256 dist/SHA256SUMS
   ```

   Release notes are in English: a summary, the changes, the verified native scope, links to the CI and CodeQL runs, the local test count, and installation notes.
9. **Verify what was published.** Download the release into an empty folder and compare every file's SHA-256 with `dist/`. Check that `gh api repos/pma1999/session-bridge/releases/latest --jq .tag_name` names the new tag, and that `git ls-remote origin refs/heads/main "refs/tags/vX.Y.Z^{}"` shows the same commit for both.
10. **Update a real installation.** Open Session Bridge on the previous release and take the offered update; it should reopen on the new version.

## The update contract

Installed copies update themselves with the code of the version being replaced, so a release must keep what older updaters rely on. The [contract](CONTRACT.md#updates) lists it; in short:

- a final `vX.Y.Z` tag, marked as the latest release;
- assets named `session-bridge-X.Y.Z-portable.zip` and `SHA256SUMS`, with every ZIP member under `session-bridge/` and `session-bridge/pyproject.toml` declaring `X.Y.Z`;
- `scripts/install.ps1` accepting `-Python`, `-CommandDirectory` and `-Runtime`, and recording the new installation in `%LOCALAPPDATA%\SessionBridge\installation.json`.

`tests/test_updater.py` pins these names. Changing any of them strands every installation that cannot read the new form; doing so needs a release that supports both.

## Release checklist

- [ ] Version bumped in `pyproject.toml` and `src/session_bridge/__init__.py`.
- [ ] `CHANGELOG.md`, compatibility and verification documents updated.
- [ ] Generated documentation current (`python scripts/build_docs.py --check`).
- [ ] Local checks and any native probes for newly verified versions passed.
- [ ] CI and CodeQL green on the exact commit being tagged.
- [ ] Four assets attached; downloaded hashes match `dist/`.
- [ ] `releases/latest` names the new tag; tag and `main` point to the same commit.
- [ ] Release notes state the tested compatibility, known limits and the checks actually observed.

Do not describe Linux installation or native behavior as verified until it has been exercised on that platform. Do not imply a provider, latency or billing guarantee from a local mock test.
