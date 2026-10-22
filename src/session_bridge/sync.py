"""Revision-based local conversation continuity; native sessions are never rewritten."""

from __future__ import annotations

import time
import uuid
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path

from . import history, i18n
from .archive import write_package
from .catalog import same_directory
from .errors import BridgeError
from .models import Conversation, Event, Profile, digest, utc_now
from .projection import project
from .service import Bridge
from .sync_capture import checkpoint
from .sync_store import SyncStore
from .ui import visible

RECONCILE_SECONDS = 30.0
# Earlier sessions are watched by their own file, and fully at most this often.
SUPERSEDED_SECONDS = 300.0
SHARED_FILES = {"opencode.db", "state_5.sqlite", "thread_history_1.sqlite"}
PREVIEW_CHARS = 80


def _signature(messages):
    return [(message["role"], message["text"]) for message in messages]


def _projected(conversation):
    return [message.model_dump(mode="json") for message in project(conversation).messages]


def _roles(link):
    """Each assistant's current session is its last enabled endpoint; earlier ones are superseded.

    Order is the only stored state: opening a session moves it to the end. Sessions that
    Session Bridge created carry a transfer ID; the rest are the user's original sessions.
    """
    latest = {
        endpoint["profile_id"]: endpoint["key"]
        for endpoint in link["endpoints"]
        if endpoint.get("enabled", True)
    }
    return {
        endpoint["key"]: (
            "detached"
            if not endpoint.get("enabled", True)
            else "current"
            if latest.get(endpoint["profile_id"]) == endpoint["key"]
            else "superseded",
            "copy" if endpoint.get("transfer_id") else "original",
        )
        for endpoint in link["endpoints"]
    }


def _promote(link, key):
    """Make an endpoint its assistant's current session."""
    endpoints = link["endpoints"]
    index = next(i for i, endpoint in enumerate(endpoints) if endpoint["key"] == key)
    endpoints.append(endpoints.pop(index))



