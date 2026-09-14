"""Private, versioned AI grants. Friendship by itself grants no service access."""
from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from pathlib import Path
from typing import Callable

from ..atomic_io import atomic_write_json, read_json
from ..file_transactions import file_transaction

VERSION = "ryn.ai-access.v1"
MAX_BYTES = 2 * 1024 * 1024


class AIAccessError(ValueError):
    pass


def rule_key(service_id: str, relationship_id: str) -> str:
    if not isinstance(service_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", service_id):
        raise AIAccessError("ai_service_invalid")
    if not isinstance(relationship_id, str) or not re.fullmatch(r"[a-f0-9]{32}", relationship_id):
        raise AIAccessError("ai_relationship_invalid")
    return hashlib.sha256((service_id + "\0" + relationship_id).encode()).hexdigest()


class AIAccessStore:
    def __init__(self, home: str | Path, *, relationship: Callable[[str], dict | None]):
        self.path = Path(home) / "ai-access" / "permissions.json"
        self.lock = self.path.parent / ".permissions.lock"
        self.relationship = relationship

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": VERSION, "grants": {}}
        value = read_json(self.path, max_bytes=MAX_BYTES)
        if not isinstance(value, dict) or value.get("version") != VERSION:
            raise AIAccessError("ai_permissions_version_unsupported")
        if not isinstance(value.get("grants"), dict):
            raise AIAccessError("ai_permissions_unreadable")
        for key, row in value["grants"].items():
            if (not isinstance(row, dict) or type(row.get("allowed")) is not bool
                    or type(row.get("revision")) is not int or row["revision"] < 1
                    or not isinstance(row.get("peer_id"), str) or not 1 <= len(row["peer_id"]) <= 128
                    or key != rule_key(row.get("service_id"), row.get("relationship_id"))):
                raise AIAccessError("ai_permissions_unreadable")
        return value

    def _public(self, row: dict) -> dict:
        relationship = self.relationship(row["relationship_id"])
        active = bool(relationship and relationship.get("status") == "active" and relationship.get("peer_id") == row["peer_id"])
        return {key: row[key] for key in ("service_id", "relationship_id", "peer_id", "allowed", "revision")} | {"effective": row["allowed"] and active}

    def list(self) -> list[dict]:
        with file_transaction(self.lock):
            data = self._read()
            return [self._public(row) for row in data["grants"].values()]

    def grant(self, service_id: str, relationship_id: str) -> dict | None:
        key = rule_key(service_id, relationship_id)
        with file_transaction(self.lock):
            row = self._read()["grants"].get(key)
            return self._public(row) if row else None

    def set(self, service_id: str, relationship_id: str, *, allowed: bool, expected_revision: int) -> dict:
        key = rule_key(service_id, relationship_id)
        if type(allowed) is not bool or type(expected_revision) is not int or expected_revision < 0:
            raise AIAccessError("ai_permission_request_invalid")
        with file_transaction(self.lock):
            data = self._read()
            prior = data["grants"].get(key)
            relationship = self.relationship(relationship_id)
            if allowed and (not relationship or relationship.get("status") != "active"):
                raise AIAccessError("ai_friend_inactive")
            if not prior and not relationship:
                raise AIAccessError("ai_friend_inactive")
            if prior and prior["revision"] == expected_revision + 1 and prior["allowed"] == allowed:
                return self._public(prior)  # Exactly the preceding write lost its response.
            if (prior or {}).get("revision", 0) != expected_revision:
                raise AIAccessError("ai_permission_revision_conflict")
            if prior and prior["allowed"] == allowed:
                return self._public(prior)
            if not prior and len(data["grants"]) >= 5000:
                raise AIAccessError("ai_permission_capacity_exhausted")
            peer_id = (prior or {}).get("peer_id") or relationship["peer_id"]
            if allowed and relationship["peer_id"] != peer_id:
                raise AIAccessError("ai_relationship_invalid")
            row = {**(prior or {}), "service_id": service_id, "relationship_id": relationship_id, "peer_id": peer_id,
                   "allowed": allowed, "revision": expected_revision + 1}
            data["grants"][key] = row
            atomic_write_json(self.path, data, max_bytes=MAX_BYTES)
            return self._public(row)

    def authorize(self, peer_id: str, service_id: str, permission: dict | None) -> dict:
        """Called after signature verification, before admission or replay.

        The revision is not a bearer secret: it must arrive inside the signed,
        encrypted request from the explicitly granted relationship's peer.
        A revoke/re-grant increments it so an old request cannot revive access.
        """
        if not isinstance(permission, dict) or type(permission.get("revision")) is not int:
            raise AIAccessError("ai_permission_required")
        key = rule_key(service_id, permission.get("relationship_id"))
        with file_transaction(self.lock):
            row = self._read()["grants"].get(key)
            if not row or row["peer_id"] != peer_id or not row["allowed"]:
                raise AIAccessError("ai_permission_denied")
            if row["revision"] != permission["revision"]:
                raise AIAccessError("ai_permission_revoked")
            relationship = self.relationship(row["relationship_id"])
            if not relationship or relationship.get("status") != "active" or relationship.get("peer_id") != peer_id:
                raise AIAccessError("ai_friend_inactive")
            return deepcopy({"relationship_id": row["relationship_id"], "revision": row["revision"]})
