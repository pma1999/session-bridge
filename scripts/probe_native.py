"""Explicit, isolated native compatibility experiments. Never uses real auth/inference.

Uses client-specific storage directories and leaves HOME, USERPROFILE, APPDATA
and LOCALAPPDATA unchanged. Every run creates a new child under work/writers;
no existing profile/config is copied.
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import ClassVar

from session_bridge.models import Profile, ProjectedMessage, WriteRequest
from session_bridge.writers import claude, codex, opencode, registry

ROOT = Path(__file__).resolve().parents[1]
MARKERS = ["bridge_user_8ef3_áé中文", "bridge_assistant_91ac_áé中文"]


class MockProvider(http.server.BaseHTTPRequestHandler):
    requests: ClassVar[list[dict]] = []

    def log_message(self, *_):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "bridge-mock", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        self.requests.append({"path": self.path, "body": body})
        if "count_tokens" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"input_tokens":20}')
            return
        if "messages" in self.path:
            events = [
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": "msg_local_mock",
                            "type": "message",
                            "role": "assistant",
                            "model": body.get("model", "bridge-mock"),
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 20, "output_tokens": 0},
                        },
                    },
                ),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "local mock accepted"},
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 3},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
        elif "responses" in self.path:
            item = {
                "id": "msg_mock",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "local mock accepted", "annotations": []}],
            }
            response = {
                "id": "resp_mock",
                "object": "response",
                "created_at": int(time.time()),
                "status": "completed",
                "model": "bridge-mock",
                "output": [item],
                "usage": {"input_tokens": 20, "output_tokens": 3, "total_tokens": 23},
            }
            events = [
                (
                    "response.created",
                    {
                        "type": "response.created",
                        "response": {**response, "status": "in_progress", "output": []},
                    },
                ),
                (
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {**item, "status": "in_progress", "content": []},
                    },
                ),
                (
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "item_id": "msg_mock",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "local mock accepted",
                    },
                ),
                (
                    "response.output_item.done",
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                ),
                ("response.completed", {"type": "response.completed", "response": response}),
            ]
        else:
            self.send_error(400, "Only local Anthropic/Responses probes supported")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for name, event in events:
            self.wfile.write(f"event: {name}\ndata: {json.dumps(event)}\n\n".encode())
        self.wfile.flush()


@contextlib.contextmanager
def provider():
    MockProvider.requests = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def isolated_env(root, tool):
    # Whitelist platform execution essentials; inherited auth/provider/plugin vars
    # are excluded. These assignments affect child processes only.
    if tool not in {"claude", "opencode", "codex"}:
        raise RuntimeError("Client-only directory isolation not yet verified for this native client")
    if tool == "opencode" and Path(r"C:\ProgramData\opencode").exists():
        raise RuntimeError("Host managed OpenCode settings exist; refusing an isolated probe")
    if tool == "codex":
        actual_home = Path(os.environ.get("USERPROFILE", str(Path.home())))
        if any(
            p.exists()
            for p in [
                Path(r"C:\ProgramData\OpenAI\Codex"),
                actual_home / ".codex" / "managed_config.toml",
                actual_home / ".codex" / "requirements.toml",
            ]
        ):
            raise RuntimeError("Host managed Codex settings exist; refusing an isolated probe")
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
            "SYSTEMROOT",
            "WINDIR",
            "PATH",
            "PATHEXT",
            "COMSPEC",
            "PROGRAMFILES",
            "PROGRAMFILES(X86)",
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
        }
    }
    for key, suffix in {
        "XDG_DATA_HOME": "data",
        "XDG_CONFIG_HOME": "config",
        "XDG_CACHE_HOME": "cache",
        "XDG_STATE_HOME": "state",
        "TEMP": "tmp",
        "TMP": "tmp",
    }.items():
        path = root / suffix
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    env.update(
        {
            "CLAUDE_CONFIG_DIR": str(root / "claude"),
            "CODEX_HOME": str(root / "codex"),
            "CODEX_SQLITE_HOME": str(root / "codex-sqlite"),
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_CONFIG_DIR": str(root / "config" / "opencode"),
            "OPENCODE_TEST_HOME": str(root / "client-home"),
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
        }
    )
    (root / "client-home").mkdir(exist_ok=True)
    return env


def invoke(client, args, env, cwd, timeout=45):
    try:
        result = subprocess.run(
            [client, *args],
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return {
            "argv": [client, *args],
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": [client, *args],
            "timeout": timeout,
            "stdout": (exc.stdout or b"").decode("utf-8", errors="replace"),
            "stderr": (exc.stderr or b"").decode("utf-8", errors="replace"),
        }


def opencode_mock_config(base):
    return json.dumps(
        {
            "model": "anthropic/bridge-mock",
            "update": "disable",
            "share": "disabled",
            "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
            "plugins": [],
            "warming": False,
            "snapshots": False,
            "providers": {
                "anthropic": {
                    "settings": {"baseURL": base + "/v1", "apiKey": "fake-local-probe-only"},
                    "models": {
                        "bridge-mock": {
                            "name": "Local mock",
                            "capabilities": {"tools": False},
                            "limit": {"context": 200000, "output": 1024},
                        }
                    },
                }
            },
        }
    )


class Rpc:
    def __init__(self, client, env, cwd):
        self.proc = subprocess.Popen(
            [client, "app-server"],
            env=env,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
        self.output = queue.Queue()
        self.denied_requests = []
        self.seq = 0
        threading.Thread(target=self._read, daemon=True).start()
        self.call(
            "initialize",
            {
                "clientInfo": {"name": "session_bridge_probe", "version": "0.1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        self.send({"method": "initialized", "params": {}})

    def _read(self):
        for line in self.proc.stdout:
            try:
                value = json.loads(line)
                if "id" in value and "method" in value:
                    self.denied_requests.append(value["method"])
                    self.send(
                        {
                            "id": value["id"],
                            "error": {
                                "code": -32601,
                                "message": "All actions disabled in local compatibility probe",
                            },
                        }
                    )
                self.output.put(value)
            except ValueError:
                continue

    def send(self, data):
        self.proc.stdin.write(json.dumps(data) + "\n")
        self.proc.stdin.flush()

    def call(self, method, params):
        self.seq += 1
        self.send({"id": self.seq, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                result = self.output.get(timeout=deadline - time.monotonic())
            except queue.Empty:
                break
            if result.get("id") == self.seq:
                return result
        return {"error": {"message": "probe RPC timeout", "method": method}}

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.proc.terminate()  # Only the exact process created by this probe.
            self.proc.wait(timeout=5)


def probe(tool, client, root):
    root.mkdir(parents=True, exist_ok=False)
    cwd = root / "project"
    cwd.mkdir()
    env = isolated_env(root, tool)
    profile = Profile(
        id="probe",
        tool=tool,
        home=str(root / tool if tool != "opencode" else root / "data" / "opencode"),
        executable=client,
        version=registry.VERSIONS[tool],
    )
    request = WriteRequest(
        operation_id=str(uuid.uuid4()),
        profile=profile,
        native_id=registry.allocate_id(profile),
        cwd=str(cwd),
        title="Session Bridge local probe",
        messages=[
            ProjectedMessage(role="user", text=MARKERS[0]),
            ProjectedMessage(role="assistant", text=MARKERS[1]),
        ],
        model="bridge/bridge-mock",
    )
    report = {
        "tool": tool,
        "expected_version": profile.version,
        "root": str(root),
        "version": invoke(client, ["--version"], env, cwd),
        "native_id": request.native_id,
        "gates": {"persistence": False, "presentation": False, "resume": False, "model_context": False},
    }
    with provider() as base:
        if tool == "claude":
            receipt = claude.write(request)
            report["write_receipt"] = receipt.model_dump()
            env.update({"ANTHROPIC_API_KEY": "fake-local-probe-only", "ANTHROPIC_BASE_URL": base})
            args = [
                "--resume",
                request.native_id,
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
                "bridge_continue_no_tools",
            ]
            report["cold_resume"] = invoke(client, args, env, cwd)
            report["gates"]["persistence"] = all(
                marker in Path(receipt.locator).read_text(encoding="utf-8") for marker in MARKERS
            )
            report["gates"]["resume"] = report["cold_resume"].get("returncode") == 0
        elif tool == "opencode":
            # Export/import are real CLI operations. Each invocation exits before the
            # next starts, establishing a cold persistent read rather than memory reuse.
            data = opencode.transfer_data(request)
            payload = root / "transfer.json"
            payload.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            report["import"] = invoke(
                client, ["session", "import", str(payload), "--directory", str(cwd), "--standalone"], env, cwd
            )
            report["cold_export"] = invoke(
                client,
                ["session", "export", request.native_id, "--standalone"],
                env,
                cwd,
            )
            try:
                observed = json.loads(report["cold_export"]["stdout"])
                report["gates"]["persistence"] = opencode.projected_messages(observed) == [
                    (m.role, m.text) for m in request.messages
                ]
            except (ValueError, KeyError):
                pass
            report["run_help"] = invoke(client, ["run", "--help"], env, cwd)
            env["ANTHROPIC_API_KEY"] = "fake-local-probe-only"
            env["OPENCODE_CONFIG_CONTENT"] = opencode_mock_config(base)
            if report["gates"]["persistence"]:
                report["cold_resume"] = invoke(
                    client,
                    [
                        "run",
                        "--session",
                        request.native_id,
                        "--model",
                        "anthropic/bridge-mock",
                        "--standalone",
                        "--format",
                        "json",
                        "bridge_continue_no_tools",
                    ],
                    env,
                    cwd,
                    timeout=45,
                )
                report["gates"]["resume"] = report["cold_resume"].get("returncode") == 0 and bool(
                    MockProvider.requests
                )
            # Model config/resume needs the version's exact provider contract. Do not
            # improvise or fall back to a registered real provider if it is rejected.
            report["large_import"] = "not run until base import verifies exact readback"
            if report["gates"]["persistence"]:
                request.native_id = registry.allocate_id(profile)
                request.messages = [
                    ProjectedMessage(role="user" if i % 2 == 0 else "assistant", text=f"large_{i}")
                    for i in range(10_000)
                ]
                payload.write_text(json.dumps(opencode.transfer_data(request)), encoding="utf-8")
                started = time.monotonic()
                report["large_import"] = invoke(
                    client,
                    ["session", "import", str(payload), "--directory", str(cwd), "--standalone"],
                    env,
                    cwd,
                    timeout=90,
                )
                report["large_import"]["elapsed_seconds"] = round(time.monotonic() - started, 3)
                report["large_export"] = invoke(
                    client,
                    ["session", "export", request.native_id, "--standalone"],
                    env,
                    cwd,
                    timeout=90,
                )
        else:
            home = Path(profile.home)
            home.mkdir(parents=True, exist_ok=True)
            (home / "config.toml").write_text(
                'cli_auth_credentials_store = "file"\nmcp_oauth_credentials_store = "file"\n'
                "project_root_markers = []\n"
                f'model = "bridge-mock"\nmodel_provider = "bridge_mock"\n'
                f'[model_providers.bridge_mock]\nname = "Local mock"\nbase_url = "{base}/v1"\n'
                'wire_api = "responses"\nrequires_openai_auth = false\n',
                encoding="utf-8",
            )
            env["CODEX_SQLITE_HOME"] = str(home)
            # Production readback starts app-server itself. Give that owned process the
            # same explicit test environment; the operating-system environment is unchanged.
            original_environment = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(env)
                receipt = codex.write(request)
                report["write_receipt"] = receipt.model_dump()
                report["native_history_before_restart"] = codex.native_history(
                    profile, request.native_id, str(cwd)
                )
            finally:
                os.environ.clear()
                os.environ.update(original_environment)
            report["gates"]["persistence"] = codex.messages(profile, request.native_id) == [
                (m.role, m.text) for m in request.messages
            ]
            rpc = Rpc(client, env, cwd)
            try:
                report["cold_resume"] = rpc.call(
                    "thread/resume",
                    {"threadId": request.native_id, "approvalPolicy": "never", "sandbox": "read-only"},
                )
                report["history_after_restart"] = rpc.call(
                    "thread/read", {"threadId": request.native_id, "includeTurns": True}
                )
                shown = json.dumps(report["history_after_restart"], ensure_ascii=False)
                report["gates"]["presentation"] = all(marker in shown for marker in MARKERS)
                report["gates"]["resume"] = "result" in report["cold_resume"]
                report["turn"] = rpc.call(
                    "turn/start",
                    {
                        "threadId": request.native_id,
                        "input": [{"type": "text", "text": "bridge_continue_no_tools"}],
                    },
                )
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    try:
                        value = rpc.output.get(timeout=max(0.01, deadline - time.monotonic()))
                    except queue.Empty:
                        break
                    report.setdefault("notifications", []).append(value)
                    if value.get("method") == "turn/completed":
                        break
            finally:
                report["denied_requests_resumed"] = rpc.denied_requests
                rpc.close()
        report["provider_requests"] = MockProvider.requests
        report["gates"]["model_context"] = any(
            all(marker in json.dumps(r["body"], ensure_ascii=False) for marker in MARKERS)
            for r in MockProvider.requests
        )
    report["native_eligible"] = all(report["gates"].values())
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=["claude", "opencode", "codex"])
    parser.add_argument("--client", required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument(
        "--capacity-counts", nargs="+", type=int, help="Synthetic OpenCode native-import boundary probes only"
    )
    parser.add_argument(
        "--present-only",
        action="store_true",
        help="Open the exact synthetic run in its native TUI; enter no prompt, Ctrl+C to close",
    )
    args = parser.parse_args()
    root = (args.root or ROOT / "work" / "writers" / f"{args.tool}-{uuid.uuid4().hex[:10]}").resolve()
    if not root.is_relative_to((ROOT / "work" / "writers").resolve()):
        parser.error("root must be strictly under work/writers")
    if args.present_only:
        if not (root / "report.json").is_file():
            parser.error("presentation requires an existing synthetic probe report")
        report = json.loads((root / "report.json").read_text(encoding="utf-8"))
        native_id = report["native_id"]
        if args.tool in {"claude", "codex"}:
            uuid.UUID(native_id)
        env = isolated_env(root, args.tool)
        env.update({"ANTHROPIC_API_KEY": "fake-local-probe-only", "ANTHROPIC_BASE_URL": "http://127.0.0.1:9"})
        argv = [
            args.client,
            "--resume",
            native_id,
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
            "--ax-screen-reader",
        ]
        if args.tool == "opencode":
            env["OPENCODE_CONFIG_CONTENT"] = opencode_mock_config("http://127.0.0.1:9")
            argv = [
                args.client,
                "mini",
                "--session",
                native_id,
                "--standalone",
                "--replay",
                "--model",
                "anthropic/bridge-mock",
            ]
        if args.tool == "codex":
            env["CODEX_SQLITE_HOME"] = str(root / "codex")
            argv = [
                args.client,
                "--cd",
                str(root / "project"),
                "--sandbox",
                "read-only",
                "--ask-for-approval",
                "never",
                "resume",
                native_id,
            ]
        report["presentation_command"] = argv
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        proc = subprocess.Popen(argv, env=env, cwd=root / "project")
        try:
            proc.wait()
        except KeyboardInterrupt:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
        return
    if root.exists():
        parser.error("root must be a new directory strictly under work/writers")
    if args.capacity_counts:
        if args.tool != "opencode" or any(n < 1 or n > 100000 for n in args.capacity_counts):
            parser.error("capacity probes require OpenCode and positive bounded synthetic counts")
        root.mkdir(parents=True)
        cwd = root / "project"
        cwd.mkdir()
        env = isolated_env(root, "opencode")
        profile = Profile(
            id="probe",
            tool="opencode",
            home=str(root / "data" / "opencode"),
            version=opencode.VERSION,
            executable=args.client,
        )
        results = []
        for count in args.capacity_counts:
            request = WriteRequest(
                operation_id=str(uuid.uuid4()),
                profile=profile,
                native_id=registry.allocate_id(profile),
                cwd=str(cwd),
                title=f"Capacity {count}",
                messages=[
                    ProjectedMessage(role="user" if i % 2 == 0 else "assistant", text=f"capacity_{i}")
                    for i in range(count)
                ],
            )
            path = root / f"capacity-{count}.json"
            path.write_text(json.dumps(opencode.transfer_data(request)), encoding="utf-8")
            started = time.monotonic()
            imported = invoke(
                args.client,
                ["session", "import", str(path), "--directory", str(cwd), "--standalone"],
                env,
                cwd,
                timeout=90,
            )
            exported = invoke(
                args.client, ["session", "export", request.native_id, "--standalone"], env, cwd, timeout=90
            )
            try:
                data = json.loads(exported["stdout"])
                exact = opencode.projected_messages(data) == [(m.role, m.text) for m in request.messages]
                observed_count = len(data["messages"])
            except (KeyError, ValueError):
                exact, observed_count = False, 0
            results.append(
                {
                    "count": count,
                    "import": imported,
                    "export": {k: v for k, v in exported.items() if k != "stdout"},
                    "observed_count": observed_count,
                    "exact_readback": exact,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            )
        log = root / "data" / "opencode" / "log" / "opencode.log"
        causes = (
            sorted(
                set(
                    re.findall(
                        r"SQLite query expected \d+ values, received \d+", log.read_text(encoding="utf-8")
                    )
                )
            )
            if log.exists()
            else []
        )
        (root / "capacity-report.json").write_text(
            json.dumps({"results": results, "native_causes": causes}, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "report": str(root / "capacity-report.json"),
                    "results": [
                        {k: v for k, v in r.items() if k not in {"import", "export"}} for r in results
                    ],
                    "native_causes": causes,
                }
            )
        )
        return
    result = probe(args.tool, args.client, root)
    print(
        json.dumps(
            {
                "report": str(root / "report.json"),
                "gates": result["gates"],
                "native_eligible": result["native_eligible"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
