"""End-to-end check of updating a real Windows installation, for CI and local verification.

Builds two releases from this checkout (OLD and NEW), installs OLD with its own install.ps1 into a
temporary LOCALAPPDATA, data folder and command folder, starts the sync worker, then runs
``sessionbridge update --yes`` through the installed command, so the command being replaced is
running. A fake GitHub answers inside the installed runtimes only: this script writes a
``sitecustomize.py`` there that serves the local release files. Nothing in the shipped package can
be redirected this way.

Then it repeats the update for an installation made from a Git clone, and checks that a release
whose checksums do not match installs nothing.

Usage: python scripts/e2e_update.py   (Windows, Python 3.11+, network access for pip)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD, NEW, BAD = "50.0.0", "50.0.1", "50.0.2"
REPO = "https://github.com/pma1999/session-bridge"
API = "https://api.github.com/repos/pma1999/session-bridge/releases/latest"
CDN = "https://release-assets.githubusercontent.com/e2e/"
TREE = ["src", "scripts", "tests", "docs", ".github"]
FILES = [
    "pyproject.toml", "README.md", "requirements.lock", "requirements-dev.lock", ".gitignore", "LICENSE",
    "CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md", "CHANGELOG.md",
]

SITECUSTOMIZE = '''
# Written by scripts/e2e_update.py into a throwaway test runtime: serve releases from local files.
# Applied only when Session Bridge imports its network and installation modules, so the package
# is first imported as in a real run (from the launcher, with the .exe already on sys.path).
import importlib.abc, importlib.util, io, json, os, sys

_ROUTES = os.environ.get("SESSIONBRIDGE_E2E_ROUTES")


class _Reply(io.BytesIO):
    def __init__(self, body, url):
        super().__init__(body)
        self.url, self.status, self.headers = url, 200, {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _apply(name, module):
    if name == "session_bridge.net":
        import urllib.error

        def _open(request, timeout, hosts):
            with open(_ROUTES, encoding="utf-8") as stream:
                route = json.load(stream).get(request.full_url)
            if route is None:
                raise urllib.error.URLError("no e2e route")
            final = route.get("final", request.full_url)
            if final != request.full_url and not hosts.allows(final, redirected=True):
                raise module.NetError("host")
            with open(route["file"], "rb") as stream:
                return _Reply(stream.read(), final)

        module._open = _open
    elif name == "session_bridge.installation" and os.environ.get("SESSIONBRIDGE_E2E_REMOTE"):
        module._REMOTES = module._REMOTES | {os.environ["SESSIONBRIDGE_E2E_REMOTE"]}


class _Patch(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name not in ("session_bridge.net", "session_bridge.installation"):
            return None
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(name)
        finally:
            sys.meta_path.insert(0, self)
        run = spec.loader.exec_module

        def exec_module(module):
            run(module)
            _apply(name, module)

        spec.loader.exec_module = exec_module
        return spec


if _ROUTES:
    sys.meta_path.insert(0, _Patch())
'''


def run(command, *, env=None, cwd=None, check=True):
    print("$", " ".join(str(part) for part in command), flush=True)
    result = subprocess.run(
        [str(part) for part in command], env=env, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.stdout.strip():
        print(result.stdout.rstrip())
    if result.stderr.strip():
        print(result.stderr.rstrip())
    if check and result.returncode != 0:
        raise SystemExit(f"failed ({result.returncode}): {command}")
    return result


def expect(condition, message):
    if not condition:
        raise SystemExit("FAILED: " + message)
    print("ok:", message, flush=True)


def source_copy(destination: Path, version: str) -> Path:
    destination.mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".venv", "dist")
    for name in TREE:
        shutil.copytree(ROOT / name, destination / name, ignore=ignore)
    for name in FILES:
        shutil.copy2(ROOT / name, destination / name)
    pyproject = destination / "pyproject.toml"
    text = re.sub(r'(?m)^version = ".*"$', f'version = "{version}"', pyproject.read_text(encoding="utf-8"), count=1)
    pyproject.write_text(text, encoding="utf-8")
    package = destination / "src" / "session_bridge" / "__init__.py"
    package.write_text(
        re.sub(r'__version__ = ".*"', f'__version__ = "{version}"', package.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    return destination


def build(source: Path, python: str, version: str) -> Path:
    run([python, source / "scripts" / "build_portable.py"], cwd=source)
    archive = source / "dist" / f"session-bridge-{version}-portable.zip"
    expect(archive.is_file(), f"release {version} built")
    return archive


def publish(server: Path, version: str, archive: Path, *, wrong_sums: bool = False) -> Path:
    """Make the fake GitHub list ``version`` as the latest release, served from ``archive``."""
    name = f"session-bridge-{version}-portable.zip"
    body = archive.read_bytes()
    digest = hashlib.sha256(b"not the archive" if wrong_sums else body).hexdigest()
    sums = server / f"{version}-SHA256SUMS"
    sums.write_text(f"{digest}  {name}\n", encoding="ascii")
    base = f"{REPO}/releases/download/v{version}/"

    def asset(asset_name, path):
        return {
            "name": asset_name,
            "size": path.stat().st_size,
            "url": "https://api.github.com/repos/pma1999/session-bridge/releases/assets/1",
            "browser_download_url": base + asset_name,
            "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    payload = {
        "tag_name": f"v{version}",
        "html_url": f"{REPO}/releases/tag/v{version}",
        "draft": False,
        "prerelease": False,
        "assets": [asset(name, archive), asset("SHA256SUMS", sums)],
    }
    if wrong_sums:
        payload["assets"][0]["digest"] = None
    api = server / f"{version}-api.json"
    api.write_text(json.dumps(payload), encoding="utf-8")
    routes = {
        API: {"file": str(api)},
        base + name: {"file": str(archive), "final": CDN + name},
        base + "SHA256SUMS": {"file": str(sums), "final": CDN + "SHA256SUMS"},
    }
    table = server / "routes.json"
    table.write_text(json.dumps(routes), encoding="utf-8")
    return table


def environment(base: Path, routes: Path | None = None, remote: str | None = None) -> dict:
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(
        LOCALAPPDATA=str(base / "appdata"),
        SESSIONBRIDGE_HOME=str(base / "home"),
        SESSIONBRIDGE_LANG="en",
        CLAUDE_CONFIG_DIR=str(base / "claude"),
        CODEX_HOME=str(base / "codex"),
    )
    if routes is not None:
        env["SESSIONBRIDGE_E2E_ROUTES"] = str(routes)
    if remote is not None:
        env["SESSIONBRIDGE_E2E_REMOTE"] = remote
    return env


def install(project: Path, python: str, commands: Path, env: dict) -> None:
    run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", project / "scripts" / "install.ps1",
         "-Python", python, "-CommandDirectory", commands], env=env)


def plant_fake_github(runtime: Path) -> None:
    (runtime / "Lib" / "site-packages" / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")


def receipt(base: Path) -> dict:
    return json.loads((base / "appdata" / "SessionBridge" / "installation.json").read_text(encoding="utf-8-sig"))


def worker(command: Path, env: dict) -> dict:
    return json.loads(run([command, "--json", "sync", "worker-status"], env=env).stdout)["data"]


def command_line(pid: int) -> str:
    # A venv's python.exe starts the base interpreter, so the process image is not the runtime's;
    # the command line shows which runtime started it.
    result = run(["powershell", "-NoProfile", "-Command",
                  f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"], check=False)
    return result.stdout.strip()


def same(first, second) -> bool:
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def from_release(work: Path, python: str, old_zip: Path, new_zip: Path) -> None:
    base, server, commands = work / "zip", work / "zip" / "server", work / "zip" / "bin"
    server.mkdir(parents=True)
    with zipfile.ZipFile(old_zip) as archive:
        archive.extractall(base / "old")
    old_project = base / "old" / "session-bridge"
    env = environment(base)
    install(old_project, python, commands, env)
    command = commands / "sessionbridge.exe"
    plant_fake_github(old_project / ".venv")
    started = json.loads(run([command, "--json", "sync", "start"], env=env).stdout)["data"]
    try:
        routes = publish(server, NEW, new_zip)
        env = environment(base, routes)
        result = run([command, "update", "--yes"], env=env, check=False)
        expect(result.returncode == 0 and f"Updated to {NEW}." in result.stdout, "the running command updated itself")
        expect(run([command, "--version"], env=env).stdout.strip() == f"Session Bridge {NEW}", "the command is NEW")
        target = base / "appdata" / "SessionBridge" / "app" / NEW
        record = receipt(base)
        expect(same(record["project"], target / "session-bridge"), "the receipt names the new release folder")
        expect((target / ".sessionbridge-managed").is_file(), "the new folder carries the updater's marker")
        direct = next((target / "session-bridge" / ".venv" / "Lib" / "site-packages").glob(
            "session_bridge-*.dist-info")) / "direct_url.json"
        expect(json.loads(direct.read_text())["url"].endswith(f"session_bridge-{NEW}-py3-none-any.whl"),
               "the verified wheel was installed, not a source build")
        status = worker(command, env)
        expect(status["healthy"] and status["generation"] != started["generation"], "the worker was restarted")
        line = command_line(status["pid"])
        # The program is what precedes " -m"; compared as resolved paths, since a temporary folder
        # can appear in its short 8.3 form (RUNNER~1) on one side and in full on the other.
        program = line.split(" -m ", 1)[0].strip().strip('"')
        expect(same(program, target / "session-bridge" / ".venv" / "Scripts" / "python.exe"),
               f"the worker runs the new runtime ({line})")
        leftovers = list(commands.glob("sessionbridge.exe.old-*"))
        expect(len(leftovers) == 1, "the command in use was renamed aside, not overwritten")
        expect(old_project.joinpath(".venv", "Scripts", "python.exe").is_file(), "the old installation is intact")

        # The next installation removes the renamed command, now that nothing runs it.
        install(target / "session-bridge", python, commands, environment(base))
        expect(not list(commands.glob("sessionbridge.exe.old-*")), "the renamed command was removed later")

        # A release whose checksums do not match installs nothing.
        plant_fake_github(target / "session-bridge" / ".venv")
        before = receipt(base)
        bad_zip = server / f"session-bridge-{BAD}-portable.zip"
        shutil.copy2(new_zip, bad_zip)
        env = environment(base, publish(server, BAD, bad_zip, wrong_sums=True))
        result = run([command, "--json", "update", "--yes"], env=env, check=False)
        expect(result.returncode == 2 and json.loads(result.stdout)["error"]["code"] == "update_integrity",
               "a release that fails verification is refused")
        expect(receipt(base) == before and not (target.parent / BAD).exists(), "and nothing was changed")
    finally:
        run([command, "--json", "sync", "stop"], env=environment(base), check=False)


def git(folder: Path, *args: str) -> str:
    return run(["git", "-C", folder, "-c", "user.name=e2e", "-c", "user.email=e2e@example.com", *args]).stdout.strip()


def from_checkout(work: Path, python: str, old_source: Path, new_source: Path, new_zip: Path) -> None:
    base, server, commands = work / "git", work / "git" / "server", work / "git" / "bin"
    server.mkdir(parents=True)
    origin = base / "origin.git"
    run(["git", "init", "-q", "--bare", "-b", "main", origin])
    author = base / "author"
    # Fresh timestamps: two copies differing only in a same-length version would look unchanged to Git.
    shutil.copytree(old_source, author, ignore=shutil.ignore_patterns("dist"), copy_function=shutil.copy)
    git(author, "init", "-q", "-b", "main")
    git(author, "add", "-A")
    git(author, "commit", "-q", "-m", OLD)
    git(author, "tag", "-a", f"v{OLD}", "-m", OLD)
    git(author, "remote", "add", "origin", str(origin))
    git(author, "push", "-q", "origin", "main", "--tags")
    clone = base / "clone"
    run(["git", "clone", "-q", origin, clone])
    env = environment(base)
    install(clone, python, commands, env)
    command = commands / "sessionbridge.exe"
    plant_fake_github(clone / ".venv")
    # NEW is published: a commit and tag on origin, and a release on the fake GitHub.
    for item in author.iterdir():
        if item.name != ".git":
            shutil.rmtree(item) if item.is_dir() else item.unlink()
    shutil.copytree(new_source, author, dirs_exist_ok=True, ignore=shutil.ignore_patterns("dist"),
                    copy_function=shutil.copy)
    git(author, "add", "-A")
    git(author, "commit", "-q", "-m", NEW)
    git(author, "tag", "-a", f"v{NEW}", "-m", NEW)
    git(author, "push", "-q", "origin", "main", "--tags")
    env = environment(base, publish(server, NEW, new_zip), remote=str(origin))
    result = run([command, "update", "--yes"], env=env, check=False)
    expect(result.returncode == 0, "a clean Git clone updated itself")
    expect(git(clone, "describe", "--tags", "--exact-match") == f"v{NEW}", "the clone moved forward to the tag")
    runtime = base / "appdata" / "SessionBridge" / "runtime" / NEW
    expect(same(receipt(base)["runtime"], runtime), "the new runtime has a folder of its own")
    expect(run([command, "--version"], env=env).stdout.strip() == f"Session Bridge {NEW}", "the command is NEW")
    expect((clone / ".venv" / "Scripts" / "python.exe").is_file(), "the clone's own environment is untouched")


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Windows only.")
    python = getattr(sys, "_base_executable", sys.executable)
    work = Path(tempfile.mkdtemp(prefix="sb-e2e-update-"))
    print("work folder:", work)
    old_source = source_copy(work / "source-old", OLD)
    new_source = source_copy(work / "source-new", NEW)
    old_zip = build(old_source, python, OLD)
    new_zip = build(new_source, python, NEW)
    from_release(work, python, old_zip, new_zip)
    from_checkout(work, python, old_source, new_source, new_zip)
    print("END-TO-END UPDATE: PASSED")


if __name__ == "__main__":
    main()
