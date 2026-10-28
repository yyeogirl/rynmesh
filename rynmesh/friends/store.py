from __future__ import annotations

import base64
import os
import threading
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

from rynmesh.atomic_io import atomic_write_json, migration_backup, read_json

_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}


class FriendStore:
    """Atomic local relationship state; secrets never appear in public records."""

    def __init__(self, home: str | Path) -> None:
        self.root = Path(home) / "friends"
        self.state_path = self.root / "state.json"
        self.secrets_path = self.root / "secrets.json"
        key = str(self.root.resolve())
        with _LOCKS_GUARD:
            self._lock = _PROCESS_LOCKS.setdefault(key, threading.RLock())

    @contextmanager
    def _guard(self):
        """Serialize atomic read-modify-write across threads and processes."""
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            lock_path = self.root / ".store.lock"
            with lock_path.open("a+b") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": "ryn.friends.v2", "invites": {}, "relationships": {},
                "nonces": {}, "cards": {}, "secrets": {}}

    def _read(self, path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
        value = read_json(path) if path.exists() else deepcopy(fallback)
        if not isinstance(value, dict):
            raise ValueError("friend_store_invalid")
        version = value.get("version")
        if version not in {None, "ryn.friends.v2"}:
            raise ValueError("friend_store_version_unsupported")
        for key in ("invites", "relationships", "nonces", "cards"):
            value.setdefault(key, {})
            if not isinstance(value[key], dict):
                raise ValueError("friend_store_invalid")
        if version is None:
            # Merge the old two-file state once, after backing up both. Every
            # later acceptance/revocation commits credentials and state together.
            secrets = read_json(self.secrets_path) if self.secrets_path.exists() else {}
            if not isinstance(secrets, dict):
                raise ValueError("friend_store_invalid")
            for legacy in (path, self.secrets_path):
                if legacy.exists() and migration_backup(legacy) is None:
                    raise OSError("friend_store_backup_failed")
            value.update(version="ryn.friends.v2", secrets=secrets)
            self._write(path, value)
        if not isinstance(value.get("secrets"), dict):
            raise ValueError("friend_store_invalid")
        return value

    def _write(self, path: Path, value: dict[str, Any]) -> None:
        atomic_write_json(path, value, ensure_ascii=False, indent=2)

    def state(self) -> dict[str, Any]:
        with self._guard():
            return {key: value for key, value in self._read(self.state_path, self._empty()).items()
                    if key != "secrets"}

    def secrets(self) -> dict[str, str]:
        with self._guard():
            return self._read(self.state_path, self._empty())["secrets"]

    def put_invite(self, invite_id: str, record: dict[str, Any]) -> None:
        with self._guard():
            state = self._read(self.state_path, self._empty())
            state["invites"][invite_id] = deepcopy(record)
            self._write(self.state_path, state)

    def list_invites(self) -> list[dict[str, Any]]:
        return [deepcopy(row) for row in self.state()["invites"].values() if isinstance(row, dict)]

    def cancel_invite(self, invite_id: str, cancelled_at: str) -> dict[str, Any] | None:
        with self._guard():
            state = self._read(self.state_path, self._empty())
            invite = state["invites"].get(invite_id)
            if not isinstance(invite, dict):
                return None
            if invite.get("status") == "active":
                invite["status"] = "cancelled"
                invite["cancelled_at"] = cancelled_at
                self._write(self.state_path, state)
            return deepcopy(invite)

    def consume_invite(self, invite_id: str, secret_hash: str, now_iso: str) -> bool:
        with self._guard():
            state = self._read(self.state_path, self._empty())
            invite = state["invites"].get(invite_id)
            if not invite or invite.get("status") != "active" or invite.get("secret_hash") != secret_hash:
                return False
            if str(invite.get("expires_at", "")) <= now_iso:
                invite["status"] = "expired"
                self._write(self.state_path, state)
                return False
            invite["status"] = "used"
            invite["used_at"] = now_iso
            self._write(self.state_path, state)
            return True

    def put_relationship(self, record: dict[str, Any], secret: bytes) -> None:
        rid = str(record["relationship_id"])
        with self._guard():
            state = self._read(self.state_path, self._empty())
            if state["relationships"].get(rid, {}).get("status") == "revoked":
                raise ValueError("friend_revoked")
            state["relationships"][rid] = deepcopy(record)
            state["secrets"][rid] = base64.b64encode(secret).decode("ascii")
            self._write(self.state_path, state)

    def accept_invite(self, invite_id: str, secret_hash: str, now_iso: str,
                      proposed: dict[str, Any], secret: bytes) -> tuple[dict[str, Any], bytes]:
        """Exactly one relationship per invitation, including a lost-response retry."""
        with self._guard():
            state = self._read(self.state_path, self._empty())
            invite = state["invites"].get(invite_id)
            if not invite or invite.get("secret_hash") != secret_hash:
                raise ValueError("invalid_join")
            if invite.get("status") == "used":
                old = state["relationships"].get(invite.get("relationship_id"))
                if not old or old.get("peer_id") != proposed["peer_id"] or old.get("messaging_pub") != proposed["messaging_pub"]:
                    raise ValueError("invite_used")
                if old.get("status") != "active":
                    raise ValueError("friend_revoked")
                encoded = state["secrets"].get(old["relationship_id"])
                if not encoded:
                    raise ValueError("friend_credentials_unavailable")
                return deepcopy(old), base64.b64decode(encoded, validate=True)
            if invite.get("status") != "active":
                raise ValueError("invite_cancelled" if invite.get("status") == "cancelled" else "invite_expired")
            if str(invite.get("expires_at", "")) <= now_iso:
                raise ValueError("invite_expired")
            if len(state["relationships"]) >= 500:
                raise ValueError("friend_capacity_exhausted")
            record = deepcopy(proposed)
            rid = record["relationship_id"]
            state["relationships"][rid] = record
            state["secrets"][rid] = base64.b64encode(secret).decode("ascii")
            invite.update(status="used", used_at=now_iso, relationship_id=rid)
            self._write(self.state_path, state)
            return record, secret

    def relationship(self, relationship_id: str, *, active_only: bool = True) -> dict[str, Any] | None:
        record = self.state()["relationships"].get(relationship_id)
        if not isinstance(record, dict) or (active_only and record.get("status") != "active"):
            return None
        return deepcopy(record)

    def relationship_for_peer(self, peer_id: str) -> dict[str, Any] | None:
        for record in self.state()["relationships"].values():
            if isinstance(record, dict) and record.get("peer_id") == peer_id and record.get("status") == "active":
                return deepcopy(record)
        return None

    def list_relationships(self) -> list[dict[str, Any]]:
        rows = [deepcopy(row) for row in self.state()["relationships"].values() if isinstance(row, dict)]
        return sorted(rows, key=lambda row: str(row.get("created_at", "")), reverse=True)

    def secret(self, relationship_id: str) -> bytes | None:
        import base64

        value = self.secrets().get(relationship_id)
        try:
            return base64.b64decode(value) if value else None
        except ValueError:
            return None

    def revoke(
        self, relationship_id: str, revoked_at: str, *, retain_secret: bool = False,
        notice: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with self._guard():
            state = self._read(self.state_path, self._empty())
            record = state["relationships"].get(relationship_id)
            if not isinstance(record, dict):
                return None
            if record.get("status") == "revoked":
                return deepcopy(record)
            record["status"] = "revoked"
            record["revoked_at"] = revoked_at
            secrets = state["secrets"]
            value = secrets.pop(relationship_id, None)
            if retain_secret and value:
                secrets[f"revocation:{relationship_id}"] = value
            if notice:
                record["revocation_wire"] = deepcopy(notice)
                record["revocation_delivery"] = "pending"
            self._write(self.state_path, state)
            return deepcopy(record)

    def pending_revocation_secret(self, relationship_id: str) -> bytes | None:
        import base64

        value = self.secrets().get(f"revocation:{relationship_id}")
        try:
            return base64.b64decode(value) if value else None
        except ValueError:
            return None

    def set_revocation_delivery(
        self, relationship_id: str, *, state_value: str, wire: dict[str, Any] | None = None,
        mailbox_id: str = "",
    ) -> None:
        with self._guard():
            state = self._read(self.state_path, self._empty())
            record = state["relationships"].get(relationship_id)
            if not isinstance(record, dict):
                return
            if record.get("revocation_delivery") == "delivered":
                return
            record["revocation_delivery"] = state_value
            if mailbox_id:
                record["revocation_mailbox_id"] = mailbox_id
            if wire is None:
                if state_value == "delivered" and isinstance(record.get("revocation_wire"), dict):
                    import hashlib

                    from rynmesh.crypto import canonical_json
                    record["revocation_request_sha256"] = hashlib.sha256(canonical_json(record["revocation_wire"])).hexdigest()
                record.pop("revocation_wire", None)
            else:
                record["revocation_wire"] = deepcopy(wire)
            secrets = state["secrets"]
            if state_value == "delivered":
                secrets.pop(f"revocation:{relationship_id}", None)
            self._write(self.state_path, state)

    def remember_nonce(self, relationship_id: str, nonce: str, *, now: int, timestamp: int, limit: int = 512) -> bool:
        with self._guard():
            state = self._read(self.state_path, self._empty())
            previous = state["nonces"].get(relationship_id, {})
            # Retain legacy entries for a full request window during migration.
            if isinstance(previous, list):
                previous = dict.fromkeys(previous, now + 120)
            seen = {key: expires for key, expires in previous.items() if expires >= now}
            if nonce in seen or len(seen) >= limit:
                return False
            seen[nonce] = timestamp + 120
            state["nonces"][relationship_id] = seen
            self._write(self.state_path, state)
            return True

    def put_card(self, record: dict[str, Any], *, limit: int = 500) -> None:
        card_id = str(record.get("card_id", ""))
        if not card_id:
            raise ValueError("friend_card_id_required")
        with self._guard():
            state = self._read(self.state_path, self._empty())
            from .card_cleanup import erased
            if erased(state, card_id):
                raise ValueError('friend_card_erased')
            cards = state.setdefault("cards", {})
            prior = cards.get(card_id)
            if prior is not None:
                if any(prior.get(key) != record.get(key) for key in ("from", "to", "dir", "relationship_id", "card", "request_sha256")):
                    raise ValueError("friend_card_id_conflict")
                return  # A duplicate must not reset downloaded content or delivery state.
            if len(cards) >= limit:
                raise ValueError("friend_card_capacity_exhausted")
            cards[card_id] = deepcopy(record)
            self._write(self.state_path, state)

    def card_erased(self, card_id: str) -> bool:
        from .card_cleanup import erased
        with self._guard():
            return erased(self._read(self.state_path, self._empty()), card_id)

    def card(self, card_id: str) -> dict[str, Any] | None:
        record = self.state().get("cards", {}).get(card_id)
        return deepcopy(record) if isinstance(record, dict) else None

    def list_cards(self) -> list[dict[str, Any]]:
        values = self.state().get("cards", {}).values()
        rows = [deepcopy(row) for row in values if isinstance(row, dict)]
        return sorted(rows, key=lambda row: str(row.get("created_at", "")), reverse=True)

    def patch_card(self, card_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        with self._guard():
            state = self._read(self.state_path, self._empty())
            record = state.setdefault("cards", {}).get(card_id)
            if not isinstance(record, dict):
                return None
            if record.get("delivered") and changes.get("delivered") is False:
                return deepcopy(record)
            record.update(deepcopy(changes))
            self._write(self.state_path, state)
            return deepcopy(record)
