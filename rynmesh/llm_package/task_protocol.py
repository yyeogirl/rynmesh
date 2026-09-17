"""Signed end-to-end encrypted LLM task envelopes and metadata-only records."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.crypto import SignedPayload, sign_payload, verify_signed_payload
from rynmesh.services import peer_box

TASK_ENVELOPE_VERSION = "rynmesh.llm.e2ee.v1"
TERMINAL_STATES = {"succeeded", "failed", "timed_out", "cancelled", "rejected"}
ALL_STATES = {"created", "accepted", "running", *TERMINAL_STATES}
ALLOWED_TRANSITIONS = {
    "created": {"accepted", "failed", "timed_out", "cancelled", "rejected"},
    "accepted": {"running", "failed", "timed_out", "cancelled", "rejected"},
    "running": {"succeeded", "failed", "timed_out", "cancelled", "rejected"},
}


class TaskProtocolError(RuntimeError):
    pass


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def seal_task(*, body: dict[str, Any], task_id: str, kind: str, sender_peer_id: str,
              recipient_peer_id: str, sender_signing_key: bytes,
              recipient_messaging_pub: str, expires_at: str) -> SignedPayload:
    ephemeral = X25519PrivateKey.generate()
    nonce, ciphertext = peer_box.seal(
        ephemeral, recipient_messaging_pub,
        json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
    )
    outer = {
        "version": TASK_ENVELOPE_VERSION, "kind": kind, "task_id": task_id,
        "from_peer_id": sender_peer_id, "to_peer_id": recipient_peer_id,
        "ephemeral_pub": peer_box.public_key_b64(ephemeral), "nonce": nonce,
        "ciphertext": ciphertext, "expires_at": expires_at,
    }
    return sign_payload(outer, private_key_bytes=sender_signing_key)


def open_task(signed: SignedPayload | dict[str, Any], *, recipient_peer_id: str,
              recipient_messaging_key: X25519PrivateKey, expected_kind: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    envelope = signed if isinstance(signed, SignedPayload) else SignedPayload.from_dict(signed)
    verify_signed_payload(envelope)
    outer = dict(envelope.payload)
    if outer.get("version") != TASK_ENVELOPE_VERSION:
        raise TaskProtocolError("task envelope version unsupported")
    if outer.get("from_peer_id") != envelope.public_key:
        raise TaskProtocolError("task sender identity/signature mismatch")
    if outer.get("to_peer_id") != recipient_peer_id:
        raise TaskProtocolError("task recipient mismatch")
    if expected_kind and outer.get("kind") != expected_kind:
        raise TaskProtocolError("task envelope kind mismatch")
    try:
        if _parse_time(str(outer["expires_at"])) < datetime.now(timezone.utc):
            raise TaskProtocolError("task envelope expired")
        plaintext = peer_box.open_sealed(
            recipient_messaging_key, str(outer["ephemeral_pub"]),
            str(outer["nonce"]), str(outer["ciphertext"]),
        )
        body = json.loads(plaintext.decode("utf-8"))
    except TaskProtocolError:
        raise
    except Exception as exc:
        raise TaskProtocolError("task envelope authentication/decryption failed") from exc
    if not isinstance(body, dict) or str(body.get("task_id")) != str(outer.get("task_id")):
        raise TaskProtocolError("task body/id mismatch")
    return outer, body


class TaskOrderStore:
    """Durable order metadata and optional encrypted response; never body plaintext."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def get(self, task_id: str) -> dict[str, Any] | None:
        # Windows can reject reads/replaces while another thread holds the file.
        # Use the same reentrant lock as transitions and checkpoints.
        with self._lock:
            return self._read(task_id)

    def _read(self, task_id: str) -> dict[str, Any] | None:
        path = self._path(task_id)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskProtocolError(f"cannot read task record: {exc}") from exc
        return value if isinstance(value, dict) else None

    def claim(self, *, task_id: str, bindings: dict[str, str]) -> tuple[dict[str, Any], bool]:
        """Atomically create a task or validate an exact idempotent duplicate."""
        cleaned = {str(key): str(value) for key, value in bindings.items()}
        if not cleaned or any(not value for value in cleaned.values()):
            raise TaskProtocolError("task bindings must be non-empty strings")
        with self._lock:
            existing = self.get(task_id)
            if existing is not None:
                if dict(existing.get("bindings") or {}) != cleaned:
                    raise TaskProtocolError("task idempotency conflict")
                return existing, False
            now = datetime.now(timezone.utc).isoformat()
            record = {
                "task_id": task_id,
                "state": "created",
                "bindings": cleaned,
                "created_at": now,
                "updated_at": now,
                "history": [{"state": "created", "at": now}],
            }
            self._write(record)
            return record, True

    def transition(self, *, task_id: str, state: str, metadata: dict[str, Any] | None = None,
                   encrypted_response: dict[str, Any] | None = None,
                   allow_recovery: bool = False) -> dict[str, Any]:
        if state not in ALL_STATES:
            raise TaskProtocolError("invalid task state")
        with self._lock:
            record = self.get(task_id)
            if record is None:
                if state != "created":
                    raise TaskProtocolError("task must be created before transitioning")
                now = datetime.now(timezone.utc).isoformat()
                record = {
                    "task_id": task_id,
                    "state": "created",
                    "created_at": now,
                    "updated_at": now,
                    "history": [{"state": "created", "at": now, **dict(metadata or {})}],
                }
                self._write(record)
                return record
            current_state = record.get("state")
            recovery = allow_recovery and current_state in {"failed", "timed_out"} and state == "succeeded"
            if current_state in TERMINAL_STATES and not recovery:
                return record
            if state == current_state:
                return record
            if not recovery and state not in ALLOWED_TRANSITIONS.get(str(current_state), set()):
                raise TaskProtocolError(f"invalid task transition: {current_state} -> {state}")
            now = datetime.now(timezone.utc).isoformat()
            event = {"state": state, "at": now, **dict(metadata or {})}
            if recovery:
                event["recovered_after_reconnect"] = True
            forbidden = {"prompt", "response", "text", "messages", "context", "api_key", "model_path"}
            if forbidden.intersection(event):
                raise TaskProtocolError("task metadata contains a forbidden body/secret field")
            record.update({"state": state, "updated_at": now})
            record.setdefault("history", []).append(event)
            if encrypted_response is not None:
                record["encrypted_response"] = encrypted_response
            self._write(record)
            return record

    def checkpoint(self, *, task_id: str, metadata: dict[str, Any],
                   encrypted_response: dict[str, Any] | None = None) -> dict[str, Any]:
        """Persist body-free progress without changing the task state."""
        with self._lock:
            record = self.get(task_id)
            if record is None:
                raise TaskProtocolError("task must exist before checkpointing")
            now = datetime.now(timezone.utc).isoformat()
            event = {"state": str(record.get("state") or "created"), "at": now,
                     "checkpoint": True, **dict(metadata)}
            forbidden = {"prompt", "response", "text", "messages", "context", "api_key", "model_path"}
            if forbidden.intersection(event):
                raise TaskProtocolError("task metadata contains a forbidden body/secret field")
            record["updated_at"] = now
            record.setdefault("history", []).append(event)
            if encrypted_response is not None:
                record["encrypted_response"] = encrypted_response
            self._write(record)
            return record

    def list(self) -> list[dict[str, Any]]:
        records = []
        for path in sorted(self.root.glob("*.json")):
            record = self.get(path.stem)
            if record:
                records.append({k: v for k, v in record.items() if k != "encrypted_response"})
        return records

    def purge_encrypted_response(self, task_id: str) -> bool:
        with self._lock:
            record = self.get(task_id)
            if record is None or "encrypted_response" not in record:
                return False
            record.pop("encrypted_response", None)
            record["response_purged_at"] = datetime.now(timezone.utc).isoformat()
            self._write(record)
            return True

    def delete(self, task_id: str) -> bool:
        with self._lock:
            path = self._path(task_id)
            if not path.exists():
                return False
            path.unlink()
            return True

    def purge_expired_responses(self) -> int:
        purged = 0
        now = datetime.now(timezone.utc)
        for record in self.list():
            expires_at = next(
                (item.get("response_expires_at") for item in reversed(record.get("history") or [])
                 if item.get("response_expires_at")),
                "",
            )
            if expires_at:
                try:
                    if _parse_time(str(expires_at)) <= now:
                        purged += int(self.purge_encrypted_response(str(record["task_id"])))
                except (TypeError, ValueError):
                    purged += int(self.purge_encrypted_response(str(record["task_id"])))
        return purged

    def prune_terminal(self, *, older_than: datetime) -> int:
        """Delete terminal records older than the provider retention boundary."""
        boundary = older_than.astimezone(timezone.utc)
        removed = 0
        with self._lock:
            for path in sorted(self.root.glob("*.json")):
                record = self.get(path.stem)
                if not record or record.get("state") not in TERMINAL_STATES:
                    continue
                try:
                    updated_at = _parse_time(str(record.get("updated_at") or ""))
                except (TypeError, ValueError):
                    updated_at = datetime.min.replace(tzinfo=timezone.utc)
                if updated_at <= boundary:
                    path.unlink(missing_ok=True)
                    removed += 1
        return removed

    def _path(self, task_id: str) -> Path:
        if not task_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in task_id):
            raise TaskProtocolError("invalid task id")
        return self.root / f"{task_id}.json"

    def _write(self, value: dict[str, Any]) -> None:
        path = self._path(str(value["task_id"]))
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
