"""Own SQLite journal: never opens or restores a coding client's global database."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .errors import BridgeError
from .models import Profile, Transfer


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "bridge.sqlite3"
        if self.path.is_symlink():
            raise BridgeError("unsafe_store", "error.unsafe_store.journal")
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise BridgeError("store_version")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS transfers(
                    id TEXT PRIMARY KEY, identity TEXT NOT NULL UNIQUE, state TEXT NOT NULL, data TEXT NOT NULL
                );
                PRAGMA user_version=1;
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def lock(self):
        """OS lock releases on crash; persistent file itself is not proof of a live owner."""
        lockpath = self.root / "operation.lock"
        if lockpath.is_symlink():
            raise BridgeError("unsafe_store", "error.unsafe_store.lock")
        with lockpath.open("a+b") as stream:
            if lockpath.stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise BridgeError("operation_busy") from exc
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def profiles(self) -> list[Profile]:
        with self.connect() as db:
            return [
                Profile.model_validate_json(row[0])
                for row in db.execute("SELECT data FROM profiles ORDER BY id")
            ]

    def set_profile(self, profile: Profile):
        with self.connect() as db:
            db.execute(
                "INSERT INTO profiles VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (profile.id, profile.model_dump_json()),
            )

    def transfers(self) -> list[Transfer]:
        with self.connect() as db:
            return [
                Transfer.model_validate_json(row[0])
                for row in db.execute("SELECT data FROM transfers ORDER BY rowid DESC")
            ]

    def find(self, identity: str) -> Transfer | None:
        with self.connect() as db:
            row = db.execute("SELECT data FROM transfers WHERE identity=?", (identity,)).fetchone()
            return Transfer.model_validate_json(row[0]) if row else None

    def get(self, identifier: str) -> Transfer:
        with self.connect() as db:
            row = db.execute("SELECT data FROM transfers WHERE id=?", (identifier,)).fetchone()
            if row:
                return Transfer.model_validate_json(row[0])
        raise BridgeError("transfer_not_found", transfer_id=identifier)

    def save(self, transfer: Transfer):
        with self.connect() as db:
            db.execute(
                "INSERT INTO transfers VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "state=excluded.state,data=excluded.data",
                (transfer.id, transfer.identity, transfer.state, transfer.model_dump_json()),
            )
