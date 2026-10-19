"""Outside-in local format tests; handoff evidence is not a native-provider claim."""

import itertools
import json
import sqlite3
import uuid

import pytest

from session_bridge.archive import read_package
from session_bridge.models import Profile, file_digest
from session_bridge.service import Bridge

TOOLS = ["claude", "opencode", "codex"]
USER_TEXT = "Continue the feature áé 中文 🚀"
ASSISTANT_TEXT = "Current implementation is ready for tests."


def seed(root, tool, cwd):
    root.mkdir(parents=True)
    native_id = "ses_" + uuid.uuid4().hex if tool == "opencode" else str(uuid.uuid4())
    profile = Profile(id=tool + "-fixture", tool=tool, home=str(root), version="fixture")
    if tool == "claude":
        file = root / "projects/project" / (native_id + ".jsonl")
        file.parent.mkdir(parents=True)
        file.write_text(
            "\n".join(
                json.dumps(row, ensure_ascii=False)
                for row in [
                    {
                        "type": "user",
                        "uuid": "u",
                        "parentUuid": None,
                        "sessionId": native_id,
                        "cwd": str(cwd),
                        "message": {"role": "user", "content": USER_TEXT},
                    },
                    {
                        "type": "assistant",
                        "uuid": "a",
                        "parentUuid": "u",
                        "sessionId": native_id,
                        "cwd": str(cwd),
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": ASSISTANT_TEXT}],
                        },
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )
    elif tool == "codex":
        file = root / "sessions/2026" / (native_id + ".jsonl")
        file.parent.mkdir(parents=True)
        file.write_text(
            "\n".join(
                json.dumps(row, ensure_ascii=False)
                for row in [
                    {"type": "session_meta", "payload": {"id": native_id, "cwd": str(cwd)}},
                    {
                        "ordinal": 1,
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": USER_TEXT}],
                        },
                    },
                    {
                        "ordinal": 2,
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": ASSISTANT_TEXT}],
                        },
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )
    else:
        file = root / "opencode.db"
        with sqlite3.connect(file) as db:
            db.executescript("""
                CREATE TABLE session_v2(id TEXT PRIMARY KEY,title TEXT,directory TEXT,parent_id TEXT,
                    time_created INT,time_updated INT,time_archived INT,revert TEXT);
                CREATE TABLE session_message(id TEXT,session_id TEXT,type TEXT,seq INT,
                    time_created INT,time_updated INT,data TEXT);
                CREATE TABLE account(secret TEXT);
                INSERT INTO account VALUES('SECRET_NOT_SESSION');
            """)
            db.execute(
                "INSERT INTO session_v2 VALUES(?,?,?,?,?,?,?,?)",
                (native_id, "Fixture", str(cwd), None, 1, 2, None, None),
            )
            for n, role, body in [
                (1, "user", {"text": USER_TEXT}),
                (
                    2,
                    "assistant",
                    {"content": [{"type": "text", "text": ASSISTANT_TEXT}], "time": {"completed": 2}},
                ),
            ]:
                db.execute(
                    "INSERT INTO session_message VALUES(?,?,?,?,?,?,?)",
                    (f"m{n}", native_id, role, n, n, n, json.dumps(body)),
                )
    return profile, f"{profile.id}:{native_id}", file


@pytest.mark.parametrize("source,target", list(itertools.permutations(TOOLS, 2)))
def test_all_six_explicit_handoffs_through_native_readers(tmp_path, monkeypatch, source, target):
    cwd = tmp_path / "project with spaces á"
    cwd.mkdir()
    p, key, file = seed(tmp_path / source, source, cwd)
    target_root = tmp_path / target
    target_root.mkdir()
    t = Profile(id=target + "-target", tool=target, home=str(target_root))
    bridge = Bridge(tmp_path / "data")
    monkeypatch.setattr(bridge, "profiles", lambda: [p, t])
    original = file_digest(file)
    preview = bridge.preview(key, t.id, mode="handoff")
    assert preview["message_count"] == 2
    transfer = bridge.convert(key, t.id, mode="handoff")
    assert transfer.state == "prepared" and transfer.native_id is None
    text = __import__("pathlib").Path(transfer.context_file).read_text(encoding="utf-8")
    assert USER_TEXT in text and ASSISTANT_TEXT in text
    captured = read_package(__import__("pathlib").Path(transfer.archive))
    assert captured.source.key == key
    assert "SECRET_NOT_SESSION" not in captured.model_dump_json()
    assert file_digest(file) == original
    assert bridge.convert(key, t.id, mode="handoff").id == transfer.id
    imported = bridge.import_package(__import__("pathlib").Path(transfer.archive), t.id, mode="handoff")
    assert imported.id == transfer.id
