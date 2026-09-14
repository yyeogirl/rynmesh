"""Encrypted owner history, separate from task and settlement metadata.

The product explicitly retains conversations. Reuse the node's existing
messaging key and atomic storage; never write conversation plaintext to disk.
Revision checks protect parallel views, tombstones prevent late writes from
resurrecting deleted history, and migration acknowledgements are transactional.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag

from ..atomic_io import atomic_write_json, migration_backup, read_json
from ..file_transactions import file_transaction
from ..services import peer_box

VERSION = "ryn.ask-history.v1"
SYNC_VERSION = "ryn.ask-history.v2"
CHANNEL = b"rynmesh-ask-history-v1"
MAX_BYTES = 16 * 1024 * 1024
MAX_PLAINTEXT = 11 * 1024 * 1024
MAX_SYNC_BYTES = 88 * 1024 * 1024
MAX_SYNC_PLAINTEXT = 64 * 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
FIELDS = ("id", "title", "serviceKey", "serviceName", "providerPeerId", "networkId", "createdAt", "updatedAt", "messages", "revision", "draft", "contextIds")
MESSAGE_FIELDS = ("id", "role", "content", "createdAt", "status", "taskId", "inputTokens", "outputTokens", "cost", "contextIds", "contextBytes", "promptSha256")


class ConversationError(ValueError):
    pass


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ConversationError("ask_invalid_conversation")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except ValueError:
        raise ConversationError("ask_invalid_conversation") from None
    return value


def _identity(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ConversationError("ask_invalid_conversation")
    return value


def clean_conversation(value: Mapping[str, Any]) -> dict[str, Any]:
    """Only explicit history fields cross the owner API (no arbitrary secrets)."""
    if not isinstance(value, dict):
        raise ConversationError("ask_invalid_conversation")
    result = {key: value[key] for key in FIELDS if key in value and key != "revision"}
    result["id"] = _identity(result.get("id"))
    for key, limit in (("title", 512), ("serviceKey", 2048), ("serviceName", 512), ("providerPeerId", 1024), ("networkId", 256)):
        text = result.get(key)
        if not isinstance(text, str) or not text.strip() or len(text) > limit or any(ord(char) < 32 for char in text):
            raise ConversationError("ask_invalid_conversation")
    if not result["serviceKey"].startswith(result["providerPeerId"] + "::") or result["serviceKey"] == result["providerPeerId"] + "::":
        raise ConversationError("ask_service_binding_mismatch")
    result["createdAt"] = _timestamp(result.get("createdAt"))
    result["updatedAt"] = _timestamp(result.get("updatedAt"))
    messages = result.get("messages")
    if not isinstance(messages, list) or len(messages) > 2000:
        raise ConversationError("ask_history_limit")
    cleaned, seen = [], set()
    for message in messages:
        if not isinstance(message, dict):
            raise ConversationError("ask_invalid_conversation")
        row = {key: message[key] for key in MESSAGE_FIELDS if key in message}
        row["id"] = _identity(row.get("id"))
        if row["id"] in seen or row.get("role") not in {"user", "assistant"} or row.get("status") not in {"complete", "failed", "cancelled", "queued", "running", "cancel_requested", "interrupted"}:
            raise ConversationError("ask_invalid_conversation")
        seen.add(row["id"])
        if not isinstance(row.get("content"), str) or len(row["content"].encode()) > 256 * 1024:
            raise ConversationError("ask_history_limit")
        row["createdAt"] = _timestamp(row.get("createdAt"))
        if "taskId" in row:
            _identity(row["taskId"])
        for key in ("inputTokens", "outputTokens", "cost"):
            if key in row and (type(row[key]) not in {int, float} or not math.isfinite(row[key]) or row[key] < 0):
                raise ConversationError("ask_invalid_conversation")
        if "contextIds" in row:
            ids, sizes = row["contextIds"], row.get("contextBytes", [])
            if not isinstance(ids, list) or len(ids) > 3 or any(not isinstance(value, str) or not re.fullmatch(r"import:imp_[a-f0-9]{32}(?:[a-f0-9]{32})?", value) for value in ids):
                raise ConversationError("ask_context_unavailable")
            if not isinstance(sizes, list) or len(sizes) != len(ids) or any(type(size) is not int or not 0 <= size <= 1024 * 1024 for size in sizes):
                raise ConversationError("ask_context_unavailable")
        if "promptSha256" in row and (not isinstance(row["promptSha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", row["promptSha256"])):
            raise ConversationError("ask_invalid_conversation")
        cleaned.append(row)
    result["messages"] = cleaned
    if "draft" in result and (not isinstance(result["draft"], str) or len(result["draft"].encode()) > 64 * 1024):
        raise ConversationError("ask_history_limit")
    if "contextIds" in result:
        ids = result["contextIds"]
        if not isinstance(ids, list) or len(ids) > 3 or any(not isinstance(value, str) or not re.fullmatch(r"import:imp_[a-f0-9]{32}(?:[a-f0-9]{32})?", value) for value in ids):
            raise ConversationError("ask_context_unavailable")
        if len(set(ids)) != len(ids):
            raise ConversationError("ask_context_unavailable")
    if len(_json(result)) > 1024 * 1024:
        raise ConversationError("ask_history_limit")
    return result


def public_conversation(value: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: value[key] for key in FIELDS if key in value}
    result["messages"] = [{key: message[key] for key in MESSAGE_FIELDS if key in message} for message in value["messages"]]
    return result


class ConversationStore:
    def __init__(self, root: str | Path, messaging_key: Any):
        self.root = Path(root)
        self.path = self.root / "history.json"
        self.lock = self.root / ".history.lock"
        self.key = messaging_key
        self.pub = peer_box.public_key_b64(messaging_key)
        self.actor = hashlib.sha256(_json(self.pub)).hexdigest()

    def _read(self) -> tuple[dict, dict]:
        if not self.path.exists():
            return {}, {"version": VERSION, "conversations": {}, "tombstones": {}, "migrations": {}}
        envelope = read_json(self.path, max_bytes=MAX_SYNC_BYTES)
        if not isinstance(envelope, dict) or envelope.get("version") not in {VERSION, SYNC_VERSION}:
            raise ConversationError("ask_history_version_unsupported")
        try:
            plaintext = peer_box.open_sealed(self.key, self.pub, envelope["nonce"], envelope["ciphertext"], info=CHANNEL)
            if len(plaintext) > (MAX_SYNC_PLAINTEXT if envelope['version'] == SYNC_VERSION else MAX_PLAINTEXT):
                raise ValueError
            data = json.loads(plaintext)
        except (KeyError, TypeError, ValueError, InvalidTag):
            raise ConversationError("ask_history_unreadable") from None
        if not isinstance(data, dict) or data.get("version") != envelope['version']:
            raise ConversationError("ask_history_version_unsupported")
        if any(not isinstance(data.get(key), dict) for key in ("conversations", "tombstones", "migrations")):
            raise ConversationError("ask_history_unreadable")
        if "runs" in data:
            section = data["runs"]
            if not isinstance(section, dict) or section.get("version") != 1:
                raise ConversationError("ask_history_version_unsupported")
            if not isinstance(section.get("records"), dict):
                raise ConversationError("ask_history_unreadable")
        if data['version'] == SYNC_VERSION:
            self._sync_state(data)
        return envelope, data

    def _write(self, envelope: dict, data: dict, *, capture_sync=True) -> None:
        synced = data['version'] == SYNC_VERSION
        if synced and capture_sync:
            self._sync_state(data).capture(data)
        plaintext = _json(data)
        if len(plaintext) > (MAX_SYNC_PLAINTEXT if synced else MAX_PLAINTEXT):
            raise ConversationError("ask_history_limit")
        nonce, ciphertext = peer_box.seal(self.key, self.pub, plaintext, info=CHANNEL)
        atomic_write_json(self.path, {**envelope, "version": data['version'], "nonce": nonce, "ciphertext": ciphertext},
                          max_bytes=MAX_SYNC_BYTES if synced else MAX_BYTES)

    def _sync_state(self, data):
        from ..device_sync.conversations import ConversationState
        from ..device_sync.records import SyncError

        if data['version'] != SYNC_VERSION:
            raise SyncError('sync_not_enabled')
        state = ConversationState(data.get('device_sync'))
        if state.value['actor'] != self.actor:
            raise SyncError('sync_device_identity_changed')
        return state

    def _public(self, data, row, *, state=None):
        result = public_conversation(row)
        if data['version'] == SYNC_VERSION:
            result['sync'] = (state or self._sync_state(data)).summary(row['id'])
        return result

    def list(self, service_key: str | None = None) -> list[dict]:
        with file_transaction(self.lock):
            _, data = self._read()
            state = self._sync_state(data) if data['version'] == SYNC_VERSION else None
            rows = [self._public(data, row, state=state) for row in data["conversations"].values()
                    if row['id'] not in data['tombstones'] and (service_key is None or row["serviceKey"] == service_key)]
            return sorted(rows, key=lambda row: row["updatedAt"], reverse=True)

    def get(self, conversation_id: str) -> dict:
        _identity(conversation_id)
        with file_transaction(self.lock):
            _, data = self._read()
            if conversation_id not in data["conversations"] or conversation_id in data['tombstones']:
                raise ConversationError("ask_conversation_not_found")
            return self._public(data, data["conversations"][conversation_id])

    def draft(self) -> dict:
        with file_transaction(self.lock):
            _, data = self._read()
            record = data.get("draft", {"version": 1, "text": "", "revision": 0})
            if not isinstance(record, dict) or record.get("version") != 1:
                raise ConversationError("ask_history_version_unsupported")
            return {key: record[key] for key in ("text", "revision")}

    def save_draft(self, text: str, *, expected_revision: int) -> dict:
        if not isinstance(text, str) or len(text.encode()) > 64 * 1024:
            raise ConversationError("ask_history_limit")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ConversationError("ask_revision_required")
        with file_transaction(self.lock):
            envelope, data = self._read()
            prior = self.draft()
            if prior["text"] == text:
                return prior
            if prior["revision"] != expected_revision:
                raise ConversationError("ask_revision_conflict")
            data["draft"] = {**data.get("draft", {}), "version": 1, "text": text, "revision": expected_revision + 1}
            self._write(envelope, data)
            return {"text": text, "revision": expected_revision + 1}

    def save(self, value: Mapping[str, Any], *, expected_revision: int) -> dict:
        clean = clean_conversation(value)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ConversationError("ask_revision_required")
        with file_transaction(self.lock):
            envelope, data = self._read()
            identifier = clean["id"]
            if identifier in data["tombstones"]:
                raise ConversationError("ask_conversation_deleted")
            prior = data["conversations"].get(identifier)
            if prior:
                if any(prior[key] != clean[key] for key in ("serviceKey", "providerPeerId", "networkId", "createdAt")):
                    raise ConversationError("ask_service_binding_mismatch")
                if clean_conversation(prior) == clean:
                    return self._public(data, prior)  # Same write after a lost response.
            if (prior or {}).get("revision", 0) != expected_revision:
                raise ConversationError("ask_revision_conflict")
            active_runs = [run for run in data.get("runs", {}).get("records", {}).values() if run["conversation_id"] == identifier and run["state"] in {"queued", "dispatching", "running"}]
            if prior and active_runs and clean["messages"] != clean_conversation(prior)["messages"]:
                raise ConversationError("ask_run_busy")
            if not prior and len(data["conversations"]) >= 1000:
                raise ConversationError("ask_history_limit")
            # Preserve unknown fields already on disk, including message fields.
            old_messages = {row["id"]: row for row in (prior or {}).get("messages", [])}
            clean["messages"] = [{**old_messages.get(row["id"], {}), **row} for row in clean["messages"]]
            saved = {**(prior or {}), **clean, "revision": expected_revision + 1}
            data["conversations"][identifier] = saved
            self._write(envelope, data)
            return self._public(data, saved)

    def remove(self, conversation_id: str, *, expected_revision: int) -> dict:
        _identity(conversation_id)
        if type(expected_revision) is not int or expected_revision < 1:
            raise ConversationError("ask_revision_required")
        with file_transaction(self.lock):
            envelope, data = self._read()
            prior = data["conversations"].get(conversation_id)
            if prior is None:
                if conversation_id in data["tombstones"]:
                    return {"removed": 0}
                raise ConversationError("ask_conversation_not_found")
            if prior["revision"] != expected_revision:
                raise ConversationError("ask_revision_conflict")
            if len(data["tombstones"]) >= 10000:
                raise ConversationError("ask_history_limit")
            data["tombstones"][conversation_id] = {"deletedAt": datetime.now(timezone.utc).isoformat(), "revision": expected_revision + 1}
            del data["conversations"][conversation_id]
            for run in data.get("runs", {}).get("records", {}).values():
                if run["conversation_id"] == conversation_id:
                    run.pop("body", None)
                    if run["state"] in {"queued", "dispatching", "running"}:
                        run["cancel_requested"] = True
                        if run["state"] == "queued":
                            run["state"] = "cancelled"
            self._write(envelope, data)
            return {"removed": 1}

    def migrate(self, value: Mapping[str, Any]) -> dict:
        """Import one legacy browser record; never overwrite newer node history.

        The browser retains its encrypted original until this acknowledgement.
        Digest + original ID make response loss/repeated imports safe. A deleted
        conversation remains deleted even if another browser imports it later.
        """
        clean = clean_conversation(value)
        identifier = clean["id"]
        digest = hashlib.sha256(_json(clean)).hexdigest()
        with file_transaction(self.lock):
            envelope, data = self._read()
            if identifier in data["tombstones"]:
                return {"id": identifier, "status": "deleted", "digest": digest}
            receipt = data["migrations"].get(identifier)
            if receipt:
                if receipt["digest"] != digest:
                    raise ConversationError("ask_migration_conflict")
                return {"id": identifier, "status": "already_imported", "digest": digest}
            prior = data["conversations"].get(identifier)
            if prior and clean_conversation(prior) != clean:
                raise ConversationError("ask_migration_conflict")
            if len(data["migrations"]) >= 10000 or (not prior and len(data["conversations"]) >= 1000):
                raise ConversationError("ask_history_limit")
            data["conversations"].setdefault(identifier, {**clean, "revision": 1})
            data["migrations"][identifier] = {"digest": digest}
            self._write(envelope, data)
            return {"id": identifier, "status": "imported", "digest": digest}

    def enable_sync(self):
        """Internal opt-in after consent; never enabled by constructing the store."""
        from ..device_sync.conversations import ConversationState

        with file_transaction(self.lock):
            envelope, data = self._read()
            if data['version'] == SYNC_VERSION:
                return
            if 'device_sync' in data:
                raise ConversationError('ask_history_version_unsupported')
            state = ConversationState.create(self.actor)
            state.capture(data, seed=True)
            if self.path.exists() and migration_backup(self.path, max_bytes=MAX_BYTES) is None:
                raise ConversationError('ask_history_backup_failed')
            data.update(version=SYNC_VERSION, device_sync=state.value)
            self._write(envelope, data, capture_sync=False)

    def sync_identity(self):
        with file_transaction(self.lock):
            _, data = self._read()
            return self._sync_state(data).value['actor']

    def sync_export(self):
        with file_transaction(self.lock):
            _, data = self._read()
            return self._sync_state(data).export()

    def sync_receive(self, rows):
        from ..device_sync.records import SyncError
        from ..device_sync.store import MAX_BATCH, MAX_BATCH_BYTES

        if not isinstance(rows, list) or len(rows) > MAX_BATCH or len(_json(rows)) > MAX_BATCH_BYTES:
            raise SyncError('sync_batch_limit')
        with file_transaction(self.lock):
            envelope, data = self._read()
            receipts = self._sync_state(data).merge(data, rows)
            if len(data['conversations']) > 1000 or len(data['tombstones']) > 10000:
                raise ConversationError('ask_history_limit')
            self._write(envelope, data, capture_sync=False)
            return receipts

    def sync_conflicts(self):
        with file_transaction(self.lock):
            _, data = self._read()
            return self._sync_state(data).issues() if data['version'] == SYNC_VERSION else []

    def sync_snapshot(self, *, expected_actor):
        from ..device_sync.records import SyncError

        with file_transaction(self.lock):
            _, data = self._read()
            state = self._sync_state(data)
            if state.value['actor'] != expected_actor:
                raise SyncError('sync_device_identity_changed')
            return state.export()

    def export_owner(self):
        """One snapshot for visible history, unsent draft and conflict recovery."""
        with file_transaction(self.lock):
            return {'version': 'ryn.ask-export.v1', 'conversations': self.list(), 'draft': self.draft(),
                    'sync_conflicts': self.sync_conflicts()}

    def sync_erase(self, identifier, *, expected_revision):
        _identity(identifier)
        with file_transaction(self.lock):
            envelope, data = self._read()
            state = self._sync_state(data)
            state.erase(data, identifier, expected_revision)
            self._write(envelope, data, capture_sync=False)
            return state.summary(identifier)

    def sync_restore(self, identifier, *, choice_id, new_id, expected_revision, replaces=None):
        from copy import deepcopy

        from ..device_sync import records
        from ..device_sync.records import SyncError

        _identity(identifier)
        _identity(new_id)
        if identifier == new_id:
            raise SyncError('sync_restore_new_identity_required')
        if replaces is not None:
            _identity(replaces)
            if replaces in {identifier, new_id}:
                raise SyncError('sync_restore_new_identity_required')
        with file_transaction(self.lock):
            envelope, data = self._read()
            state = self._sync_state(data)
            identity = {'source': identifier, 'choice_id': choice_id, 'revision': expected_revision}
            if replaces is not None:
                identity['replaces'] = replaces
            previous = state.value['restores'].get(new_id)
            if previous:
                if previous != identity:
                    raise SyncError('sync_restore_identity_conflict')
                if new_id in data['tombstones']:
                    raise ConversationError('ask_conversation_deleted')
                return self.get(new_id)
            if replaces is not None:
                prior = state.value['restores'].get(replaces, {})
                if any(prior.get(key) != identity[key] for key in ('source', 'choice_id', 'revision')):
                    raise SyncError('sync_restore_identity_conflict')
                if replaces not in data['tombstones'] or replaces in data['conversations']:
                    raise SyncError('sync_restore_copy_not_deleted')
            entity = state.value['entities'].get(identifier)
            if entity is None or records.fingerprint(entity['record']) != expected_revision:
                raise SyncError('sync_revision_conflict')
            current = records.view('conversations', identifier, entity['record'])
            choices = current['branches'] + current['recovery']
            if entity.get('local_draft'):
                choices = [*choices, {'choice_id': 'local-draft', 'value': clean_conversation(entity['local_draft'])}]
            choice = next((row for row in choices if row['choice_id'] == choice_id), None)
            if choice is None:
                raise SyncError('sync_choice_unavailable')
            if new_id in data['conversations'] or new_id in data['tombstones'] or new_id in state.value['entities']:
                raise SyncError('sync_restore_identity_conflict')
            if len(data['conversations']) >= 1000 or len(state.value['restores']) >= 10000:
                raise ConversationError('ask_history_limit')
            value = deepcopy(choice['value'])
            value.update(id=new_id, revision=1)
            data['conversations'][new_id] = value
            state.value['restores'][new_id] = identity
            self._write(envelope, data)
            return self._public(data, data['conversations'][new_id])

    def sync_discard_recovery(self, identifier, *, review_token):
        from ..device_sync.conversations import active
        from ..device_sync.records import SyncError

        _identity(identifier)
        with file_transaction(self.lock):
            envelope, data = self._read()
            state = self._sync_state(data)
            entity = state.value['entities'].get(identifier)
            if not isinstance(review_token, str) or len(review_token) != 64 or entity is None:
                raise SyncError('sync_revision_conflict')
            current = state.summary(identifier)
            if current['erased'] and entity.get('discarded_review') == review_token:
                return current
            if state.recovery_review(identifier) != review_token:
                raise SyncError('sync_revision_conflict')
            if not current['deleted']:
                raise SyncError('sync_recovery_not_deleted')
            if active(data, identifier):
                raise SyncError('sync_recovery_busy')
            state.erase(data, identifier, current['revision'])
            entity['discarded_review'] = review_token
            self._write(envelope, data, capture_sync=False)
            return state.summary(identifier)
