"""Build a clean portable source bundle; never include sessions, logs, or private state."""

import hashlib
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
__version__ = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def main():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    destination = ROOT / "dist"
    destination.mkdir(exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(destination), str(ROOT)],
        check=True,
    )
    files = [
        ROOT / name
        for name in (
            "pyproject.toml",
            "README.md",
            "requirements.lock",
            "requirements-dev.lock",
            ".gitignore",
            "LICENSE",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "CODE_OF_CONDUCT.md",
            "CHANGELOG.md",
        )
    ]
    private_suffixes = {
        ".pyc",
        ".sbridge",
        ".jsonl",
        ".zst",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".log",
        ".tmp",
        ".bak",
    }
    for directory in ("src", "scripts", "tests", "docs", ".github"):
        files.extend(
            p
            for p in (ROOT / directory).rglob("*")
            if p.is_file()
            and "__pycache__" not in p.parts
            and p.suffix not in private_suffixes
            and not p.name.startswith(".env")
            and not p.name.endswith(("-wal", "-shm", "-journal"))
        )
    files.append(destination / f"session_bridge-{version}-py3-none-any.whl")
    output = destination / f"session-bridge-{version}-portable.zip"
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(files):
            archive.write(file, "session-bridge/" + file.relative_to(ROOT).as_posix())
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(checksum + "  " + output.name + "\n", encoding="ascii")
    wheel = destination / f"session_bridge-{version}-py3-none-any.whl"
    (destination / "SHA256SUMS").write_text(
        checksum
        + "  "
        + output.name
        + "\n"
        + hashlib.sha256(wheel.read_bytes()).hexdigest()
        + "  "
        + wheel.name
        + "\n",
        encoding="ascii",
    )
    print(output)
    print(checksum)


if __name__ == "__main__":
    main()
