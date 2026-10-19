"""Portable rootless installer. Run from the extracted project using Python 3.11+."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    if sys.version_info < (3, 11):  # noqa: UP036 - the installer runs before project constraints apply
        raise SystemExit("Python 3.11+ is required.")
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    from session_bridge.bootstrap import prepare_venv

    environment = root / ".venv"
    python = prepare_venv(environment)
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    for args in [["-r", str(root / "requirements.lock")], ["--no-deps", str(root)]]:
        subprocess.run([str(python), "-m", "pip", "install", *args], check=True)
    command = scripts / ("sessionbridge.exe" if os.name == "nt" else "sessionbridge")
    directory = Path.home() / ".local/bin"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / command.name
    if target.exists() or target.is_symlink():
        if target.is_symlink() and target.resolve() == command.resolve():
            pass
        else:
            raise SystemExit(f"Refusing to replace an existing command: {target}. Use {command} directly.")
    elif os.name == "nt":
        # A pip launcher contains the absolute interpreter path of this isolated installation.
        import shutil

        shutil.copy2(command, target)
    else:
        target.symlink_to(command)
    subprocess.run([str(target), "--help"], check=True)
    print(f"Installed {target}. Keep the extracted project folder in place.")
    receipt = {
        "command": str(target),
        "project": str(root),
        "sha256": hashlib.sha256(command.read_bytes()).hexdigest(),
    }
    (root / ".installation.json").write_text(json.dumps(receipt), encoding="utf-8")


if __name__ == "__main__":
    main()
