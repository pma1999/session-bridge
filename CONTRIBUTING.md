# Contributing

Thanks for taking the time to improve Session Bridge. Issues and pull requests are welcome. Please describe the user-visible problem, the platforms and client versions involved, and the evidence you used. Do not attach real conversation archives, credentials, or private client data to an issue.

## Development setup

Use Python 3.11 or newer. From the repository root:

```powershell
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
```

Run the focused tests while working, then the full checks before opening a pull request:

```powershell
python -m pytest -q -p no:cacheprovider
python -m ruff check src tests scripts
python -m compileall -q src scripts
python scripts/build_portable.py
python scripts/verify_release.py
```

Native probes use isolated client data and local mock providers. Do not point probes at personal profiles or send a real provider request. See [verification](docs/verification.md) and [native compatibility](docs/native-compatibility.md) for what the existing evidence establishes.

## Pull requests

- Keep changes focused and explain the behavior and any compatibility effect.
- Add or update tests for behavior changes, including refusal and recovery cases where relevant.
- Update user documentation and `CHANGELOG.md` when behavior or supported compatibility changes.
- Preserve archives and source histories; do not claim that native history, files, permissions, or running processes were restored unless that behavior is directly verified.
- Do not add dependencies without explaining why existing facilities are insufficient.
- Follow the [Code of Conduct](CODE_OF_CONDUCT.md).
