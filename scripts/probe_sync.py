"""Isolated Windows sync acceptance with native clients and a local-only mock.

No real session, credential, configuration or WSL distribution is accessed. The
existing native probe supplies isolation, provider and invocation boundaries.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from itertools import permutations
from pathlib import Path

from probe_native import MockProvider, invoke, isolated_env, opencode_mock_config, provider

from session_bridge import sync_daemon
from session_bridge.errors import BridgeError
from session_bridge.models import Conversation, Event, Profile, SessionRef
from session_bridge.profiles import executable
from session_bridge.service import Bridge
from session_bridge.sync import SyncService
from session_bridge.sync_store import SyncStore
from session_bridge.writers import registry

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="Isolated native Windows sync acceptance")
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    root = (args.root or ROOT / "work" / "sync" / uuid.uuid4().hex[:10]).resolve()
    if not root.is_relative_to((ROOT / "work").resolve()):
        raise RuntimeError("Probe root must be a new directory under project work/")
    root.mkdir(parents=True, exist_ok=False)
    project = root / "project with spaces á"
    project.mkdir()
    clients = {tool: executable(tool) for tool in ("claude", "opencode", "codex")}
    if not all(clients.values()):
        raise RuntimeError("All three installed Windows clients are required")
    # Run both managed-config preflights. No common home variable is changed.
    isolated_env(root, "codex")
    env = isolated_env(root, "opencode")
    env["SESSIONBRIDGE_HOME"] = str(root / "bridge")
    env["PYTHONPATH"] = str(ROOT / "src")
    previous_environment = os.environ.copy()
    report = {"root": str(root), "directions": [], "paid_inference_requests": 0, "wsl_invocations": 0}
    managed_started = False
    try:
        with provider() as base:
            env["OPENCODE_CONFIG_CONTENT"] = opencode_mock_config(base)
            (root / "codex").mkdir(exist_ok=True)
            (root / "codex/config.toml").write_text(
                'cli_auth_credentials_store = "file"\nmcp_oauth_credentials_store = "file"\n'
                'project_root_markers = []\nmodel = "bridge-mock"\nmodel_provider = "bridge_mock"\n'
                f'[model_providers.bridge_mock]\nname = "Local mock"\nbase_url = "{base}/v1"\n'
                'wire_api = "responses"\nrequires_openai_auth = false\n',
                encoding="utf-8",
            )
            os.environ.clear()
            os.environ.update(env)
            bridge = Bridge(root / "bridge")
            profiles = {}
            for tool in clients:
                profile = Profile(
                    id="sync-" + tool,
                    tool=tool,
                    version=registry.VERSIONS[tool],
                    home=str(root / tool if tool != "opencode" else root / "data/opencode"),
                    executable=clients[tool],
                )
                profiles[tool] = bridge.register_profile(profile)
            synthetic = Conversation(
                source=SessionRef(
                    profile_id="synthetic",
                    native_id=str(uuid.uuid4()),
                    tool="claude",
                    cwd=str(project),
                    title="Isolated sync fixture",
                ),
                events=[
                    Event(id="u", kind="message", role="user", text="sync_user_á中文"),
                    Event(id="a", kind="message", role="assistant", text="sync_answer_á中文"),
                ],
                raw_records=[{"synthetic_fixture": True}],
                source_fingerprint="synthetic-fixture",
            )
            originals, original_fingerprints, links = {}, {}, {}
            service = SyncService(bridge=bridge)
            for tool, profile in profiles.items():
                transfer = bridge._convert(synthetic, profile.id)
                originals[tool] = profile.id + ":" + transfer.native_id
                original_fingerprints[tool] = registry.inspect(profile, transfer.native_id).fingerprint
                links[tool] = service.link(originals[tool], name="continuity-" + tool)
                assert links[tool]["state"] == "synced", links[tool]["state"]
            for source, target in permutations(profiles, 2):
                started = time.monotonic()
                result = service.open(links[source]["id"], profiles[target].id, launch=False)
                repeated = service.open(links[source]["id"], profiles[target].id, launch=False)
                assert repeated["native_id"] == result["native_id"]
                assert result["native_id"] in result["argv"]
                assert not result["launched"]
                report["directions"].append(
                    {
                        "from": source,
                        "to": target,
                        "native_id": result["native_id"],
                        "same_id_retry": True,
                        "seconds": round(time.monotonic() - started, 3),
                    }
                )
            for tool, key in originals.items():
                assert (
                    registry.inspect(profiles[tool], key.split(":", 1)[1]).fingerprint
                    == original_fingerprints[tool]
                )
            assert not MockProvider.requests, "Conversion/synchronization must not start inference"
            report["source_unchanged_during_six_conversions"] = True
            report["six_conversions_inference_requests"] = 0

            report["worker_start"] = sync_daemon.start(root / "bridge")
            managed_started = True
            assert sync_daemon.start(root / "bridge")["started"] is False
            # Actual Claude persistence/turn completion, no tools and only localhost.
            local_env = dict(env, ANTHROPIC_API_KEY="fake-local-probe-only", ANTHROPIC_BASE_URL=base)
            continued = invoke(
                clients["claude"],
                [
                    "--resume",
                    originals["claude"].split(":", 1)[1],
                    "--bare",
                    "--disable-slash-commands",
                    "--tools",
                    "",
                    "--strict-mcp-config",
                    "--mcp-config",
                    '{"mcpServers":{}}',
                    "--setting-sources",
                    "",
                    "--model",
                    "bridge-mock",
                    "--output-format",
                    "json",
                    "-p",
                    "sync_continue_no_tools",
                ],
                local_env,
                project,
            )
            (root / "local-continuation.json").write_text(json.dumps(continued, indent=2), encoding="utf-8")
            assert continued.get("returncode") == 0, "Local-only Claude continuation failed"
            deadline = time.monotonic() + 25
            current = None
            while time.monotonic() < deadline:
                current = service.status(links["claude"]["id"])
                if (
                    len(current["heads"]) == 1
                    and current["heads"][0]["id"] != links["claude"]["heads"][0]["id"]
                ):
                    break
                time.sleep(0.1)
            assert current is not None and current["heads"][0]["id"] != links["claude"]["heads"][0]["id"], (
                current
            )
            updated = service.open(links["claude"]["id"], profiles["codex"].id, launch=False)
            observed = bridge.show(profiles["codex"].id + ":" + updated["native_id"], snapshot=True)
            active = [event.text for event in observed.events if event.active and event.kind == "message"]
            assert active == [
                "sync_user_á中文",
                "sync_answer_á中文",
                "sync_continue_no_tools",
                "local mock accepted",
            ]
            report["background_real_client_continuation"] = {
                "captured": True,
                "message_count": len(active),
                "native_codex_id": updated["native_id"],
            }
            reopened = SyncService(root / "bridge")
            assert (
                reopened.status(links["claude"]["id"])["heads"]
                == service.status(links["claude"]["id"])["heads"]
            )
            backup = service.backup(root / "history-backup.sqlite3")
            restored = SyncStore.restore(Path(backup["path"]), root / "restored")
            saved = restored.get_link(links["claude"]["id"])
            revision = restored.get_revision(saved["heads"][0])
            assert len(restored.get_context(revision["context"])) == 4
            report["restart_and_backup_restore"] = True
            report["local_mock_requests"] = len(MockProvider.requests)
            report["worker_stop"] = sync_daemon.stop(root / "bridge")
            managed_started = False
            assert report["worker_stop"]["acknowledged"]
            report["worker_restart"] = sync_daemon.start(root / "bridge")
            managed_started = True
            assert report["worker_restart"]["started"]
            report["worker_stop_after_restart"] = sync_daemon.stop(root / "bridge")
            managed_started = False
    finally:
        if managed_started:
            report["worker_cleanup"] = sync_daemon.stop(root / "bridge")
        os.environ.clear()
        os.environ.update(previous_environment)
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except BridgeError as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        raise SystemExit(1) from None
