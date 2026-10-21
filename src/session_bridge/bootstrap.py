"""Small stdlib-only installer used from the project wheel during WSL setup.

This module deliberately avoids importing the application runtime.  It can be
loaded from the supplied wheel before any third-party dependencies exist.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import venv
from pathlib import Path
from typing import Any

from .errors import BridgeError

PIP_VERSION = "26.2.1"
PIP_WHEEL_NAME = f"pip-{PIP_VERSION}-py3-none-any.whl"
PIP_WHEEL_URL = (
    "https://files.pythonhosted.org/packages/f3/6e/1736e5b4ae2b778ef2f81c47d797de9f891d4d8acb047a24ca37a60294dd/"
    + PIP_WHEEL_NAME
)
PIP_WHEEL_SHA256 = "71138adf1f4ca900cdb7d289c21b7494329f2332b6d85f0e1c42108c0384ed3e"
PIP_WHEEL_SIZE = 1_816_632
DOWNLOAD_TIMEOUT = 30
_DISTRO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
_PIP_ENTRY = (
    "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module('pip',run_name='__main__')"
)
_CHECK_PIP = (
    "import pip,sys; from pathlib import Path; "
    "Path(pip.__file__).resolve().relative_to(Path(sys.prefix).resolve())"
)
_CHECK_VENV = (
    "import sys; from pathlib import Path; "
    "assert Path(sys.prefix).resolve()==Path(sys.argv[1]).resolve(); "
    "assert sys.prefix!=sys.base_prefix"
)
_PIP_FLAGS = ["--disable-pip-version-check", "--no-input"]


def _fail(stage: str) -> BridgeError:
    return BridgeError("bootstrap_failed", stage=stage)


def _venv_python(venv_dir: Path) -> Path:
    candidates = (venv_dir / "bin" / "python", venv_dir / "Scripts" / "python.exe")
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])


def _pip_wheel(cache_dir: Path) -> Path:
    """Return the verified pinned pip wheel, downloading only the constant HTTPS asset."""
    cache_dir = Path(cache_dir)
    destination = cache_dir / PIP_WHEEL_NAME
    try:
        if cache_dir.is_symlink():
            raise _fail("pip_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_file():
                raise _fail("pip_cache")
            _verify_pip(destination)
            return destination
        if not PIP_WHEEL_URL.startswith("https://files.pythonhosted.org/"):
            raise _fail("pip_download")
        request = urllib.request.Request(PIP_WHEEL_URL, headers={"User-Agent": "session-bridge-bootstrap"})
        temp_path: Path | None = None
        try:
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
                final_url = getattr(response, "url", PIP_WHEEL_URL)
                if not final_url.startswith("https://files.pythonhosted.org/"):
                    raise _fail("pip_download")
                header_size = response.headers.get("Content-Length")
                if header_size is not None and int(header_size) != PIP_WHEEL_SIZE:
                    raise _fail("pip_download")
                fd, raw_temp = tempfile.mkstemp(prefix="pip-bootstrap-", suffix=".tmp", dir=cache_dir)
                temp_path = Path(raw_temp)
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(fd, "wb") as output:
                    while block := response.read(64 * 1024):
                        size += len(block)
                        if size > PIP_WHEEL_SIZE:
                            raise _fail("pip_download")
                        digest.update(block)
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                if size != PIP_WHEEL_SIZE or digest.hexdigest() != PIP_WHEEL_SHA256:
                    raise _fail("pip_download")
            try:
                os.link(temp_path, destination)
            except FileExistsError:
                _verify_pip(destination)
            except OSError:
                # Some filesystems disallow hard links. Exclusive creation keeps
                # the fallback from replacing a path another installer owns.
                try:
                    with temp_path.open("rb") as source, destination.open("xb") as output:
                        shutil.copyfileobj(source, output)
                        output.flush()
                        os.fsync(output.fileno())
                except FileExistsError:
                    _verify_pip(destination)
                except OSError:
                    try:
                        destination.unlink()
                    except OSError:
                        pass
                    raise
            return destination
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
    except BridgeError:
        raise
    except (OSError, ValueError, urllib.error.URLError, TimeoutError):
        raise _fail("pip_download") from None


def _verify_pip(path: Path) -> None:
    try:
        if path.stat().st_size != PIP_WHEEL_SIZE:
            raise _fail("pip_cache")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(128 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != PIP_WHEEL_SHA256:
            raise _fail("pip_cache")
    except BridgeError:
        raise
    except OSError:
        raise _fail("pip_cache") from None


def _run(argv: list[str], stage: str) -> None:
    try:
        result = subprocess.run(argv, capture_output=True, check=False, timeout=300, shell=False)
    except (OSError, subprocess.TimeoutExpired):
        raise _fail(stage) from None
    if result.returncode != 0:
        raise _fail(stage)


def _has_pip(python: Path) -> bool:
    try:
        result = subprocess.run(
            [str(python), "-c", _CHECK_PIP], capture_output=True, check=False, timeout=10, shell=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def prepare_venv(venv_dir: str | os.PathLike[str]) -> Path:
    """Create/reuse a venv and ensure pip exists using only that venv's Python."""
    venv_path = Path(venv_dir).expanduser().absolute()
    if venv_path.is_symlink():
        raise _fail("venv_create")
    python = _venv_python(venv_path)
    try:
        if not python.is_file():
            venv.EnvBuilder(with_pip=False).create(venv_path)
            python = _venv_python(venv_path)
        if not python.is_file():
            raise _fail("venv_create")
    except BridgeError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise _fail("venv_create") from None
    # A normal Linux venv points its interpreter at the base Python. Isolation is established
    # by pyvenv.cfg and the runtime prefix, not by whether that interpreter path is a symlink.
    _run([str(python), "-c", _CHECK_VENV, str(venv_path)], "venv_identity")
    if not _has_pip(python):
        pip_wheel = _pip_wheel(venv_path.parent / "cache")
        _run(
            [
                str(python),
                "-c",
                _PIP_ENTRY,
                str(pip_wheel),
                "install",
                *_PIP_FLAGS,
                "--no-index",
                str(pip_wheel),
            ],
            "pip_bootstrap",
        )
    return python


