"""Check the newest Claude Code, Codex and OpenCode against the native writers (Windows only).

Each client's latest npm release, or the version given with ``--version tool=X.Y.Z``, is installed
into a new folder under ``work/clients``, and ``scripts/probe_native.py`` runs against it with its
isolated storage and local mock model. The gates that run unattended are reported: persistence,
resume and model context. Presentation stays a manual check (``probe_native.py --present-only``).

Exit status 1 means a client changed in a way the writers do not handle yet, or a version the gate
does not accept: native writing needs work before the next release. Otherwise no release is needed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

from session_bridge.writers import registry

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "claude": ("@anthropic-ai/claude-code", "@anthropic-ai/claude-code/bin/claude.exe"),
    "codex": ("@openai/codex", "@openai/codex*/vendor/*/bin/codex.exe"),
    "opencode": ("@opencode/cli", "@opencode/cli/bin/opencode.exe"),
}
UNATTENDED = ("persistence", "resume", "model_context")


def install(prefix: Path, specs: list[str]) -> None:
    prefix.mkdir(parents=True, exist_ok=False)
    subprocess.run(
        ["npm.cmd", "install", "--prefix", str(prefix), "--no-audit", "--no-fund", *specs], check=True
    )


def client(prefix: Path, pattern: str) -> Path:
    found = sorted((prefix / "node_modules").glob(pattern))
    if not found:
        raise SystemExit(f"client executable not found: {pattern}")
    return found[0]


def probe(tool: str, executable: Path) -> dict:
    root = ROOT / "work" / "writers" / f"watch-{tool}-{uuid.uuid4().hex[:10]}"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "probe_native.py"), tool, "--client", str(executable), "--root", str(root)],
        check=False,
        timeout=900,
    )
    report = root / "report.json"
    return json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {"gates": {}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="append", default=[], metavar="TOOL=X.Y.Z")
    parser.add_argument("--summary", type=Path, help="Also write the Markdown result to this file")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("native writing is verified on Windows only")
    pinned = dict(item.split("=", 1) for item in args.version)
    prefix = ROOT / "work" / "clients" / uuid.uuid4().hex[:10]
    install(prefix, [f"{PACKAGES[tool][0]}@{pinned.get(tool, 'latest')}" for tool in PACKAGES])
    lines = ["| Client | Version | Gate | Persistence | Resume | Model context |", "| --- | --- | --- | --- | --- | --- |"]
    failed = False
    for tool, (_, pattern) in PACKAGES.items():
        executable = client(prefix, pattern)
        output = subprocess.run([str(executable), "--version"], capture_output=True, text=True, check=False)
        version = (output.stdout.strip().splitlines() or ["unknown"])[0]
        state = registry.status(tool, version) or "refused"
        gates = probe(tool, executable)["gates"]
        results = [bool(gates.get(name)) for name in UNATTENDED]
        failed |= state == "refused" or not all(results)
        marks = " | ".join("pass" if ok else "**fail**" for ok in results)
        lines.append(f"| {tool} | {version} | {state} | {marks} |")
    text = "\n".join(lines) + "\n"
    print(text)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as stream:
            stream.write(text)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
