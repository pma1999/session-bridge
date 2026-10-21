"""Small, content-free subprocess and receipt boundaries."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from session_bridge.errors import BridgeError


def validate_id(tool, native_id):
    pattern = (
        r"ses_[a-zA-Z0-9]+" if tool == "opencode" else r"[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}"
    )
    if not re.fullmatch(pattern, native_id):
        raise BridgeError("invalid_native_id")


def env_for_profile(profile):
    env = os.environ.copy()
    if profile.tool == "claude":
        env["CLAUDE_CONFIG_DIR"] = profile.home
    elif profile.tool == "codex":
        env["CODEX_HOME"] = profile.home
        env["CODEX_SQLITE_HOME"] = profile.home
    else:
        env["XDG_DATA_HOME"] = str(Path(profile.home).parent)
    return env


def executable(profile):
    if not profile.executable:
        raise BridgeError("client_missing")
    return profile.executable


def run(profile, args, *, cwd, timeout=90):
    try:
        return subprocess.run(
            [executable(profile), *args],
            cwd=cwd,
            env=env_for_profile(profile),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise BridgeError("native_timeout", "error.native_timeout.client") from None
    except OSError:
        raise BridgeError("client_unavailable") from None


def check_version(profile, expected, *, cwd):
    result = run(profile, ["--version"], cwd=cwd, timeout=15)
    found = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", result.stdout)
    if result.returncode or not found or found.group(1) != expected:
        raise BridgeError("native_version_changed")


def detect_version(profile):
    """No inference; standalone version lookup, using the selected profile env."""
    directory = Path(profile.home).parent
    result = run(
        profile, ["--version"], cwd=str(directory) if directory.is_dir() else os.getcwd(), timeout=15
    )
    found = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", result.stdout)
    return found.group(1) if result.returncode == 0 and found else None
