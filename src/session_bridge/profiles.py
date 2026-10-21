"""Installation discovery without starting clients, distros, or network services."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
from pathlib import Path

from .errors import BridgeError
from .models import Profile


def data_home() -> Path:
    override = os.environ.get("SESSIONBRIDGE_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "SessionBridge"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "session-bridge"


def validate_profile(profile: Profile) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", profile.id):
        raise BridgeError("invalid_profile", "error.invalid_profile.identifier")
    if not profile.home or "\x00" in profile.home:
        raise BridgeError("invalid_profile", "error.invalid_profile.folder")
    if profile.environment == "wsl":
        if not profile.distro or not profile.helper or not profile.home.startswith("/"):
            raise BridgeError("invalid_profile", "error.invalid_profile.wsl_fields")
    elif not Path(profile.home).is_absolute():
        raise BridgeError("invalid_profile", "error.invalid_profile.folder_absolute")


def executable(tool: str) -> str | None:
    if sys.platform != "win32":
        return shutil.which(tool)
    npm = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "npm"
    if tool == "opencode":
        candidate = npm / "node_modules/@opencode/cli/bin/opencode.exe"
        if candidate.is_file():
            return str(candidate)
    elif tool == "codex":
        root = npm / "node_modules/@openai/codex"
        candidates = list(root.glob("node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe"))
        candidates += list(root.glob("vendor/*/bin/codex.exe"))
        if len(candidates) == 1:
            return str(candidates[0])
    candidate = Path.home() / ".local/bin" / (tool + ".exe")
    if candidate.is_file():
        return str(candidate)
    found = shutil.which(tool + ".exe")
    return found


def defaults() -> list[Profile]:
    home = Path.home()
    env = "windows" if sys.platform == "win32" else "posix"
    suffix = "win" if env == "windows" else "local"
    roots = {
        "claude": Path(os.environ.get("CLAUDE_CONFIG_DIR", str(home / ".claude"))),
        "codex": Path(os.environ.get("CODEX_HOME", str(home / ".codex"))),
        "opencode": Path(os.environ.get("XDG_DATA_HOME", str(home / ".local/share"))) / "opencode",
    }
    result = []
    for tool, root in roots.items():
        exe = executable(tool)
        version = None
        if exe and tool in {"codex", "opencode"}:
            npm = Path(os.environ.get("APPDATA", "")) / "npm/node_modules"
            package = npm / (
                "@openai/codex/package.json" if tool == "codex" else "@opencode/cli/package.json"
            )
            try:
                version = json.loads(package.read_text(encoding="utf-8"))["version"]
            except (OSError, ValueError, KeyError):
                pass
        if root.is_dir() or exe:
            result.append(
                Profile(
                    id=f"{tool}-{suffix}",
                    tool=tool,
                    environment=env,
                    home=str(root),
                    executable=exe,
                    version=version,
                    machine=socket.gethostname(),
                )
            )
    return result