def _requirements_lock() -> bytes:
    try:
        resource = importlib.resources.files("session_bridge").joinpath("resources", "requirements.lock")
        if not resource.is_file():
            raise _fail("requirements_lock")
        content = resource.read_bytes()
        if not content or len(content) > 1024 * 1024:
            raise _fail("requirements_lock")
        return content
    except BridgeError:
        raise
    except (OSError, TypeError, ValueError, ModuleNotFoundError):
        raise _fail("requirements_lock") from None


def _profile_version(tool: str, home: Path) -> str | None:
    package_paths = {
        "claude": (
            home / ".npm-global/lib/node_modules/@anthropic-ai/claude-code/package.json",
            home / ".local/share/pnpm/global/5/node_modules/@anthropic-ai/claude-code/package.json",
        ),
        "codex": (
            home / ".npm-global/lib/node_modules/@openai/codex/package.json",
            home / ".local/share/pnpm/global/5/node_modules/@openai/codex/package.json",
        ),
        "opencode": (
            home / ".npm-global/lib/node_modules/@opencode/cli/package.json",
            home / ".local/share/pnpm/global/5/node_modules/@opencode/cli/package.json",
            home / ".npm-global/lib/node_modules/@opencode-ai/opencode/package.json",
            home / ".local/share/pnpm/global/5/node_modules/@opencode-ai/opencode/package.json",
        ),
    }
    for path in package_paths[tool]:
        try:
            version = json.loads(path.read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(version, str):
            return version
    return None


def _install_launcher(launcher: Path, managed_target: Path) -> str:
    launcher.parent.mkdir(parents=True, exist_ok=True)
    if launcher.is_symlink():
        try:
            return "managed" if launcher.resolve() == managed_target.resolve() else "conflict"
        except OSError:
            return "conflict"
    if launcher.exists():
        return "conflict"
    try:
        launcher.symlink_to(managed_target)
        return "added"
    except FileExistsError:
        return "conflict"
    except OSError:
        return "unavailable"


def install_wsl(wheel: str | os.PathLike[str], distro: str) -> dict[str, Any]:
    """Install the supplied wheel into a dedicated rootless WSL virtualenv."""
    if not isinstance(distro, str) or not _DISTRO.fullmatch(distro):
        raise BridgeError("invalid_distro")
    wheel_path = Path(wheel)
    if (
        not wheel_path.is_absolute()
        or wheel_path.suffix.casefold() != ".whl"
        or not wheel_path.name.casefold().startswith(("session_bridge-", "session-bridge-"))
        or not wheel_path.is_file()
    ):
        raise BridgeError("invalid_wheel", "error.invalid_wheel.local")
    home = Path.home()
    root = home / ".local" / "share" / "session-bridge"
    venv_dir = root / "venv"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise _fail("install_root") from None
    if root.is_symlink():
        raise _fail("install_root")
    python = prepare_venv(venv_dir)
    lock_content = _requirements_lock()
    lock = root / "requirements.lock"
    if lock.is_symlink():
        raise _fail("requirements_lock")
    temp_name: str | None = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix="requirements-", suffix=".tmp", dir=root)
        with os.fdopen(fd, "wb") as output:
            output.write(lock_content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_name, lock)
    except OSError:
        raise _fail("requirements_lock") from None
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            except OSError:
                pass
    _run(
        [str(python), "-m", "pip", "install", *_PIP_FLAGS, "--requirement", str(lock)], "runtime_dependencies"
    )
    _run(
        [str(python), "-m", "pip", "install", *_PIP_FLAGS, "--force-reinstall", "--no-deps", str(wheel_path)],
        "session_bridge_wheel",
    )

    launcher_dir = home / ".local" / "bin"
    launcher = launcher_dir / "sessionbridge"
    managed_target = venv_dir / "bin" / "sessionbridge"
    try:
        path_status = _install_launcher(launcher, managed_target)
    except OSError:
        path_status = "unavailable"
    roots = {
        "claude": home / ".claude",
        "codex": home / ".codex",
        "opencode": home / ".local" / "share" / "opencode",
    }
    executables = {tool: shutil.which(tool) for tool in roots}
    profiles = [
        {
            "id": f"{tool}-{distro.lower().replace(' ', '-')}"[:80],
            "tool": tool,
            "environment": "wsl",
            "home": str(path),
            "distro": distro,
            "helper": str(python),
            "version": _profile_version(tool, home),
            "machine": f"wsl:{distro}",
            "executable": executables[tool],
        }
        for tool, path in roots.items()
        if path.is_dir()
    ]
    return {
        "helper": str(python),
        "home": str(home),
        "install_root": str(root),
        "launcher": str(launcher),
        "path_status": path_status,
        "on_path": str(launcher_dir) in os.environ.get("PATH", "").split(os.pathsep),
        "profiles": profiles,
    }


__all__ = ["install_wsl", "prepare_venv"]
