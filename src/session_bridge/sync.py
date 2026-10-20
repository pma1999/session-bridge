"""Revision-based local conversation continuity; native sessions are never rewritten."""

from __future__ import annotations

import time
import uuid
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path

from .archive import write_package
from .errors import BridgeError
from .models import Conversation, Event, Profile, digest, utc_now
from .projection import project
from .service import Bridge
from .sync_capture import checkpoint
from .sync_store import SyncStore

RECONCILE_SECONDS = 30.0


def _signature(messages):
    return [(message["role"], message["text"]) for message in messages]


def _projected(conversation):
    return [message.model_dump(mode="json") for message in project(conversation).messages]


class SyncService:
    """One serialized revision graph, explicitly linked Windows endpoints, no inference."""

    def __init__(self, data_dir: Path | None = None, *, bridge=None):
        self.bridge = bridge or Bridge(data_dir)
        self.data_dir = self.bridge.data_dir
        self.store = SyncStore(self.data_dir)

    @contextmanager
    def _operation(self):
        """Foreground work waits briefly for a capture; the worker itself never spins."""
        deadline = time.monotonic() + 10.0
        with ExitStack() as stack:
            while True:
                try:
                    stack.enter_context(self.store.lock())
                    break
                except BridgeError as exc:
                    if exc.code != "sync_busy" or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            yield

    def _profile(self, identifier):
        profile = self.bridge._profile(identifier)
        if profile.environment != "windows":
            raise BridgeError(
                "sync_windows_only", "La sincronización está habilitada solo para perfiles Windows."
            )
        return profile

    def _source(self, key):
        if ":" not in key:
            raise BridgeError("sync_source_required", "Indica el origen completo como perfil:identificador.")
        self._profile(key.split(":", 1)[0])  # Refuse WSL before any discovery/read.
        conversation = self.bridge.show(key, snapshot=True)
        self._profile(conversation.source.profile_id)
        return conversation

    def _read(self, endpoint):
        current = self._profile(endpoint["profile"]["id"])
        if current.model_dump(mode="json") != endpoint["profile"]:
            raise BridgeError(
                "sync_profile_changed",
                "El perfil cambió desde que se vinculó; revisa el vínculo antes de continuar.",
            )
        conversation = self.bridge.show(endpoint["key"], snapshot=True)
        if conversation.source.key != endpoint["key"]:
            raise BridgeError("sync_source_changed", "El lector no devolvió el identificador vinculado.")
        return self.bridge._capture_tree(conversation, snapshot=True)

    def _endpoint(self, conversation, profile, revision=None):
        return {
            "key": conversation.source.key,
            "profile_id": profile.id,
            "profile": profile.model_dump(mode="json"),
            "locator": conversation.source.locator,
            "revision_id": revision,
            "state": "waiting",
            "pending": False,
            "error": None,
            "capture": None,
            "observed_signature": None,
            "last_full_check": 0.0,
            "enabled": True,
            "cwd": conversation.source.cwd,
            "model": None,
        }

    def link(self, key: str, *, name=None, transfer_id=None):
        if name is not None and (not name.strip() or len(name) > 120 or any(ord(c) < 32 for c in name)):
            raise BridgeError("sync_name_invalid", "El nombre necesita entre 1 y 120 caracteres visibles.")
        with self._operation():
            conversation = self._source(key)
            profile = self._profile(conversation.source.profile_id)
            existing = [
                link
                for link in self.store.links()
                if any(endpoint["key"] == conversation.source.key for endpoint in link["endpoints"])
            ]
            if existing:
                link = existing[0]
                if name and name.casefold() != link["name"].casefold():
                    raise BridgeError(
                        "sync_already_linked", "La sesión ya pertenece a un vínculo con otro nombre."
                    )
            else:
                link_id = "ln_" + uuid.uuid4().hex
                link = {
                    "id": link_id,
                    "name": name.strip() if name else link_id,
                    "heads": [],
                    "selected_head": None,
                    "endpoints": [],
                    "paused": False,
                    "unlinked": False,
                    "last_checked": None,
                    "last_error": None,
                    "created_at": utc_now(),
                    "materializations": {},
                }
                endpoint = self._endpoint(conversation, profile)
                link["endpoints"].append(endpoint)
                if transfer_id:
                    transfer = self.bridge.store.get(transfer_id)
                    archived, projection = self.bridge._archived_context(transfer)
                    if transfer.source_key != conversation.source.key:
                        raise BridgeError("sync_transfer_mismatch", "El traspaso pertenece a otro origen.")
                    # Explicit conversion is a user-selected historical snapshot. Preserve
                    # that exact baseline, even if the live source is already continuing.
                    self._record(
                        link,
                        endpoint,
                        archived,
                        [m.model_dump(mode="json") for m in projection.messages],
                        pending=False,
                        reason="explicit_transfer",
                    )
                else:
                    self._observe(link, endpoint, self.bridge._capture_tree(conversation, snapshot=True))
            if transfer_id:
                self._attach_transfer(link, transfer_id)
            self.store.save_link(link)
            return self._status(link)

    def _attach_transfer(self, link, transfer_id):
        transfer = self.bridge.store.get(transfer_id)
        if transfer.state != "completed" or transfer.mode != "native" or not transfer.receipt:
            raise BridgeError(
                "sync_transfer_unverified", "Solo se vinculan conversiones nativas verificadas."
            )
        profile = self._profile(transfer.target_profile)
        if transfer.target and transfer.target.model_dump(mode="json") != profile.model_dump(mode="json"):
            raise BridgeError("sync_profile_changed", "El destino del traspaso ya no coincide con el perfil.")
        archived, projection = self.bridge._archived_context(transfer)
        expected = [(m.role, m.text) for m in projection.messages]
        revisions = [
            revision
            for revision in self.store.revisions(link["id"])
            if revision["source_key"] == transfer.source_key
            and _signature(self.store.get_context(revision["context"])) == expected
        ]
        if not revisions:
            raise BridgeError(
                "sync_transfer_context", "El traspaso no corresponde a una revisión conocida del vínculo."
            )
        key = profile.id + ":" + transfer.native_id
        if any(endpoint["key"] == key for endpoint in link["endpoints"]):
            return
        receipt = self.bridge._writer(profile).inspect(profile, transfer.native_id)
        if (
            receipt is None
            or receipt.evidence.get("operation_ids") != [transfer.id]
            or not receipt.evidence.get("owned")
        ):
            raise BridgeError(
                "sync_target_unverified", "El destino no acredita pertenecer al traspaso verificado."
            )
        ref = self.bridge.show(key, snapshot=True)
        endpoint = self._endpoint(ref, profile, revisions[-1]["id"])
        endpoint.update(
            state="synced",
            transfer_id=transfer.id,
            cwd=transfer.cwd,
            model=transfer.context_policy.get("model"),
        )
        link["endpoints"].append(endpoint)

    def _record(self, link, endpoint, conversation, messages, *, pending, reason=None):
        parent = endpoint["revision_id"]
        previous = self.store.get_context(self.store.get_revision(parent)["context"]) if parent else []
        old, new = _signature(previous), _signature(messages)
        with self.store.transaction() as db:
            capture_id = self.store.put_capture(conversation.model_dump(mode="json"), db=db)
            endpoint.update(
                capture=capture_id, pending=pending, state="waiting" if pending else "synced", error=None
            )
            if old == new or (pending and parent and new == old[: len(new)]):
                self.store.save_link(link, db=db)
                return False
            common = 0
            while common < min(len(old), len(new)) and old[common] == new[common]:
                common += 1
            context = previous[:common]
            for index, message in enumerate(messages[common:], common):
                origin = {"key": endpoint["key"], "event_ids": message.get("source_ids", [])}
                context.append(
                    {
                        "id": "ev_" + digest([origin, index, message["role"], message["text"]]),
                        "role": message["role"],
                        "text": message["text"],
                        "origin": origin,
                    }
                )
            kind = reason or (
                "initial"
                if not parent
                else "append"
                if common == len(old)
                else "rewind"
                if common == len(new)
                else "context_replacement"
            )
            revision_id = "rv_" + digest([link["id"], parent, endpoint["key"], [m["id"] for m in context]])
            revision = {
                "id": revision_id,
                "link_id": link["id"],
                "parent_id": parent,
                "source_key": endpoint["key"],
                "capture": capture_id,
                "context": self.store.put_context(context, db=db),
                "created_at": utc_now(),
                "reason": kind,
            }
            self.store.put_revision(revision, db=db)
            was_head = parent in link["heads"]
            link["heads"] = [head for head in link["heads"] if head != parent] + [revision_id]
            if link["selected_head"] == parent and was_head:
                link["selected_head"] = revision_id
            elif parent and not was_head:
                # A new continuation from a stale endpoint creates a genuine divergence.
                link["selected_head"] = None
            endpoint["revision_id"] = revision_id
            self.store.save_link(link, db=db)
        return True

    def _observe(self, link, endpoint, conversation):
        try:
            closed = checkpoint(conversation)
        except BridgeError as exc:
            with self.store.transaction() as db:
                endpoint["capture"] = self.store.put_capture(conversation.model_dump(mode="json"), db=db)
                endpoint.update(pending=True, state="waiting", error=exc.code)
                self.store.save_link(link, db=db)
            if exc.code == "sync_pending_turn":
                return False
            raise
        return self._record(
            link, endpoint, conversation, _projected(closed.conversation), pending=closed.pending
        )

    def _file_signature(self, endpoint):
        profile = Profile.model_validate(endpoint["profile"])
        paths = []
        if endpoint.get("locator"):
            path = Path(endpoint["locator"])
            paths += [path, Path(str(path) + ".zst")]
        if profile.tool in {"opencode", "codex"}:
            names = (
                ["opencode.db"]
                if profile.tool == "opencode"
                else ["state_5.sqlite", "thread_history_1.sqlite"]
            )
            for name in names:
                path = Path(profile.home) / name
                paths += [path, Path(str(path) + "-wal")]
        result = []
        for path in paths:
            try:
                stat = path.stat()
                result.append([str(path), stat.st_size, stat.st_mtime_ns, stat.st_ino])
            except OSError:
                result.append([str(path), None])
        return result

    def _poll_locked(self, link, *, force=False):
        if link["paused"] or link["unlinked"]:
            return 0, []
        changed, errors = 0, []
        for endpoint_key in [
            endpoint["key"] for endpoint in link["endpoints"] if endpoint.get("enabled", True)
        ]:
            endpoint = next(item for item in link["endpoints"] if item["key"] == endpoint_key)
            now = time.time()
            signature = self._file_signature(endpoint)
            if (
                not force
                and signature == endpoint.get("observed_signature")
                and now - endpoint["last_full_check"] < RECONCILE_SECONDS
            ):
                continue
            try:
                # A failed SQLite transaction must not leak staged heads/capture IDs
                # back into the last valid in-memory graph before the next save.
                staged = deepcopy(link)
                staged_endpoint = next(item for item in staged["endpoints"] if item["key"] == endpoint_key)
                conversation = self._read(staged_endpoint)
                delta = self._observe(staged, staged_endpoint, conversation)
                link.clear()
                link.update(staged)
                endpoint = next(item for item in link["endpoints"] if item["key"] == endpoint_key)
                changed += int(delta)
                endpoint["locator"] = conversation.source.locator
            except BridgeError as exc:
                endpoint.update(state="error", error=exc.code)
                errors.append(
                    {
                        "link_id": link["id"],
                        "code": exc.code,
                        "message": "No se pudo actualizar uno de los orígenes; se conserva la revisión anterior.",
                    }
                )
            except (OSError, ValueError):
                endpoint.update(state="error", error="sync_source_unreadable")
                errors.append(
                    {
                        "link_id": link["id"],
                        "code": "sync_source_unreadable",
                        "message": "No se pudo capturar el origen vinculado.",
                    }
                )
            endpoint["observed_signature"] = signature
            endpoint["last_full_check"] = now
        link["last_checked"] = utc_now()
        link["last_error"] = (
            errors[0]["code"]
            if errors
            else next(
                (
                    endpoint["error"]
                    for endpoint in link["endpoints"]
                    if endpoint.get("enabled", True) and endpoint.get("error")
                ),
                None,
            )
        )
        self.store.save_link(link)
        return changed, errors

    def poll(self, link: str | None = None, *, force=False):
        with self.store.lock():
            links = [self.store.get_link(link)] if link is not None else self.store.links()
            changed, errors = 0, []
            for value in links:
                delta, issues = self._poll_locked(value, force=force)
                changed += delta
                errors.extend(issues)
            return {"changed": changed, "links": [self._status(value) for value in links], "errors": errors}

    def _status(self, link):
        heads = [self.store.get_revision(head) for head in link["heads"]]
        enabled = [endpoint for endpoint in link["endpoints"] if endpoint.get("enabled", True)]
        state = (
            "unlinked"
            if link["unlinked"]
            else "paused"
            if link["paused"]
            else "conflict"
            if len(heads) > 1 and not link["selected_head"]
            else "error"
            if any(endpoint["state"] == "error" for endpoint in enabled)
            else "waiting"
            if not heads or any(endpoint["pending"] for endpoint in enabled)
            else "synced"
        )
        return {
            "id": link["id"],
            "name": link["name"],
            "state": state,
            "paused": link["paused"],
            "heads": [
                {key: head[key] for key in ("id", "source_key", "created_at", "reason")} for head in heads
            ],
            "selected_head": link["selected_head"],
            "last_checked": link["last_checked"],
            "last_error": link["last_error"],
            "endpoints": [
                {
                    key: endpoint.get(key)
                    for key in (
                        "key",
                        "profile_id",
                        "revision_id",
                        "state",
                        "error",
                        "pending",
                        "enabled",
                        "cwd",
                    )
                }
                for endpoint in link["endpoints"]
            ],
        }

    def list(self):
        return [self._status(link) for link in self.store.links()]

    def status(self, link):
        return self._status(self.store.get_link(link))

    def _choose(self, link, branch):
        if branch is not None:
            matches = [head for head in link["heads"] if head == branch or head.startswith(branch)]
            if len(matches) != 1:
                raise BridgeError(
                    "sync_branch_required", "Elige una revisión de rama única del estado del vínculo."
                )
            return matches[0]
        if link["selected_head"] in link["heads"]:
            return link["selected_head"]
        if len(link["heads"]) != 1:
            raise BridgeError(
                "sync_conflict" if link["heads"] else "sync_pending_turn",
                "Hay varias ramas: elige --branch o sync select."
                if link["heads"]
                else "El seguimiento espera un primer turno terminado.",
            )
        return link["heads"][0]

    def _conversation(self, revision, link):
        conversation = Conversation.model_validate(self.store.get_capture(revision["capture"]))
        # Original native data, reasoning, tool calls and branches stay archival. Only
        # the selected canonical, already adapted text becomes new destination history.
        for event in conversation.events:
            event.active = False
        context = self.store.get_context(revision["context"])
        conversation.events += [
            Event(
                id=message["id"],
                kind="message",
                role=message["role"],
                text=message["text"],
                native={"session_bridge_origin": message["origin"]},
            )
            for message in context
        ]
        conversation.context_ambiguous = False
        conversation.source.title = link["name"]
        conversation.source_fingerprint = digest([conversation.source_fingerprint, revision["id"]])
        return conversation

    def open(self, link, target, *, branch=None, cwd=None, model=None, launch=True, new_copy=False):
        with self._operation():
            value = self.store.get_link(link)
            if value["paused"] or value["unlinked"]:
                raise BridgeError(
                    "sync_paused", "Reanuda el vínculo antes de abrir una revisión sincronizada."
                )
            _, errors = self._poll_locked(value, force=True)
            revision_id = self._choose(value, branch)
            if branch is None and (
                errors
                or any(
                    endpoint["pending"] or endpoint.get("error")
                    for endpoint in value["endpoints"]
                    if endpoint.get("enabled", True)
                )
            ):
                raise BridgeError(
                    "sync_not_current",
                    "Hay un origen pendiente o no verificable. Espera o elige explícitamente --branch para abrir una instantánea.",
                )
            profile = self._profile(target)
            revision = self.store.get_revision(revision_id)
            conversation = self._conversation(revision, value)
            # Pin cwd/profile/model in the intention before any native publication.
            profile, _, directory, cap = self.bridge._prepare(conversation, target, cwd=cwd, model=model)
            if not cap.native:
                raise BridgeError("native_unavailable", cap.reason)
            candidates = [
                endpoint
                for endpoint in value["endpoints"]
                if endpoint["profile"] == profile.model_dump(mode="json")
                and endpoint["revision_id"] == revision_id
                and not endpoint["pending"]
                and not endpoint["error"]
                and endpoint.get("transfer_id")
                and endpoint.get("enabled", True)
                and endpoint.get("cwd", directory) == directory
                and endpoint.get("model") == model
            ]
            resume_key = None
            if candidates and not new_copy:
                transfer_id = candidates[-1]["transfer_id"]
                resume_key = transfer_id
                try:
                    self.bridge.store.get(transfer_id)
                except BridgeError as exc:
                    if exc.code != "transfer_not_found":
                        raise
                    # A history-only restore has no old transfer journal. The exact
                    # native mapping remains usable only with its operation marker.
                    native_id = candidates[-1]["key"].split(":", 1)[1]
                    receipt = self.bridge._writer(profile).inspect(profile, native_id)
                    if (
                        not receipt
                        or receipt.evidence.get("operation_ids") != [transfer_id]
                        or not receipt.evidence.get("owned")
                    ):
                        raise BridgeError(
                            "sync_target_unverified", "El destino restaurado no acredita su procedencia."
                        )
                    resume_key = candidates[-1]["key"]
            else:
                intent_key = digest([revision_id, profile.model_dump(mode="json"), directory, model])
                intent = None if new_copy else value["materializations"].get(intent_key)
                if new_copy:
                    intent_key += "_" + uuid.uuid4().hex
                if intent and intent["state"] == "completed":
                    # Its old native history has since been continued on another branch.
                    intent_key += "_" + uuid.uuid4().hex
                    intent = None
                if intent is None:
                    intent = {
                        "transfer_id": "tr_" + uuid.uuid4().hex,
                        "revision_id": revision_id,
                        "state": "prepared",
                        "profile": profile.model_dump(mode="json"),
                        "cwd": directory,
                        "model": model,
                    }
                    value["materializations"][intent_key] = intent
                    self.store.save_link(value)
                transfer_id = intent["transfer_id"]
                try:
                    transfer = self.bridge.store.get(transfer_id)
                except BridgeError as exc:
                    if exc.code != "transfer_not_found":
                        raise
                    transfer = None
                if transfer is None:
                    transfer = self.bridge._convert(
                        conversation, target, cwd=directory, model=model, operation_id=transfer_id
                    )
                elif transfer.state != "completed":
                    transfer = self.bridge.recover(transfer.id)
                if transfer.state != "completed" or transfer.mode != "native":
                    raise BridgeError(
                        "sync_recovery_required", "La publicación necesita recuperación antes de abrirse."
                    )
                native_key = profile.id + ":" + transfer.native_id
                native_capture = self.bridge.show(native_key, snapshot=True)
                endpoint = self._endpoint(native_capture, profile, revision_id)
                endpoint.update(transfer_id=transfer.id, cwd=directory, model=model, state="synced")
                if not any(old["key"] == native_key for old in value["endpoints"]):
                    value["endpoints"].append(endpoint)
                intent.update(state="completed", transfer_id=transfer.id)
                self.store.save_link(value)
                advanced = self._observe(value, endpoint, native_capture)
                self.store.save_link(value)
                if advanced or endpoint["pending"]:
                    raise BridgeError(
                        "sync_target_advanced",
                        "El destino continuó durante la recuperación. Su nueva revisión se conserva; consulta sync status y vuelve a abrirla.",
                    )
                transfer_id = transfer.id
            resume_key = resume_key or transfer_id
            result = self.bridge.resume(resume_key, launch=False)
            result.update(
                link_id=value["id"],
                revision_id=revision_id,
                transfer_id=transfer_id,
                launched=False,
                snapshot=branch is not None,
            )
        # Native interactive execution may block until the client exits. Background
        # captures must remain able to obtain the sync lock throughout that time.
        if launch:
            launched = self.bridge.resume(resume_key, launch=True)
            result.update(launched)
            result["launched"] = True
        return result

    def _toggle(self, link, **fields):
        with self._operation():
            value = self.store.get_link(link)
            value.update(fields)
            self.store.save_link(value)
            return self._status(value)

    def pause(self, link):
        return self._toggle(link, paused=True)

    def unpause(self, link):
        value = self.store.get_link(link)
        if value["unlinked"]:
            raise BridgeError("sync_unlinked", "El vínculo está desvinculado; sus archivos se conservan.")
        return self._toggle(link, paused=False)

    def unlink(self, link):
        return self._toggle(link, unlinked=True, paused=True)

    def detach(self, link, key):
        """Retire an old endpoint without deleting its archive, branch or native session."""
        with self._operation():
            value = self.store.get_link(link)
            endpoints = [endpoint for endpoint in value["endpoints"] if endpoint["key"] == key]
            if len(endpoints) != 1:
                raise BridgeError(
                    "sync_endpoint_not_found", "Elige el identificador completo mostrado por sync status."
                )
            endpoint = endpoints[0]
            if (
                endpoint.get("enabled", True)
                and sum(item.get("enabled", True) for item in value["endpoints"]) <= 1
            ):
                raise BridgeError(
                    "sync_last_endpoint", "Es el último origen; usa sync unlink para detener todo el vínculo."
                )
            endpoint.update(enabled=False, state="detached", pending=False, error=None)
            value["last_error"] = next(
                (
                    item.get("error")
                    for item in value["endpoints"]
                    if item.get("enabled", True) and item.get("error")
                ),
                None,
            )
            self.store.save_link(value)
            return self._status(value)

    def lookup(self, key):
        """Only exact qualified session IDs/own transfer IDs may redirect an old resume."""
        matches = [
            (value, endpoint)
            for value in self.store.links()
            for endpoint in value["endpoints"]
            if endpoint.get("enabled", True)
            and (endpoint["key"] == key or endpoint.get("transfer_id") == key)
        ]
        if len(matches) > 1:
            raise BridgeError("sync_ambiguous", "La sesión figura en varios vínculos; utiliza sync open.")
        return (matches[0][0]["id"], matches[0][1]["profile_id"]) if matches else None

    def select(self, link, branch):
        with self._operation():
            value = self.store.get_link(link)
            value["selected_head"] = self._choose(value, branch)
            self.store.save_link(value)
            return self._status(value)

    def export(self, link, output, *, branch=None):
        with self._operation():
            value = self.store.get_link(link)
            revision = self.store.get_revision(self._choose(value, branch))
            result = write_package(self._conversation(revision, value), Path(output))
            return {"link_id": value["id"], "revision_id": revision["id"], **result}

    def backup(self, output):
        with self._operation():
            if any(
                intent["state"] != "completed"
                for link in self.store.links(include_unlinked=True)
                for intent in link["materializations"].values()
            ):
                raise BridgeError(
                    "sync_recovery_required",
                    "Recupera las publicaciones pendientes antes de copiar el archivo de sincronización.",
                )
            return self.store.backup(Path(output))
