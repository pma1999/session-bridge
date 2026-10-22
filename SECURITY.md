# Security policy

Session Bridge reads private conversation history, writes new sessions into other programs' storage for specifically verified versions, and installs its own updates. Please report suspected vulnerabilities privately, so a fix can be prepared before the details are public.

## Reporting a vulnerability

Use [GitHub's private vulnerability reporting form](https://github.com/pma1999/session-bridge/security/advisories/new). Include the affected version (`sessionbridge --version`), your platform, the steps to reproduce and the impact you observed. Use synthetic conversation data, and remove credentials, tokens, personal paths and conversation content from the report.

Please do not open a public issue for an unpatched vulnerability. This project does not publish a guaranteed response or remediation time.

## Supported versions

Fixes are made in the latest release. Installations on 0.8.0 or later receive them through the [built-in updater](docs/updates.md); older ones need a manual installation.

## Scope

Examples of what we want to hear about:

- an archive (`.sbridge`), synchronization backup or native session that makes Session Bridge write outside the folders it owns, execute code, or read data it should not;
- a source conversation, archive or update that changes or deletes data Session Bridge does not own;
- instructions, approvals or tool calls from a source that become permissions in the target assistant;
- conversation content leaking into errors, logs, receipts or other places documented as content-free;
- an update that installs files other than the verified release, or a way around the update checks described in [updates](docs/updates.md#what-the-checksums-do-and-do-not-prove);
- WSL calls that can be made to run commands or reach a distribution the user did not choose.

Out of scope: vulnerabilities in the coding assistants themselves, in Python or in the dependencies (report those upstream), and attacks that need an attacker to already control your user account. SHA-256 checksums detect damaged or altered files; they do not authenticate the author of an archive, backup or release, as the documentation states.
