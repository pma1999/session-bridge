"""Separate, content-addressed SQLite storage for automatic conversation synchronization."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import BridgeError
from .models import canonical, digest, file_digest


class SyncStore:
    """Own sync.sqlite3 without opening or mutating native client stores."""

    SCHEMA_VERSION = 1
    MAX_BLOB_BYTES = 64 * 1024 * 1024
    MAX_CAPTURE_BYTES = 1024 * 1024 * 1024
    MAX_RELATED_DEPTH = 32
    REFERENCE_CHUNK = 256

    SCHEMA_OBJECTS = frozenset(
        {"links", "links_name_ci", "revisions", "revisions_link", "captures", "contexts", "blobs"}
    )

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "sync.sqlite3"
        if self.path.is_symlink():
            raise BridgeError("sync_store_unsafe", "El almacén de sincronización no es seguro.")
        try:
            with self._read_connection() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version > self.SCHEMA_VERSION:
                    raise BridgeError(
                        "sync_store_version", "El almacén pertenece a una versión más reciente."
                    )
                present = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
                if version == self.SCHEMA_VERSION and self.SCHEMA_OBJECTS <= present:
                    # Already current: opening needs no write, so a busy worker cannot block it.
                    return
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS links(
                        id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE, data TEXT NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS links_name_ci ON links(name COLLATE NOCASE);
                    CREATE TABLE IF NOT EXISTS revisions(
                        id TEXT PRIMARY KEY, link_id TEXT NOT NULL, data TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS revisions_link ON revisions(link_id);
                    CREATE TABLE IF NOT EXISTS captures(id TEXT PRIMARY KEY, manifest TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS contexts(id TEXT PRIMARY KEY, manifest TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS blobs(
                        hash TEXT PRIMARY KEY, raw_size INTEGER NOT NULL, data BLOB NOT NULL
                    );
                    PRAGMA user_version=1;
                    """
                )
        except BridgeError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise BridgeError("sync_store_invalid", "No se pudo abrir el almacén de sincronización.") from exc

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def lock(self):
        """Acquire a process-owned lock that the OS releases after a crash."""
        lockpath = self.root / "sync-operation.lock"
        if lockpath.is_symlink():
            raise BridgeError("sync_store_unsafe", "El bloqueo de sincronización no es seguro.")
        try:
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
                    raise BridgeError(
                        "sync_busy", "Ya hay una operación de sincronización en marcha."
                    ) from exc
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
        except BridgeError:
            raise
        except OSError as exc:
            raise BridgeError("sync_store_unsafe", "No se pudo usar el bloqueo de sincronización.") from exc

    @staticmethod
    def _json(value: Any) -> str:
        return canonical(value).decode("utf-8")

    @contextmanager
    def _write_db(self, db: sqlite3.Connection | None):
        if db is None:
            with self.transaction() as owned:
                yield owned
        else:
            yield db

    def links(self, *, include_unlinked: bool = False) -> list[dict]:
        try:
            with self._read_connection() as db:
                rows = db.execute("SELECT data FROM links ORDER BY name COLLATE NOCASE, id").fetchall()
            values = [json.loads(row[0]) for row in rows]
            return (
                values
                if include_unlinked
                else [value for value in values if not value.get("unlinked", False)]
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise BridgeError("sync_store_invalid", "No se pudieron leer los vínculos.") from exc

    def get_link(self, key: str) -> dict:
        try:
            with self._read_connection() as db:
                exact_id = db.execute("SELECT data FROM links WHERE id=?", (key,)).fetchone()
                if exact_id:
                    return json.loads(exact_id[0])
                exact_name = db.execute(
                    "SELECT data FROM links WHERE name=? COLLATE NOCASE", (key,)
                ).fetchone()
                if exact_name:
                    return json.loads(exact_name[0])
                rows = db.execute(
                    "SELECT id,data FROM links WHERE id LIKE ? ESCAPE '\\' ORDER BY id",
                    (key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
                ).fetchall()
            if len(rows) == 1:
                return json.loads(rows[0][1])
            if len(rows) > 1:
                raise BridgeError("sync_ambiguous", "El identificador coincide con varios vínculos.")
        except BridgeError:
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise BridgeError("sync_store_invalid", "No se pudo leer el vínculo.") from exc
        raise BridgeError("sync_link_not_found", "No existe ese vínculo.")

    def save_link(self, value: dict, *, db: sqlite3.Connection | None = None) -> None:
        if not isinstance(value, dict) or not value.get("id") or not value.get("name"):
            raise BridgeError("sync_link_invalid", "El vínculo no contiene identidad y nombre válidos.")
        with self._write_db(db) as conn:
            row = conn.execute("SELECT name FROM links WHERE id=?", (value["id"],)).fetchone()
            if row and row[0] != value["name"]:
                raise BridgeError("sync_link_conflict", "El vínculo ya tiene otro nombre.")
            try:
                conn.execute(
                    "INSERT INTO links(id,name,data) VALUES(?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                    (value["id"], value["name"], self._json(value)),
                )
            except sqlite3.IntegrityError as exc:
                raise BridgeError("sync_link_conflict", "Ese nombre ya pertenece a otro vínculo.") from exc

    def put_revision(self, value: dict, *, db: sqlite3.Connection | None = None) -> None:
        required = {"id", "link_id", "parent_id", "source_key", "capture", "context", "created_at", "reason"}
        if not isinstance(value, dict) or not required.issubset(value):
            raise BridgeError("sync_revision_invalid", "La revisión no contiene los campos obligatorios.")
        body = self._json(value)
        with self._write_db(db) as conn:
            row = conn.execute("SELECT data FROM revisions WHERE id=?", (value["id"],)).fetchone()
            if row:
                if row[0] != body:
                    raise BridgeError("sync_revision_conflict", "La revisión existente es inmutable.")
                return
            conn.execute(
                "INSERT INTO revisions(id,link_id,data) VALUES(?,?,?)", (value["id"], value["link_id"], body)
            )

    def get_revision(self, identifier: str, *, db: sqlite3.Connection | None = None) -> dict:
        try:
            if db is None:
                with self._read_connection() as conn:
                    return self.get_revision(identifier, db=conn)
            row = db.execute("SELECT data FROM revisions WHERE id=?", (identifier,)).fetchone()
            if row:
                return json.loads(row[0])
        except (sqlite3.Error, ValueError) as exc:
            raise BridgeError("sync_store_invalid", "No se pudo leer la revisión.") from exc
        raise BridgeError("sync_revision_not_found", "No existe esa revisión.")

    def revisions(self, link_id: str) -> list[dict]:
        try:
            with self._read_connection() as db:
                return [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT data FROM revisions WHERE link_id=? ORDER BY rowid", (link_id,)
                    )
                ]
        except (sqlite3.Error, ValueError) as exc:
            raise BridgeError("sync_store_invalid", "No se pudieron leer las revisiones.") from exc

    def _put_blob(self, raw: bytes, db: sqlite3.Connection) -> str:
        if len(raw) > self.MAX_BLOB_BYTES:
            raise BridgeError(
                "sync_capture_too_large", "Un elemento de la captura supera el límite permitido."
            )
        key = hashlib.sha256(raw).hexdigest()
        db.execute(
            "INSERT OR IGNORE INTO blobs(hash,raw_size,data) VALUES(?,?,?)",
            (key, len(raw), zlib.compress(raw, 6)),
        )
        return key

    @classmethod
    def _max_packed_blob_bytes(cls) -> int:
        """Use zlib's compressBound formula for the configured maximum raw payload."""
        size = cls.MAX_BLOB_BYTES
        return size + (size >> 12) + (size >> 14) + (size >> 25) + 13

    def _get_blob(self, key: str, db: sqlite3.Connection) -> bytes:
        # Keep metadata and payload reads on one SQLite snapshot, but don't transfer the BLOB
        # to Python until its declared raw size and stored compressed length pass our bounds.
        if not db.in_transaction:
            db.execute("BEGIN")
        row = db.execute("SELECT raw_size,length(data) FROM blobs WHERE hash=?", (key,)).fetchone()
        if row is None:
            raise BridgeError("sync_blob_invalid", "El contenido almacenado no está disponible.")
        size, packed_size = row
        if not isinstance(size, int) or size < 0 or size > self.MAX_BLOB_BYTES:
            raise BridgeError("sync_blob_invalid", "El contenido almacenado no es válido.")
        if not isinstance(packed_size, int) or packed_size < 0 or packed_size > self._max_packed_blob_bytes():
            raise BridgeError("sync_blob_invalid", "El contenido almacenado no es válido.")
        payload_row = db.execute("SELECT data FROM blobs WHERE hash=?", (key,)).fetchone()
        if payload_row is None or not isinstance(payload_row[0], bytes) or len(payload_row[0]) != packed_size:
            raise BridgeError("sync_blob_invalid", "El contenido almacenado no es válido.")
        packed = payload_row[0]
        try:
            decoder = zlib.decompressobj()
            raw = decoder.decompress(packed, self.MAX_BLOB_BYTES + 1)
            if (
                len(raw) > self.MAX_BLOB_BYTES
                or decoder.unconsumed_tail
                or decoder.unused_data
                or not decoder.eof
            ):
                raise ValueError
            raw += decoder.flush(self.MAX_BLOB_BYTES + 1 - len(raw))
            if len(raw) != size or hashlib.sha256(raw).hexdigest() != key:
                raise ValueError
            return raw
        except (ValueError, zlib.error, TypeError) as exc:
            raise BridgeError(
                "sync_blob_invalid", "El contenido almacenado no supera la verificación."
            ) from exc

    def _capture_manifest(self, value: dict, db: sqlite3.Connection, depth: int, budget: list[int]) -> dict:
        if depth > self.MAX_RELATED_DEPTH:
            raise BridgeError(
                "sync_capture_too_large", "La captura contiene demasiados niveles relacionados."
            )
        if not isinstance(value, dict):
            raise BridgeError("sync_capture_invalid", "La captura no tiene el formato esperado.")
        base = {
            key: item
            for key, item in value.items()
            if key not in {"events", "raw_records", "attachments", "related_conversations"}
        }
        budget[0] += len(canonical(base))
        if budget[0] > self.MAX_CAPTURE_BYTES:
            raise BridgeError("sync_capture_too_large", "La captura supera el límite permitido.")
        collections = {}
        for field in ("events", "raw_records", "attachments"):
            refs = []
            for item in value.get(field, []):
                body = dict(item) if isinstance(item, dict) else item
                if field == "attachments" and isinstance(body, dict) and body.get("data_base64") is not None:
                    data = body.pop("data_base64")
                    raw = canonical(data)
                    budget[0] += len(raw)
                    body["data_base64_ref"] = self._put_blob(raw, db)
                raw = canonical(body)
                budget[0] += len(raw)
                if budget[0] > self.MAX_CAPTURE_BYTES:
                    raise BridgeError("sync_capture_too_large", "La captura supera el límite permitido.")
                refs.append(self._put_blob(raw, db))
            ref_chunks = [
                refs[pos : pos + self.REFERENCE_CHUNK] for pos in range(0, len(refs), self.REFERENCE_CHUNK)
            ]
            collections[field] = [self._put_blob(canonical(chunk), db) for chunk in ref_chunks]
        related = [
            self._capture_manifest(item, db, depth + 1, budget)
            for item in value.get("related_conversations", [])
        ]
        base["_sync_collections"] = collections
        base["_sync_related"] = related
        return base

    def put_capture(self, conversation_dict: dict, *, db: sqlite3.Connection | None = None) -> str:
        if hasattr(conversation_dict, "model_dump"):
            conversation_dict = conversation_dict.model_dump(mode="json")
        with self._write_db(db) as conn:
            try:
                manifest = self._capture_manifest(conversation_dict, conn, 0, [0])
                body = self._json(manifest)
                key = digest(manifest)
                conn.execute("INSERT OR IGNORE INTO captures(id,manifest) VALUES(?,?)", (key, body))
                return key
            except BridgeError:
                raise
            except (TypeError, ValueError, sqlite3.Error) as exc:
                raise BridgeError("sync_capture_invalid", "No se pudo almacenar la captura.") from exc

    def _restore_capture(self, manifest: dict, db: sqlite3.Connection, depth: int, budget: list[int]) -> dict:
        if depth > self.MAX_RELATED_DEPTH:
            raise BridgeError("sync_blob_invalid", "La captura almacenada no es válida.")
        result = {
            key: value for key, value in manifest.items() if key not in {"_sync_collections", "_sync_related"}
        }
        budget[0] += len(canonical(result))
        if budget[0] > self.MAX_CAPTURE_BYTES:
            raise BridgeError("sync_blob_invalid", "La captura almacenada supera el límite permitido.")
        collections = manifest.get("_sync_collections")
        if not isinstance(collections, dict):
            raise BridgeError("sync_blob_invalid", "La captura almacenada no es válida.")
        for field in ("events", "raw_records", "attachments"):
            entries = []
            chunks = collections.get(field)
            if not isinstance(chunks, list):
                raise BridgeError("sync_blob_invalid", "La captura almacenada no es válida.")
            for chunk_ref in chunks:
                try:
                    chunk_raw = self._get_blob(chunk_ref, db)
                    budget[0] += len(chunk_raw)
                    if budget[0] > self.MAX_CAPTURE_BYTES:
                        raise BridgeError(
                            "sync_blob_invalid", "La captura almacenada supera el límite permitido."
                        )
                    chunk = json.loads(chunk_raw)
                except BridgeError:
                    raise
                except (UnicodeDecodeError, ValueError) as exc:
                    raise BridgeError("sync_blob_invalid", "La captura almacenada no es válida.") from exc
                if not isinstance(chunk, list) or len(chunk) > self.REFERENCE_CHUNK:
                    raise BridgeError("sync_blob_invalid", "La captura almacenada no es válida.")
                for key in chunk:
                    raw = self._get_blob(key, db)
                    budget[0] += len(raw)
                    if budget[0] > self.MAX_CAPTURE_BYTES:
                        raise BridgeError(
                            "sync_blob_invalid", "La captura almacenada supera el límite permitido."
                        )
                    try:
                        entries.append(json.loads(raw))
                    except (UnicodeDecodeError, ValueError) as exc:
                        raise BridgeError("sync_blob_invalid", "La captura almacenada no es válida.") from exc
            result[field] = entries
        for attachment in result["attachments"]:
            if "data_base64_ref" in attachment:
                ref = attachment.pop("data_base64_ref")
                try:
                    raw = self._get_blob(ref, db)
                    budget[0] += len(raw)
                    if budget[0] > self.MAX_CAPTURE_BYTES:
                        raise BridgeError(
                            "sync_blob_invalid", "La captura almacenada supera el límite permitido."
                        )
                    attachment["data_base64"] = json.loads(raw)
                except BridgeError:
                    raise
                except (UnicodeDecodeError, ValueError) as exc:
                    raise BridgeError("sync_blob_invalid", "El archivo almacenado no es válido.") from exc
        related = manifest.get("_sync_related", [])
        if not isinstance(related, list):
            raise BridgeError("sync_blob_invalid", "La captura almacenada no es válida.")
        result["related_conversations"] = [
            self._restore_capture(item, db, depth + 1, budget) for item in related
        ]
        return result

    def get_capture(self, identifier: str, *, db: sqlite3.Connection | None = None) -> dict:
        try:
            if db is None:
                with self._read_connection() as conn:
                    return self.get_capture(identifier, db=conn)
            row = db.execute("SELECT manifest FROM captures WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise BridgeError("sync_capture_not_found", "No existe esa captura.")
            manifest = json.loads(row[0])
            if digest(manifest) != identifier:
                raise BridgeError("sync_blob_invalid", "La captura almacenada no supera la verificación.")
            return self._restore_capture(manifest, db, 0, [0])
        except BridgeError:
            raise
        except (sqlite3.Error, UnicodeDecodeError, ValueError, TypeError) as exc:
            raise BridgeError("sync_blob_invalid", "La captura almacenada no se puede leer.") from exc

    def put_context(self, messages: list[dict], *, db: sqlite3.Connection | None = None) -> list[str]:
        with self._write_db(db) as conn:
            try:
                refs = []
                total = 0
                for message in messages:
                    raw = canonical(message)
                    total += len(raw)
                    if total > self.MAX_CAPTURE_BYTES:
                        raise BridgeError("sync_capture_too_large", "El contexto supera el límite permitido.")
                    refs.append(self._put_blob(raw, conn))
                ref_chunks = [
                    refs[pos : pos + self.REFERENCE_CHUNK]
                    for pos in range(0, len(refs), self.REFERENCE_CHUNK)
                ]
                chunks = [self._put_blob(canonical(chunk), conn) for chunk in ref_chunks]
                manifest = {"version": 1, "chunks": chunks}
                key = digest(manifest)
                conn.execute(
                    "INSERT OR IGNORE INTO contexts(id,manifest) VALUES(?,?)", (key, self._json(manifest))
                )
                return [key]
            except BridgeError:
                raise
            except (TypeError, ValueError, sqlite3.Error) as exc:
                raise BridgeError("sync_context_invalid", "No se pudo almacenar el contexto.") from exc

    def get_context(self, refs: list[str], *, db: sqlite3.Connection | None = None) -> list[dict]:
        try:
            if db is None:
                with self._read_connection() as conn:
                    return self.get_context(refs, db=conn)
            result = []
            budget = 0
            for ref in refs:
                row = db.execute("SELECT manifest FROM contexts WHERE id=?", (ref,)).fetchone()
                if row is None:
                    raise BridgeError("sync_context_not_found", "No existe ese contexto.")
                manifest = json.loads(row[0])
                if digest(manifest) != ref:
                    raise BridgeError(
                        "sync_blob_invalid", "El contexto almacenado no supera la verificación."
                    )
                for chunk_ref in manifest.get("chunks", []):
                    try:
                        chunk_raw = self._get_blob(chunk_ref, db)
                        budget += len(chunk_raw)
                        if budget > self.MAX_CAPTURE_BYTES:
                            raise BridgeError(
                                "sync_blob_invalid", "El contexto almacenado supera el límite permitido."
                            )
                        chunk = json.loads(chunk_raw)
                    except BridgeError:
                        raise
                    except (UnicodeDecodeError, ValueError) as exc:
                        raise BridgeError(
                            "sync_blob_invalid", "El contexto almacenado no es válido."
                        ) from exc
                    if not isinstance(chunk, list) or len(chunk) > self.REFERENCE_CHUNK:
                        raise BridgeError("sync_blob_invalid", "El contexto almacenado no es válido.")
                    for key in chunk:
                        raw = self._get_blob(key, db)
                        budget += len(raw)
                        if budget > self.MAX_CAPTURE_BYTES:
                            raise BridgeError(
                                "sync_blob_invalid", "El contexto almacenado supera el límite permitido."
                            )
                        result.append(json.loads(raw))
            return result
        except BridgeError:
            raise
        except (sqlite3.Error, UnicodeDecodeError, ValueError, TypeError) as exc:
            raise BridgeError("sync_blob_invalid", "El contexto almacenado no se puede leer.") from exc

    @classmethod
    def validate_backup(cls, path: Path) -> dict:
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise BridgeError("sync_backup_invalid", "La copia no es un archivo válido.")
        try:
            db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            try:
                integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
                version = db.execute("PRAGMA user_version").fetchone()[0]
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if (
                    integrity != "ok"
                    or version != cls.SCHEMA_VERSION
                    or not {"links", "revisions", "captures", "contexts", "blobs"}.issubset(tables)
                ):
                    raise BridgeError("sync_backup_invalid", "La copia no pasa la validación del almacén.")
                return {"path": str(path), "schema_version": version, "integrity": integrity}
            finally:
                db.close()
        except BridgeError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise BridgeError("sync_backup_invalid", "La copia no se puede validar.") from exc

    def backup(self, output: Path) -> dict:
        output = Path(output)
        if os.path.lexists(output):
            raise BridgeError("sync_backup_exists", "La ruta de copia ya existe.")
        temp: Path | None = None
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=".sync-backup-", suffix=".tmp", dir=output.parent)
            os.close(fd)
            temp = Path(temp_name)
            destination = sqlite3.connect(temp)
            try:
                with self._read_connection() as source:
                    source.backup(destination)
            finally:
                destination.close()
            with temp.open("r+b") as stream:
                os.fsync(stream.fileno())
            self.validate_backup(temp)
            sha = file_digest(temp)
            self._publish_exclusive(temp, output)
            temp = None
            self._fsync_directory(output.parent)
            return {"path": str(output), "sha256": sha}
        except BridgeError:
            if temp is not None:
                try:
                    temp.unlink()
                except OSError:
                    pass
            raise
        except (OSError, sqlite3.Error) as exc:
            if temp is not None:
                try:
                    temp.unlink()
                except OSError:
                    pass
            if isinstance(exc, FileExistsError):
                raise BridgeError("sync_backup_exists", "La ruta de copia ya existe.") from exc
            raise BridgeError("sync_backup_failed", "No se pudo completar la copia.") from exc

    @staticmethod
    def _publish_exclusive(temp: Path, destination: Path) -> None:
        """Atomically publish a same-directory file without replacing an existing path."""
        if os.name == "nt":
            # Windows rename fails when destination exists and is atomic within one volume.
            os.rename(temp, destination)
        else:
            os.link(temp, destination)
            temp.unlink()

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        try:
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass

    @staticmethod
    def _is_directory_alias(path: Path) -> bool:
        try:
            if path.is_symlink():
                return True
            metadata = path.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise BridgeError(
                "sync_restore_unsafe", "No se pudo validar la carpeta de restauración."
            ) from exc
        return bool(getattr(metadata, "st_file_attributes", 0) & 0x400)

    @classmethod
    def restore(cls, backup: Path, root: Path) -> SyncStore:
        cls.validate_backup(backup)
        root = Path(root)
        if cls._is_directory_alias(root):
            raise BridgeError("sync_restore_unsafe", "La carpeta de restauración no puede ser un alias.")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if cls._is_directory_alias(root):
            raise BridgeError("sync_restore_unsafe", "La carpeta de restauración no puede ser un alias.")
        if any(root.iterdir()):
            raise BridgeError("sync_restore_not_empty", "La restauración requiere una carpeta vacía.")
        temp: Path | None = None
        try:
            fd, temp_name = tempfile.mkstemp(prefix=".sync.sqlite3.restore-", dir=root)
            os.close(fd)
            temp = Path(temp_name)
            src = sqlite3.connect(f"{Path(backup).resolve().as_uri()}?mode=ro", uri=True)
            dst = sqlite3.connect(temp)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            cls.validate_backup(temp)
            if cls._is_directory_alias(root):
                raise BridgeError("sync_restore_unsafe", "La carpeta de restauración no puede ser un alias.")
            if any(entry != temp for entry in root.iterdir()):
                raise BridgeError("sync_restore_not_empty", "La restauración requiere una carpeta vacía.")
            cls._publish_exclusive(temp, root / "sync.sqlite3")
            temp = None
            cls._fsync_directory(root)
            return cls(root)
        except BridgeError:
            if temp is not None:
                try:
                    if not cls._is_directory_alias(root):
                        temp.unlink()
                except OSError:
                    pass
            raise
        except (OSError, sqlite3.Error) as exc:
            if temp is not None:
                try:
                    if not cls._is_directory_alias(root):
                        temp.unlink()
                except OSError:
                    pass
            raise BridgeError("sync_restore_failed", "No se pudo restaurar el almacén.") from exc
