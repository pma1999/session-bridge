# Verification

This record summarizes the evidence available for the 0.3.2 source state. It describes local checks and observed native-client behavior; it is not a guarantee for other machines, client versions, or external providers.

## Automated checks

The project test suite passed **326 tests**, with **one skipped** Windows symlink-privilege case. A separate directory-junction safety check was exercised. Ruff and Python compilation also passed. The tests cover archive integrity, native readers and writers, six transfer directions, sync capture and branching, recovery, backup and restore, CLI behavior, directory scope, multiword completion, metadata search, and refusal paths. Release tests additionally check license inclusion, current-checkout version selection, private-file exclusion, checksum validation, and rejection of unexpected wheel members or package identities.

The local run was on Windows x64 with Python 3.13.1. The skipped symlink case reflects unavailable Windows privilege, not a passing symlink test. There is no configured static typecheck. Hosted CI is configured for Windows and Ubuntu with Python 3.11–3.14, package verification, and Windows installation with worker restart. [Per-run results](https://github.com/pma1999/session-bridge/actions/workflows/ci.yml) show which checks actually completed for each commit. Tests of mocked Windows adapters on Ubuntu do not establish native Linux compatibility.

## Native-client observations

For Windows x64, Claude Code 2.1.302, OpenCode 2.0.29, and Codex CLI 0.159.0 passed the four native-write gates: persistence after import, visible native history, cold resume of the exact session, and inclusion of imported messages in a subsequent request to a local mock provider. Each of the six cross-client directions among these versions was exercised through the Session Bridge CLI.

Claude Code 2.1.304 on Windows x64 then passed the same four gates. `scripts/probe_native.py` recorded persistence, cold resume and the local mock request in its report. The probe does not record presentation for Claude, so that gate was confirmed by inspecting the native TUI opened with `--present-only`: after cold resume it showed both imported Unicode messages. The isolated probe profile, and only that profile, was marked as onboarded, trusting its synthetic project and accepting the fake probe key. 2.1.304 replaces 2.1.302 as the enabled Claude version. The cross-client directions into Claude were not re-run on 2.1.304.

These continuation checks used a local mock provider. They show the tested clients exposed the expected continuation context to that mock; they do not establish a guarantee about any external provider, model response, or billing. Native formats can change, so a new client version remains disabled until its exact version passes the compatibility checks.

The current native resource limits are 9,362 messages for OpenCode 2.0.29 and 128 MiB per Codex 0.159.0 rollout. Oversized native transfers are refused rather than truncated. Native writes on Linux and WSL have not been verified and remain disabled; explicit handoff is available.

## Synchronization observations

The Windows sync checks exercised completed-checkpoint capture, all six supported native directions, unchanged-source checks, immutable copy reuse, branch selection, worker lifecycle, and history backup/restore. A real continuation used a local simulated provider; it made one mock request and no paid request. This is local evidence for the exercised versions and setup, not a service-level latency or external-provider guarantee.

Synchronization remains opt-in and Windows-only. No Linux or WSL native sync gate has been observed. See the [synchronization guide](synchronization.md) for its data boundary, lifecycle, and backup scope.

## Reproducing local checks

From a development checkout with the dependencies installed:

```powershell
python -m pytest -q -p no:cacheprovider
python -m ruff check src tests scripts
python -m compileall -q src scripts
python scripts/build_portable.py
python scripts/verify_release.py
```

The test suite and portable build do not establish native-client compatibility by themselves. Native compatibility evidence requires the exact installed client version, an isolated test profile, visible history, cold resume, and a local mock continuation check. Probe instructions are maintained with the [native compatibility notes](native-compatibility.md).
