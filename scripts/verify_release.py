"""Verify release contents without executing a native client or accessing user data."""

import hashlib
import io
import json
import re
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

ROOT_FILES = {
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
}
SOURCE_DIRS = {"src", "scripts", "tests", "docs", ".github"}
PRIVATE_SUFFIXES = {
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


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(root: Path) -> dict:
    root = Path(root).resolve()
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    dist = root / "dist"
    output = dist / f"session-bridge-{version}-portable.zip"
    wheel = dist / f"session_bridge-{version}-py3-none-any.whl"
    checksum = sha256(output)
    require(
        output.with_suffix(".zip.sha256").read_text(encoding="ascii").split() == [checksum, output.name],
        "Invalid portable checksum",
    )
    expected_sums = {output.name: checksum, wheel.name: sha256(wheel)}
    lines = (dist / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    sums = [line.split() for line in lines]
    require(len(sums) == 2 and all(len(parts) == 2 for parts in sums), "Invalid release checksum manifest")
    require({name: value for value, name in sums} == expected_sums, "Invalid release checksum manifest")
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)) and archive.testzip() is None, "Invalid portable archive")
        for name in names:
            path = PurePosixPath(name)
            require(
                len(path.parts) >= 2
                and path.parts[0] == "session-bridge"
                and ".." not in path.parts
                and "\\" not in name,
                "Unexpected archive member",
            )
            relative = path.relative_to("session-bridge")
            if relative.as_posix() == "dist/" + wheel.name:
                require(archive.read(name) == wheel.read_bytes(), "Wheel bytes differ in portable archive")
                continue
            require(
                relative.as_posix() in ROOT_FILES or relative.parts[0] in SOURCE_DIRS,
                "Unexpected archive member",
            )
            require(
                relative.suffix not in PRIVATE_SUFFIXES
                and not relative.name.endswith(("-wal", "-shm", "-journal"))
                and not any(part.startswith(".env") or part == "__pycache__" for part in relative.parts),
                "Private data in archive",
            )
            source = root / relative
            require(source.resolve() == source and source.is_file(), "Aliased or missing release source")
            require(archive.read(name) == source.read_bytes(), "Source bytes differ in portable archive")
        for name in (
            "LICENSE",
            "README.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "CODE_OF_CONDUCT.md",
            "CHANGELOG.md",
        ):
            require("session-bridge/" + name in names, "Missing public document: " + name)
        require("session-bridge/dist/" + wheel.name in names, "Missing current wheel")
        with zipfile.ZipFile(io.BytesIO(wheel.read_bytes())) as package:
            wheel_names = package.namelist()
            require(
                len(wheel_names) == len(set(wheel_names)) and package.testzip() is None,
                "Invalid wheel archive",
            )
            info = f"session_bridge-{version}.dist-info"
            require(info + "/METADATA" in wheel_names, "Missing wheel metadata")
            metadata = BytesParser().parsebytes(package.read(info + "/METADATA"))
            require(
                re.sub(r"[-_.]+", "-", metadata["Name"] or "").lower() == "session-bridge",
                "Invalid wheel package name",
            )
            require(
                metadata["Version"] == version and metadata["License-Expression"] == "MIT",
                "Invalid wheel version or license metadata",
            )
            license_path = info + "/licenses/LICENSE"
            require(license_path in wheel_names, "Missing LICENSE in wheel")
            require(package.read(license_path) == (root / "LICENSE").read_bytes(), "Wheel license differs")
            python_sources = list((root / "src/session_bridge").rglob("*.py"))
            expected_members = (
                {source.relative_to(root / "src").as_posix() for source in python_sources}
                | {info + "/" + name for name in ("METADATA", "WHEEL", "entry_points.txt", "RECORD")}
                | {license_path}
            )
            lock = root / "requirements.lock"
            if lock.is_file():
                resource = "session_bridge/resources/requirements.lock"
                expected_members.add(resource)
                require(resource in wheel_names, "Missing wheel dependency lock")
                require(package.read(resource) == lock.read_bytes(), "Wheel dependency lock differs")
            require(set(wheel_names) == expected_members, "Unexpected or missing wheel member")
            matched = 0
            for source in python_sources:
                relative = source.relative_to(root)
                require(
                    archive.read("session-bridge/" + relative.as_posix()) == source.read_bytes(),
                    "Missing Python source",
                )
                require(
                    package.read(source.relative_to(root / "src").as_posix()) == source.read_bytes(),
                    "Wheel/source mismatch",
                )
                matched += 1
    return {
        "version": version,
        "members": len(names),
        "source_files": matched,
        "sha256": checksum,
        "verified": True,
    }


if __name__ == "__main__":
    print(json.dumps(verify(Path(__file__).resolve().parents[1]), indent=2))
