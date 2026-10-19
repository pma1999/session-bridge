# Maintaining Session Bridge

This guide is for maintainers preparing a compatibility change or release. Keep claims tied to the exact project and native-client versions that were exercised.

## Compatibility changes

Before enabling native writes for a client version, verify all four gates on that exact platform and version:

1. The imported data persists after the importing process exits.
2. The native history surface shows the imported user and assistant messages.
3. A cold resume opens the exact session ID in the intended working directory.
4. A subsequent request to a local mock provider contains the imported context.

Use isolated client profiles and synthetic sessions. Never use a personal profile or a paid provider as a probe target. Unknown versions must continue to refuse native writes. Record tested platform/version pairs, limits, and any unverified areas in [native compatibility](native-compatibility.md) and [verification](verification.md).

For synchronization changes, verify that only explicitly linked sources are captured, incomplete turns do not become checkpoints, branches remain available, and backups contain sync history only. Reconfirm that sync does not invoke or update the WSL helper.

## Local release checks

From a clean checkout, install the development dependencies and run:

```powershell
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
python -m pytest -q -p no:cacheprovider
python -m ruff check src tests scripts
python -m compileall -q src scripts
python scripts/build_portable.py
python scripts/verify_release.py
```

The release verifier checks the portable archive CRC and members, source and wheel contents, license and package metadata, and the SHA-256 manifests. The builder selects the wheel for the current project version even when older wheels remain in `dist`. The verifier prints the verified version, member count, and archive checksum.

## Release checklist

- Update the package version in `pyproject.toml` and `src/session_bridge/__init__.py` together.
- Add the user-facing changes to `CHANGELOG.md` and update compatibility and verification documents.
- Run the local checks above and any exact-version native probes required by the change.
- Run `python scripts/verify_release.py` and resolve any contents, metadata, or checksum mismatch it reports.
- Confirm installation and `sessionbridge --help` using the release artifact on each platform for which an installation claim is made.
- On Windows, close interactive Session Bridge windows before reinstalling. A running interactive launcher can prevent its replacement; the installer manages its synchronization worker separately.
- Publish the release notes with the tested compatibility matrix, known limits, and checks actually observed.

Do not describe Linux test or installation behavior as verified until it has been exercised in CI or on the target platform. Do not imply a provider, latency, or billing guarantee from a local mock test.