def same_profile(first, second):
    """Whether two recorded profiles name the same installation; an assistant update keeps it."""
    return {k: v for k, v in first.items() if k != "version"} == {k: v for k, v in second.items() if k != "version"}

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
            raise BridgeError("sync_windows_only")
        return profile

    def _source(self, key):
        if ":" not in key:
            raise BridgeError("sync_source_required")
        self._profile(key.split(":", 1)[0])  # Refuse WSL before any discovery/read.
        conversation = self.bridge.show(key, snapshot=True)
        self._profile(conversation.source.profile_id)
        return conversation

    def _read(self, endpoint):
        current = self._profile(endpoint["profile"]["id"])
        if not same_profile(current.model_dump(mode="json"), endpoint["profile"]):
            raise BridgeError(
                "sync_profile_changed",
                "error.sync_profile_changed.profile",
            )
        conversation = self.bridge.show(endpoint["key"], snapshot=True)
        if conversation.source.key != endpoint["key"]:
            raise BridgeError("sync_source_changed")
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
            raise BridgeError("sync_name_invalid")
        if transfer_id and self.bridge.store.get(transfer_id).context_policy.get("variant"):
            # Before anything is saved: the link would follow a version the source no longer continues.
            raise BridgeError(
                "sync_variant_unsupported",
            )
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
                    raise BridgeError("sync_already_linked")
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
                        raise BridgeError("sync_transfer_mismatch")
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
            raise BridgeError("sync_transfer_unverified")
        profile = self._profile(transfer.target_profile)
        if transfer.target and not same_profile(transfer.target.model_dump(mode="json"), profile.model_dump(mode="json")):
            raise BridgeError("sync_profile_changed", "error.sync_profile_changed.target")
        archived, projection = self.bridge._archived_context(transfer)
        expected = [(m.role, m.text) for m in projection.messages]
        revisions = [
            revision
            for revision in self.store.revisions(link["id"])
            if revision["source_key"] == transfer.source_key
            and _signature(self.store.get_context(revision["context"])) == expected
        ]
        if not revisions:
            raise BridgeError("sync_transfer_context")
        key = profile.id + ":" + transfer.native_id
        if any(endpoint["key"] == key for endpoint in link["endpoints"]):
            return
        receipt = self.bridge._writer(profile).inspect(profile, transfer.native_id)
        if (
            receipt is None
            or receipt.evidence.get("operation_ids") != [transfer.id]
            or not receipt.evidence.get("owned")
        ):
            raise BridgeError("sync_target_unverified", "error.sync_target_unverified.transfer")
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
            elif parent:
                # A new continuation from a stale endpoint, or new turns in a continuation that
                # was not chosen, is fresh work elsewhere: ask again rather than ignore it.
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

    def _file_signature(self, endpoint, *, own=False):
        """Stat data for change detection; ``own`` keeps only the session's own file, if any."""
        profile = Profile.model_validate(endpoint["profile"])
        paths = []
        if endpoint.get("locator") and not (own and Path(endpoint["locator"]).name in SHARED_FILES):
            path = Path(endpoint["locator"])
            paths += [path, Path(str(path) + ".zst")]
        if own:
            if not paths:
                return None
        elif profile.tool in {"opencode", "codex"}:
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
        roles = _roles(link)
        for endpoint_key in [
            endpoint["key"] for endpoint in link["endpoints"] if endpoint.get("enabled", True)
        ]:
            endpoint = next(item for item in link["endpoints"] if item["key"] == endpoint_key)
            now = time.time()
            age = now - endpoint["last_full_check"]
            if roles[endpoint_key][0] == "superseded":
                # An earlier session is read when its own file changes, or every few minutes,
                # so activity elsewhere in a shared database never re-reads it. An explicit check
                # also reads one without a file of its own, or one whose last read failed or was
                # pending. A turn typed in it is still captured as its own continuation.
                signature = self._file_signature(endpoint, own=True)
                unhealthy = bool(endpoint.get("error") or endpoint.get("pending"))
                unchanged = not (force and (signature is None or unhealthy)) and (
                    signature is None or signature == endpoint.get("observed_signature")
                )
                if age < SUPERSEDED_SECONDS and unchanged:
                    continue
            else:
                signature = self._file_signature(endpoint)
                if not force and signature == endpoint.get("observed_signature") and age < RECONCILE_SECONDS:
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
                        "message": i18n.t("sync.endpoint_failed"),
                    }
                )
            except (OSError, ValueError):
                endpoint.update(state="error", error="sync_source_unreadable")
                errors.append(
                    {
                        "link_id": link["id"],
                        "code": "sync_source_unreadable",
                        "message": i18n.t("error.sync_source_unreadable"),
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
        roles = _roles(link)
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
                    **{
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
                    },
                    "tool": (endpoint.get("profile") or {}).get("tool"),
                    "role": roles[endpoint["key"]][0],
                    "origin": roles[endpoint["key"]][1],
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
            if not matches:
                # An assistant's profile ID names the continuation made in it, when there is one.
                matches = [
                    head
                    for head in link["heads"]
                    if self.store.get_revision(head)["source_key"].split(":", 1)[0] == branch
                ]
                if len(matches) > 1:
                    raise BridgeError(
                        "sync_branch_required",
                        "error.sync_branch_required.assistant",
                    )
            if len(matches) != 1:
                raise BridgeError(
                    "sync_branch_required",
                    "error.sync_branch_required.choose",
                )
            return matches[0]
        if link["selected_head"] in link["heads"]:
            return link["selected_head"]
        if len(link["heads"]) != 1:
            if link["heads"]:
                raise BridgeError("sync_conflict")
            raise BridgeError("sync_pending_turn", "error.sync_pending_turn.first_turn")
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
        # The canonical context replaces the native one, so its versions no longer apply.
        history.strip(conversation)
        conversation.source.title = link["name"]
        conversation.source_fingerprint = digest([conversation.source_fingerprint, revision["id"]])
        return conversation

    def _target(self, value, revision_id, target, cwd, model):
        """The revision as a conversation, with its profile and directory pinned for the target."""
        self._profile(target)
        conversation = self._conversation(self.store.get_revision(revision_id), value)
        profile, _, directory, cap = self.bridge._prepare(conversation, target, cwd=cwd, model=model)
        if not cap.native:
            raise BridgeError("native_unavailable", cap.reason)
        return conversation, profile, directory

    @staticmethod
    def _reusable(value, profile, revision_id, directory, model):
        """A healthy session of this assistant already at the revision, the current one first."""
        roles = _roles(value)
        candidates = [
            endpoint
            for endpoint in value["endpoints"]
            if endpoint.get("enabled", True)
            and same_profile(endpoint["profile"], profile.model_dump(mode="json"))
            and endpoint["revision_id"] == revision_id
            and not endpoint["pending"]
            and not endpoint["error"]
            and bool(endpoint.get("cwd"))
            and same_directory(endpoint["cwd"], directory)
            and endpoint.get("model") == model
        ]
        candidates.sort(key=lambda endpoint: roles[endpoint["key"]][0] == "current")
        return candidates[-1] if candidates else None

    def plan_open(self, link, target, *, branch=None, cwd=None, model=None):
        """What ``open`` would do, without capturing, publishing or saving anything."""
        value = self.store.get_link(link)
        if value["paused"] or value["unlinked"]:
            raise BridgeError("sync_paused")
        revision_id = self._choose(value, branch)
        _, profile, directory = self._target(value, revision_id, target, cwd, model)
        reusable = self._reusable(value, profile, revision_id, directory, model)
        return {
            "link_id": value["id"],
            "revision_id": revision_id,
            "profile_id": profile.id,
            "tool": profile.tool,
            "source_key": self.store.get_revision(revision_id)["source_key"],
            "reuse": reusable is not None,
            "session_key": reusable["key"] if reusable else None,
        }

    def head_previews(self, link):
        """The last request of each continuation, one line, for choosing between them.

        Conversation text: only the interactive chooser asks for it; status stays content-free.
        """
        value = self.store.get_link(link)
        previews = {}
        for head in value["heads"]:
            context = self.store.get_context(self.store.get_revision(head)["context"])
            text = next((m["text"] for m in reversed(context) if m["role"] == "user"), "")
            line = visible(" ".join(text.split()))
            previews[head] = line if len(line) <= PREVIEW_CHARS else line[: PREVIEW_CHARS - 1] + "…"
        return previews

    def open(self, link, target, *, branch=None, cwd=None, model=None, launch=True, new_copy=False):
        with self._operation():
            value = self.store.get_link(link)
            if value["paused"] or value["unlinked"]:
                raise BridgeError("sync_paused")
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
                )
            conversation, profile, directory = self._target(value, revision_id, target, cwd, model)
            reusable = None if new_copy else self._reusable(value, profile, revision_id, directory, model)
            resume_key = None
            if reusable and not reusable.get("transfer_id"):
                # The user's own session is already at this revision: reopen it as it is.
                transfer_id = None
                resume_key = reusable["key"]
            elif reusable:
                candidates = [reusable]
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
                        raise BridgeError("sync_target_unverified", "error.sync_target_unverified.restored")
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
                    raise BridgeError("sync_recovery_required", "error.sync_recovery_required.open")
                native_key = profile.id + ":" + transfer.native_id
                native_capture = self.bridge.show(native_key, snapshot=True)
                endpoint = self._endpoint(native_capture, profile, revision_id)
                endpoint.update(transfer_id=transfer.id, cwd=directory, model=model, state="synced")
                if not any(old["key"] == native_key for old in value["endpoints"]):
                    value["endpoints"].append(endpoint)
                _promote(value, native_key)
                intent.update(state="completed", transfer_id=transfer.id)
                self.store.save_link(value)
                advanced = self._observe(value, endpoint, native_capture)
                self.store.save_link(value)
                if advanced or endpoint["pending"]:
                    raise BridgeError(
                        "sync_target_advanced",
                    )
                transfer_id = transfer.id
            if reusable:
                # Reopening makes this session its assistant's current one.
                _promote(value, reusable["key"])
                self.store.save_link(value)
            resume_key = resume_key or transfer_id
            result = self.bridge.resume(resume_key, launch=False)
            result.update(
                link_id=value["id"],
                revision_id=revision_id,
                transfer_id=transfer_id,
                launched=False,
                snapshot=branch is not None,
                reused=reusable is not None,
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
            raise BridgeError("sync_unlinked")
        return self._toggle(link, paused=False)

    def unlink(self, link):
        return self._toggle(link, unlinked=True, paused=True)

    def detach(self, link, key):
        """Retire an old endpoint without deleting its archive, branch or native session."""
        with self._operation():
            value = self.store.get_link(link)
            endpoints = [endpoint for endpoint in value["endpoints"] if endpoint["key"] == key]
            if len(endpoints) != 1:
                raise BridgeError("sync_endpoint_not_found")
            endpoint = endpoints[0]
            if (
                endpoint.get("enabled", True)
                and sum(item.get("enabled", True) for item in value["endpoints"]) <= 1
            ):
                raise BridgeError("sync_last_endpoint")
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
            raise BridgeError("sync_ambiguous", "error.sync_ambiguous.session")
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
                    "error.sync_recovery_required.backup",
                )
            return self.store.backup(Path(output))
