"""An exclusive lock on the first byte of a file: the operating system releases it if its owner dies."""

from __future__ import annotations

import os
from pathlib import Path


class ByteLock:
    def __init__(self, path: Path):
        self.path = path
        self.stream = None

    def acquire(self) -> bool:
        """Take the lock without waiting; False when another process holds it."""
        stream = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600), "r+b", buffering=0)
        try:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.stream = stream
        return True

    def owner(self, generation: str) -> None:
        """Record who holds the lock in the bytes after the locked one."""
        self.stream.seek(1)
        self.stream.write(generation.encode("ascii"))
        self.stream.truncate()
        os.fsync(self.stream.fileno())

    def release(self) -> None:
        if self.stream is None:
            return
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
            self.stream = None
