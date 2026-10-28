"""Private service discovery over an already authenticated friendship."""
from __future__ import annotations

import json
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Callable

from ..crypto import canonical_json
from ..services import peer_box
from .store import AIAccessError, rule_key

PATH = "/api/peer/ai-access/services"
VERSION = "ryn.friend-ai-catalog.v1"
MAX_RESPONSE_BYTES = 64 * 1024
FRESH_SECONDS = 120


class FriendAICatalog:
    def __init__(self, *, friends: Callable, grants: Callable, provider: Callable, clock: Callable = time.time):
        self.friends = friends
        self.grants = grants
        self.provider = provider
        self.clock = clock
        self.lock = threading.Lock()
        self.cache: dict[str, dict] = {}
        self.poll_index = 0
        self.sequence = 0
        self.pending: dict[str, int] = {}

    def respond(self, request: dict, relationship: dict) -> dict:
        friends = self.friends()
        if (set(request) != {"request_id", "relationship_id", "from", "to"}
                or request.get("relationship_id") != relationship["relationship_id"]
                or request.get("from") != relationship["peer_id"] or request.get("to") != friends.peer_id
                or not isinstance(request.get("request_id"), str) or len(request["request_id"]) != 32):
            raise AIAccessError("ai_catalog_request_invalid")
        current = self.provider()
        services = []
        status = "service_unavailable"
        if current and current.get("configured"):
            manifest = current["service"]
            grant = self.grants().grant(manifest["package_id"], relationship["relationship_id"])
            status = "revoked" if grant and not grant["allowed"] else "not_authorized"
            if grant and grant["effective"] and grant["peer_id"] == relationship["peer_id"]:
                status = "authorized"
                services.append({"service": manifest, "capacity": current["capacity"],
                    "network_id": current.get("network_id", "rynmesh-main"),
                    "online": bool(current.get("online")), "ready": bool(current.get("ready")),
                    "ai_permission": {"relationship_id": grant["relationship_id"], "revision": grant["revision"]}})
        payload = {"version": VERSION, **request, "status": status, "services": services}
        nonce, ciphertext = peer_box.seal(friends.messaging_private, relationship["messaging_pub"], canonical_json(payload))
        return {"nonce": nonce, "ciphertext": ciphertext}

    def refresh(self, peer_id: str) -> dict:
        friends = self.friends()
        relationship, secret = friends._relationship(peer_id)
        request = {"request_id": uuid.uuid4().hex, "relationship_id": relationship["relationship_id"],
                   "from": friends.peer_id, "to": peer_id}
        with self.lock:
            self.sequence += 1
            ticket = self.sequence
            self.pending[peer_id] = ticket
        try:
            wire = friends._request_wire(relationship, secret, PATH, request, max_response_bytes=MAX_RESPONSE_BYTES)
            plaintext = peer_box.open_sealed(friends.messaging_private, relationship["messaging_pub"], wire["nonce"], wire["ciphertext"])
            if len(plaintext) > MAX_RESPONSE_BYTES:
                raise ValueError
            response = json.loads(plaintext)
            if response.get("version") != VERSION or any(response.get(key) != value for key, value in request.items()):
                raise ValueError
            services = response.get("services")
            if not isinstance(services, list) or len(services) > 1:
                raise ValueError
            status = response.get("status")
            if status not in {"authorized", "not_authorized", "revoked", "service_unavailable"} or bool(services) != (status == "authorized"):
                raise ValueError
            now = self.clock()
            records = []
            for service in services:
                manifest = service["service"]
                permission = service["ai_permission"]
                rule_key(manifest["package_id"], permission["relationship_id"])
                if permission["relationship_id"] != relationship["relationship_id"] or type(permission["revision"]) is not int or permission["revision"] < 1:
                    raise ValueError
                records.append({"service": manifest, "capacity": service["capacity"], "online": service.get("online") is True,
                    "network_id": str(service.get("network_id", "rynmesh-main"))[:128],
                    "ready": service.get("ready") is True, "ai_permission": permission, "access": "friend",
                    "peer_id": peer_id, "node_name": relationship["node_name"], "node_messaging_pub": relationship["messaging_pub"],
                    "updated_at": datetime.fromtimestamp(now, timezone.utc).isoformat()})
            latest = friends.store.relationship(relationship["relationship_id"])
            if not latest or latest["peer_id"] != peer_id:
                raise AIAccessError("ai_friend_inactive")
            snapshot = {"peer_id": peer_id, "relationship_id": relationship["relationship_id"],
                        "checked_at": now, "status": status, "services": records}
            with self.lock:
                if self.pending.get(peer_id) != ticket:
                    if peer_id in self.cache:
                        return deepcopy(self.cache[peer_id])
                    raise AIAccessError("ai_catalog_refresh_superseded")
                if peer_id not in self.cache and len(self.cache) >= 512:
                    oldest = min(self.cache, key=lambda key: self.cache[key]["checked_at"])
                    self.cache.pop(oldest, None)
                self.cache[peer_id] = snapshot
            return deepcopy(snapshot)
        except Exception:
            with self.lock:
                if self.pending.get(peer_id) == ticket:
                    self.cache.pop(peer_id, None)
            raise AIAccessError("ai_friend_service_unreachable") from None
        finally:
            with self.lock:
                if self.pending.get(peer_id) == ticket:
                    self.pending.pop(peer_id, None)

    def snapshots(self) -> list[dict]:
        friends = self.friends()
        with self.lock:
            snapshots = deepcopy(list(self.cache.values()))
        result = []
        invalid = []
        for snapshot in snapshots:
            relation = friends.store.relationship(snapshot["relationship_id"])
            if not relation or relation["peer_id"] != snapshot["peer_id"]:
                invalid.append((snapshot["peer_id"], snapshot["relationship_id"]))
                continue
            if not 0 <= self.clock() - snapshot["checked_at"] <= FRESH_SECONDS:
                snapshot["status"] = "stale"
                for record in snapshot["services"]:
                    record["online"] = False
            result.append(snapshot)
        with self.lock:
            for peer_id, relationship_id in invalid:
                if self.cache.get(peer_id, {}).get("relationship_id") == relationship_id:
                    self.cache.pop(peer_id, None)
        return result

    def records(self) -> list[dict]:
        return [record for snapshot in self.snapshots() for record in snapshot["services"]]

    def run_once(self) -> bool:
        friends = [row for row in self.friends().list_friends() if row["status"] == "active"]
        if not friends:
            return False
        peer = friends[self.poll_index % len(friends)]["peer_id"]
        self.poll_index += 1
        self.refresh(peer)
        return True
