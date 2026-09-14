from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from rynmesh.crypto import SignedPayload, canonical_json, sign_payload, verify_signed_payload
from rynmesh.file_transactions import file_transaction
from rynmesh.services import peer_box
from rynmesh.services.messaging_store import MessagingStore

from .crypto import (
    PERMISSIONS,
    FriendCryptoError,
    auth_headers,
    endpoint_category,
    invite_uri,
    parse_invite,
    secret_hash,
    validate_endpoint,
    verify_auth,
)
from .store import FriendStore

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_SHARED_CONTENT_BYTES = 5 * 1024 * 1024
# The document is base64 inside JSON, then that encrypted JSON is base64 again.
MAX_SHARED_RESPONSE_BYTES = ((((MAX_SHARED_CONTENT_BYTES + 2) // 3) * 4 + 65536 + 2) // 3) * 4 + 65536
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CARD_ID = re.compile(r"^[0-9a-f]{32}$")


class FriendError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


class FriendService:
    def __init__(
        self,
        *,
        home: Any,
        peer_id: str,
        node_name: str,
        endpoint: str,
        identity_private: bytes,
        messaging_private: Any,
        post_json: Callable[..., dict[str, Any]],
        resolve_content: Callable[[str], dict[str, Any] | None] | None = None,
        import_content: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        allow_loopback: bool = False,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.home = home
        self.peer_id = peer_id
        self.node_name = node_name
        self.endpoint = endpoint.rstrip("/")
        self.identity_private = identity_private
        self.messaging_private = messaging_private
        self.post_json = post_json
        self.resolve_content = resolve_content
        self.import_content = import_content
        self.allow_loopback = allow_loopback
        self.clock = clock
        self.queue_mail: Callable | None = None
        self.import_generation: Callable | None = None
        self.verify_import: Callable | None = None
        self.store = FriendStore(home)
        self.messages = MessagingStore(home)

    def create_invite(self, *, ttl_minutes: int = 15) -> dict[str, Any]:
        ttl = max(1, min(int(ttl_minutes), 24 * 60))
        endpoint = validate_endpoint(self.endpoint, allow_loopback=self.allow_loopback)
        created = self.clock()
        invite_id = uuid.uuid4().hex
        secret = os.urandom(32)
        payload = {
            "kind": "ryn.friend-invite.v1",
            "invite_id": invite_id,
            "peer_id": self.peer_id,
            "node_name": self.node_name,
            "endpoint": endpoint,
            "endpoints": [endpoint],
            "network_id": os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main"),
            "messaging_pub": peer_box.public_key_b64(self.messaging_private),
            "permissions": list(PERMISSIONS),
            "created_at": created.isoformat(),
            "expires_at": (created + timedelta(minutes=ttl)).isoformat(),
            "secret_hash": secret_hash(secret),
        }
        signed = sign_payload(payload, private_key_bytes=self.identity_private)
        self.store.put_invite(
            invite_id,
            {**{k: v for k, v in payload.items() if k != "messaging_pub"}, "status": "active"},
        )
        return {"invite_uri": invite_uri(signed, secret), "invite": self._public_invite(payload)}

    def inspect_invite(self, uri: str) -> dict[str, Any]:
        signed, _ = parse_invite(uri, now=self.clock(), allow_loopback=self.allow_loopback)
        validate_endpoint(str(signed.payload["endpoint"]), allow_loopback=self.allow_loopback)
        return self._public_invite(signed.payload)

    def list_invites(self) -> list[dict[str, Any]]:
        now_iso = self.clock().isoformat()
        result = []
        for row in self.store.list_invites():
            public = {key: value for key, value in row.items() if key != "secret_hash"}
            # Expiration does not require someone to attempt accepting the
            # invite. Project it on reads without rewriting persisted history.
            if public.get("status") == "active" and str(public.get("expires_at", "")) <= now_iso:
                public["status"] = "expired"
            result.append(public)
        return result

    def cancel_invite(self, invite_id: str) -> dict[str, Any]:
        record = self.store.cancel_invite(invite_id, self.clock().isoformat())
        if not record:
            raise FriendError("invite_not_found")
        return {key: value for key, value in record.items() if key != "secret_hash"}

    @staticmethod
    def _public_invite(payload: dict[str, Any]) -> dict[str, Any]:
        public = {key: payload[key] for key in ("invite_id", "peer_id", "node_name", "endpoint", "permissions", "created_at", "expires_at")}
        public["endpoints"] = list(payload.get("endpoints") or [payload["endpoint"]])
        public["network_id"] = str(payload.get("network_id") or "rynmesh-main")
        public["address_category"] = endpoint_category(str(payload["endpoint"]))
        public["fingerprint"] = str(payload["peer_id"])[:12] + "…" + str(payload["peer_id"])[-8:]
        return public

    def join(self, uri: str) -> dict[str, Any]:
        signed, secret = parse_invite(uri, now=self.clock(), allow_loopback=self.allow_loopback,
                                     allow_expired=True)
        for prior in self.store.list_relationships():
            if prior.get("invite_id") == signed.payload["invite_id"] and prior.get("peer_id") == signed.payload["peer_id"]:
                if prior.get("status") != "active":
                    raise FriendError("friend_revoked")
                return self.public_relationship(prior)
        endpoint = validate_endpoint(str(signed.payload["endpoint"]), allow_loopback=self.allow_loopback)
        join_payload = {
            "kind": "ryn.friend-join.v1",
            "invite": signed.to_dict(),
            "invite_secret": base64.urlsafe_b64encode(secret).decode("ascii").rstrip("="),
            "peer_id": self.peer_id,
            "node_name": self.node_name,
            "endpoint": validate_endpoint(self.endpoint, allow_loopback=self.allow_loopback),
            "messaging_pub": peer_box.public_key_b64(self.messaging_private),
            "created_at": self.clock().isoformat(),
            "timestamp": int(self.clock().timestamp()),
            "nonce": uuid.uuid4().hex,
            "permissions": list(PERMISSIONS),
        }
        join_payload["proof"] = sign_payload(join_payload, private_key_bytes=self.identity_private).to_dict()
        try:
            response = self.post_json(endpoint, "/api/peer/friends/accept", join_payload, None)
            response_proof = SignedPayload.from_dict(response["proof"])
            verify_signed_payload(response_proof)
            if (response_proof.public_key != signed.payload["peer_id"]
                or response_proof.payload != {key: value for key, value in response.items() if key != "proof"}
                or response.get("invite_id") != signed.payload["invite_id"]
                or response.get("receiver") != self.peer_id):
                raise FriendError("friend_acceptance_invalid")
            if response.get("error"):
                code = response["error"]
                if code not in {"invite_expired", "invite_used", "invite_cancelled", "invite_not_found", "friend_revoked", "invalid_join", "friend_capacity_exhausted"}:
                    raise FriendError("friend_acceptance_invalid")
                raise FriendError(code)
            inviter_pub = str(response["messaging_pub"])
            if inviter_pub != signed.payload["messaging_pub"]:
                raise FriendError("friend_acceptance_invalid")
            relationship_secret = peer_box.open_sealed(
                self.messaging_private,
                inviter_pub,
                str(response["secret_nonce"]),
                str(response["secret_ciphertext"]),
            )
            if len(relationship_secret) != 32:
                raise FriendError("friend_acceptance_invalid")
        except FriendError:
            raise
        except Exception as exc:
            raise FriendError("could_not_join_friend") from exc
        record = {
            "relationship_id": str(response["relationship_id"]),
            "invite_id": str(signed.payload["invite_id"]),
            "peer_id": str(signed.payload["peer_id"]),
            "node_name": str(signed.payload["node_name"]),
            "endpoint": endpoint,
            "messaging_pub": inviter_pub,
            "permissions": list(PERMISSIONS),
            "status": "active",
            "created_at": self.clock().isoformat(),
        }
        self.store.put_relationship(record, relationship_secret)
        return self.public_relationship(record)

    def accept(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            signed_invite = SignedPayload.from_dict(body["invite"])
            verify_signed_payload(signed_invite)
            secret = base64.urlsafe_b64decode(str(body["invite_secret"]) + "=" * (-len(str(body["invite_secret"])) % 4))
            proof = SignedPayload.from_dict(body["proof"])
            unsigned = {key: value for key, value in body.items() if key != "proof"}
            verify_signed_payload(proof)
            if proof.public_key != body.get("peer_id") or proof.payload != unsigned:
                raise FriendError("invalid_join")
            if tuple(body.get("permissions") or ()) != PERMISSIONS:
                raise FriendError("invalid_join")
            if abs(int(self.clock().timestamp()) - int(body.get("timestamp", 0))) > 120:
                raise FriendError("invalid_join")
            payload = signed_invite.payload
            if (signed_invite.public_key != self.peer_id or payload.get("peer_id") != self.peer_id
                or payload.get("secret_hash") != secret_hash(secret) or body["peer_id"] == self.peer_id):
                raise FriendError("invalid_join")
            if len(base64.b64decode(str(body["messaging_pub"]), validate=True)) != 32:
                raise FriendError("invalid_join")
            validate_endpoint(str(body.get("endpoint", "")), allow_loopback=self.allow_loopback)
            now_iso = self.clock().isoformat()
        except Exception as exc:
            raise FriendError("invalid_join") from exc
        relationship_id = uuid.uuid4().hex
        relationship_secret = os.urandom(32)
        record = {
            "relationship_id": relationship_id,
            "invite_id": str(payload["invite_id"]),
            "peer_id": str(body["peer_id"]),
            "node_name": str(body.get("node_name") or "Friend"),
            "endpoint": str(body["endpoint"]).rstrip("/"),
            "messaging_pub": str(body["messaging_pub"]),
            "permissions": list(PERMISSIONS),
            "status": "active",
            "created_at": now_iso,
        }
        try:
            record, relationship_secret = self.store.accept_invite(
                str(payload["invite_id"]), secret_hash(secret), now_iso, record, relationship_secret
            )
        except ValueError as exc:
            raise FriendError(str(exc)) from None
        nonce, ciphertext = peer_box.seal(self.messaging_private, record["messaging_pub"], relationship_secret)
        response = {
            "relationship_id": record["relationship_id"],
            "invite_id": str(payload["invite_id"]),
            "receiver": str(body["peer_id"]),
            "messaging_pub": peer_box.public_key_b64(self.messaging_private),
            "secret_nonce": nonce,
            "secret_ciphertext": ciphertext,
        }
        return {**response, "proof": sign_payload(response, private_key_bytes=self.identity_private).to_dict()}

    @staticmethod
    def public_relationship(record: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in record.items()
            if key in {"relationship_id", "peer_id", "node_name", "endpoint", "permissions", "status",
                       "created_at", "revoked_at", "revocation_delivery"}
        }

    def list_friends(self) -> list[dict[str, Any]]:
        return [self.public_relationship(row) for row in self.store.list_relationships()]

    def _relationship(self, peer_id: str) -> tuple[dict[str, Any], bytes]:
        record = self.store.relationship_for_peer(peer_id)
        if not record:
            raise FriendError("active_friend_required")
        secret = self.store.secret(str(record["relationship_id"]))
        if not secret:
            raise FriendError("active_friend_required")
        return record, secret

    def _request_wire(
        self,
        record: dict[str, Any],
        secret: bytes,
        path: str,
        wire: dict[str, Any],
        *,
        max_response_bytes: int | None = None,
    ) -> dict[str, Any]:
        body = canonical_json(wire)
        headers = auth_headers(
            secret,
            method="POST",
            path=path,
            body=body,
            sender=self.peer_id,
            receiver=str(record["peer_id"]),
            relationship_id=str(record["relationship_id"]),
            timestamp=int(self.clock().timestamp()),
            nonce=uuid.uuid4().hex,
        )
        kwargs = {"max_response_bytes": max_response_bytes} if max_response_bytes else {}
        return self.post_json(str(record["endpoint"]), path, wire, headers, **kwargs)

    def _send_wire(self, record: dict[str, Any], secret: bytes, path: str, wire: dict[str, Any]) -> bool:
        try:
            response = self._request_wire(record, secret, path, wire)
            receipt = SignedPayload.from_dict(response["receipt"])
            verify_signed_payload(receipt)
            return receipt.public_key == record["peer_id"] and receipt.payload == {
                "kind": "ryn.friend-receipt.v1", "receiver": record["peer_id"],
                "sender": self.peer_id, "relationship_id": record["relationship_id"],
                "path": path, "request_sha256": hashlib.sha256(canonical_json(wire)).hexdigest(),
            }
        except Exception:
            return False

    def delivery_receipt(self, path: str, wire: dict[str, Any]) -> dict[str, Any]:
        """Called only after a received operation has committed durably."""
        payload = {"kind": "ryn.friend-receipt.v1", "receiver": self.peer_id,
                   "sender": wire["from"], "relationship_id": wire["relationship_id"],
                   "path": path, "request_sha256": hashlib.sha256(canonical_json(wire)).hexdigest()}
        return {"ok": True, "receipt": sign_payload(payload, private_key_bytes=self.identity_private).to_dict()}

    def send_message(self, peer_id: str, *, text: str = "", attachment: dict[str, Any] | None = None,
                     message_id: str | None = None) -> dict[str, Any]:
        with file_transaction(self.store.root / ".delivery.lock"):
            local = self._send_message(peer_id, text=text, attachment=attachment, message_id=message_id)
        # Never hold the receive transaction while contacting a friend: simultaneous
        # A→B and B→A sends must not wait on each other's receive transaction.
        if isinstance(local.get("wire"), dict):
            local = self._attempt_delivery(peer_id, local)
        return {key: value for key, value in local.items() if key not in {"wire", "request_digest"}}

    def _attempt_delivery(self, peer_id: str, snapshot: dict[str, Any], *, explicit_retry: bool = False) -> dict[str, Any]:
        # Separate bounded lock stripes serialize attempts for one outgoing item.
        # Receive paths never take these locks, so two-way sends cannot deadlock.
        identifier = str(snapshot.get("card_id") or snapshot["msg_id"])
        stripe = hashlib.sha256(f"{peer_id}:{identifier}".encode()).hexdigest()[:2]
        with file_transaction(self.store.root / f".outgoing-{stripe}.lock"):
            if snapshot.get("card_id"):
                if self.store.card_erased(identifier):
                    raise FriendError('friend_card_erased')
                local = self.store.card(identifier) or snapshot
            else:
                rows = {str(row.get("msg_id")): row for row in self.messages.history(peer_id)}
                local = rows.get(identifier, snapshot)
            if not isinstance(local.get("wire"), dict) or local.get("delivered"):
                return local
            record, secret = self._relationship(peer_id)
            # A request that overlapped a completed deposit must observe its
            # receipt rather than spend another mailbox slot. A later explicit
            # retry may still request a lost delivery acknowledgment again.
            if explicit_retry and local.get("mailbox_id") == snapshot.get("mailbox_id"):
                if local.get("delivery_state") in {"failed", "mailbox"}:
                    local["delivery_state"] = "queued"
            self._deliver_message(record, secret, local)
            if local.get("card_id"):
                return self.store.patch_card(identifier, local) or local
            return self._commit_delivery(peer_id, local)

    def _commit_delivery(self, peer_id: str, local: dict[str, Any]) -> dict[str, Any]:
        with file_transaction(self.store.root / ".delivery.lock"):
            latest = {str(row.get("msg_id")): row for row in self.messages.history(peer_id)}
            prior = latest.get(local["msg_id"], {})
            if prior.get("delivered") and not local.get("delivered"):
                return prior  # A fast mailbox receipt must never regress to queued.
            self.messages.append(peer_id, local)
            return local

    def _deliver_message(self, record: dict[str, Any], secret: bytes, local: dict[str, Any]) -> None:
        if local.get("expires_at") and local["expires_at"] <= self.clock().isoformat():
            local.update(delivered=False, delivery_state="expired", error="message_expired")
            return
        path = str(local.get("delivery_path") or "/api/peer/friends/message")
        local["last_attempt_unix"] = self.clock().timestamp()
        if self._send_wire(record, secret, path, local["wire"]):
            local.update(delivered=True, delivery_state="delivered", wire=None, error="")
            return
        if self.queue_mail is None or local.get("delivery_state") == "mailbox":
            return
        try:
            receipt = self.queue_mail(record, path, local["wire"], expires_at=local.get("expires_at"))
            local.update(delivered=False, delivery_state="mailbox", mailbox_id=receipt["message_id"], error="")
        except Exception as exc:
            code = getattr(exc, "detail", "") or str(exc)
            if code in {"envelope_too_large", "message_too_large", "payload_too_large", "no_registry"}:
                local.update(delivery_state="queued", error="waiting_for_direct_connection")
            elif code == "message_expired":
                local.update(delivered=False, delivery_state="expired", error="message_expired")
            elif code in {"recipient_full", "sender_quota", "rate_limited"}:
                local.update(delivered=False, delivery_state="failed", error=code)
            else:
                local.update(delivery_state="failed", error="mailbox_unavailable")

    def _send_message(self, peer_id: str, *, text: str, attachment: dict[str, Any] | None,
                      message_id: str | None) -> dict[str, Any]:
        record, secret = self._relationship(peer_id)
        if len(text.encode("utf-8")) > 32 * 1024:
            raise FriendError("message_too_large")
        latest = {str(row.get("msg_id")): row for row in self.messages.history(peer_id)}
        attachment = attachment or None
        raw = attachment.get("bytes") if attachment else None
        if raw is not None and len(raw) > MAX_ATTACHMENT_BYTES:
            raise FriendError("attachment_too_large")
        msg_id = message_id or uuid.uuid4().hex
        if not _CARD_ID.fullmatch(msg_id):
            raise FriendError("message_id_invalid")
        intent = {"text": text, "attachment": {key: value for key, value in (attachment or {}).items() if key != "bytes"},
                  "attachment_sha256": hashlib.sha256(raw or b"").hexdigest()}
        intent_hash = hashlib.sha256(canonical_json(intent)).hexdigest()
        if msg_id in latest:
            old = latest[msg_id]
            if old.get("request_digest") != intent_hash or old.get("dir") != "out":
                raise FriendError("message_id_conflict")
            return old
        if sum(row.get("delivery_state") == "queued" for row in latest.values()) >= 100:
            raise FriendError("friend_queue_full")
        inner: dict[str, Any] = {"msg_id": msg_id, "kind": "file" if attachment else "text", "text": text, "ts": self.clock().isoformat()}
        if attachment:
            inner["attachment"] = {
                "filename": str(attachment.get("filename") or "file")[:255],
                "mime": str(attachment.get("mime") or "application/octet-stream")[:127],
                "size": len(raw or b""),
                "bytes": base64.b64encode(raw or b"").decode("ascii"),
            }
        nonce, ciphertext = peer_box.seal(self.messaging_private, str(record["messaging_pub"]), canonical_json(inner))
        wire = {"v": 1, "relationship_id": record["relationship_id"], "from": self.peer_id, "to": peer_id, "from_pub": peer_box.public_key_b64(self.messaging_private), "nonce": nonce, "ciphertext": ciphertext}
        local = {key: value for key, value in inner.items() if key != "attachment"}
        local.update({"dir": "out", "from": self.peer_id, "to": peer_id, "delivered": False,
                      "delivery_state": "queued", "wire": wire, "request_digest": intent_hash,
                      "request_sha256": hashlib.sha256(canonical_json(wire)).hexdigest(),
                      "expires_at": (self.clock() + timedelta(hours=1)).isoformat()})
        if attachment:
            blob_id = hashlib.sha256(f"{self.peer_id}:{peer_id}:{msg_id}".encode()).hexdigest()
            self.messages.save_attachment(blob_id, raw or b"")
            local["attachment"] = {key: value for key, value in inner["attachment"].items() if key != "bytes"}
            local["attachment"]["blob_id"] = blob_id
        self.messages.append(peer_id, local)
        return local

    def receive_message(self, wire: dict[str, Any]) -> dict[str, Any]:
        with file_transaction(self.store.root / ".delivery.lock"):
            return self._receive_message(wire)

    def _receive_message(self, wire: dict[str, Any]) -> dict[str, Any]:
        relationship = self.store.relationship(str(wire.get("relationship_id", "")))
        if not relationship or relationship.get("peer_id") != wire.get("from") or wire.get("to") != self.peer_id:
            raise FriendError("friend_auth_failed")
        plain = peer_box.open_sealed(self.messaging_private, str(relationship["messaging_pub"]), str(wire["nonce"]), str(wire["ciphertext"]))
        inner = json.loads(plain.decode("utf-8"))
        if (not isinstance(inner, dict) or not _CARD_ID.fullmatch(str(inner.get("msg_id", "")))
            or inner.get("kind") not in {"text", "file"} or not isinstance(inner.get("text"), str)
            or len(inner["text"].encode("utf-8")) > 32 * 1024):
            raise FriendError("friend_message_invalid")
        request_hash = hashlib.sha256(canonical_json(wire)).hexdigest()
        prior = next((row for row in self.messages.history(str(wire["from"]))
                      if row.get("msg_id") == inner["msg_id"]), None)
        if prior:
            if prior.get("request_sha256") != request_hash:
                raise FriendError("message_id_conflict")
            return prior
        local = {key: inner.get(key) for key in ("msg_id", "kind", "text", "ts")}
        local.update({"dir": "in", "from": wire["from"], "to": self.peer_id, "delivered": True,
                      "delivery_state": "delivered", "request_sha256": request_hash})
        attachment = inner.get("attachment")
        if attachment:
            if not isinstance(attachment, dict) or len(str(attachment.get("bytes", ""))) > (MAX_ATTACHMENT_BYTES + 2) // 3 * 4:
                raise FriendError("attachment_too_large")
            data = base64.b64decode(attachment.get("bytes", ""), validate=True)
            if len(data) > MAX_ATTACHMENT_BYTES or attachment.get("size") != len(data):
                raise FriendError("attachment_too_large")
            blob_id = hashlib.sha256(f"{wire['from']}:{self.peer_id}:{inner['msg_id']}".encode()).hexdigest()
            self.messages.save_attachment(blob_id, data)
            local["attachment"] = {"filename": str(attachment.get("filename") or "file")[:255],
                                   "mime": str(attachment.get("mime") or "application/octet-stream")[:127],
                                   "size": len(data), "blob_id": blob_id}
        self.messages.append_unique(str(wire["from"]), local)
        return local

    def verify_request(self, *, path: str, body: bytes, headers: Any) -> dict[str, Any]:
        normalized = {str(key).lower(): str(value) for key, value in headers.items()}
        relationship_id = normalized.get("x-ryn-friend-id", "")
        sender = normalized.get("x-ryn-friend-peer", "")
        relationship = self.store.relationship(relationship_id)
        secret = self.store.secret(relationship_id)
        if not relationship or not secret or relationship.get("peer_id") != sender:
            raise FriendError("friend_auth_failed")
        try:
            timestamp = int(normalized.get("x-ryn-friend-time", "0"))
            nonce = normalized.get("x-ryn-friend-nonce", "")
            now = int(self.clock().timestamp())
            verify_auth(secret, method="POST", path=path, body=body, sender=sender, receiver=self.peer_id, relationship_id=relationship_id, timestamp=timestamp, nonce=nonce, signature=normalized.get("x-ryn-friend-mac", ""), now=now)
            if not nonce or len(nonce) > 128 or not self.store.remember_nonce(relationship_id, nonce, now=now, timestamp=timestamp):
                raise FriendCryptoError("friend_auth_failed")
        except Exception as exc:
            raise FriendError("friend_auth_failed") from exc
        return relationship

    @staticmethod
    def _clean_card(card: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {
            "version": "ryn.shared-content-card.v1",
            "library_id": str(card.get("library_id", ""))[:320],
            "content_id": str(card.get("content_id", ""))[:256],
            "title": str(card.get("title", ""))[:300],
            "summary": str(card.get("summary", ""))[:500],
            "kind": str(card.get("kind", ""))[:64],
            "source": str(card.get("source", ""))[:300],
            "source_url": str(card.get("source_url", ""))[:2048],
            "publisher_peer_id": str(card.get("publisher_peer_id", ""))[:256],
            "manifest_ref": str(card.get("manifest_ref", ""))[:256],
            "filename": str(card.get("filename", ""))[:180],
            "mime": str(card.get("mime", "application/octet-stream"))[:127],
            "size_bytes": max(0, min(int(card.get("size_bytes", 0) or 0), MAX_SHARED_CONTENT_BYTES)),
            "sha256": str(card.get("sha256", "")).lower(),
            "fetch_available": bool(card.get("fetch_available")),
        }
        if 'content_truncated' in card and type(card['content_truncated']) is not bool:
            raise FriendError('friend_card_reference_invalid')
        if card.get('content_truncated') is True:
            clean['content_truncated'] = True
        if clean["fetch_available"] and (
            not clean["library_id"]
            or not clean["filename"]
            or not _SHA256.fullmatch(clean["sha256"])
            or not 0 < clean["size_bytes"] <= MAX_SHARED_CONTENT_BYTES
        ):
            raise FriendError("friend_card_reference_invalid")
        return clean

    def send_content_card(self, peer_id: str, card: dict[str, Any], *, card_id: str | None = None) -> dict[str, Any]:
        with file_transaction(self.store.root / ".cards.lock"):
            local = self._prepare_content_card(peer_id, card, card_id=card_id)
        if isinstance(local.get("wire"), dict):
            local = self._attempt_delivery(peer_id, local)
        return self.public_card(self.store.card(local["card_id"]) or local)

    def _prepare_content_card(self, peer_id: str, card: dict[str, Any], *, card_id: str | None) -> dict[str, Any]:
        record, _ = self._relationship(peer_id)
        clean = self._clean_card(card)
        card_id = card_id or uuid.uuid4().hex
        if not _CARD_ID.fullmatch(card_id):
            raise FriendError("friend_card_invalid")
        digest = hashlib.sha256(canonical_json(clean)).hexdigest()
        prior = self.store.card(card_id)
        if prior:
            if (prior.get("dir") != "out" or prior.get("to") != peer_id or prior.get("request_digest") != digest
                or prior.get("source_item_id", "") != str(card.get("source_item_id", ""))
                or prior.get('source_offline_job_id', '') != str(card.get('source_offline_job_id', ''))):
                raise FriendError("friend_card_id_conflict")
            return prior
        if self.resolve_content and clean["library_id"]:
            resource = self.resolve_content(str(clean["library_id"]))
            if resource:
                data = bytes(resource.get("data", b""))
                if 0 < len(data) <= MAX_SHARED_CONTENT_BYTES:
                    clean.update(
                        {
                            "content_id": str(resource.get("content_id", clean["content_id"]))[:256],
                            "publisher_peer_id": str(
                                resource.get("publisher_peer_id", self.peer_id)
                            )[:256],
                            "manifest_ref": str(resource.get("manifest_ref", ""))[:256],
                            "filename": str(resource.get("filename", "shared-content"))[:180],
                            "mime": str(
                                resource.get("mime", "application/octet-stream")
                            )[:127],
                            "size_bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "fetch_available": True,
                        }
                    )
        clean = self._clean_card(clean)
        inner = {
            "version": "ryn.shared-content-card.v1",
            "card_id": card_id,
            "created_at": self.clock().isoformat(),
            "card": clean,
        }
        nonce, ciphertext = peer_box.seal(self.messaging_private, str(record["messaging_pub"]), canonical_json(inner))
        wire = {"v": 1, "relationship_id": record["relationship_id"], "from": self.peer_id, "to": peer_id, "nonce": nonce, "ciphertext": ciphertext}
        local = {
                **inner,
                "from": self.peer_id,
                "to": peer_id,
                "dir": "out",
                "relationship_id": record["relationship_id"],
                "delivery_state": "queued", "delivered": False,
                "wire": wire, "request_digest": digest,
                "request_sha256": hashlib.sha256(canonical_json(wire)).hexdigest(),
                "delivery_path": "/api/peer/friends/content-card",
                "source_item_id": str(card.get("source_item_id", "")),
                "source_offline_job_id": str(card.get("source_offline_job_id", "")),
                "expires_at": (self.clock() + timedelta(hours=1)).isoformat(),
            }
        self.store.put_card(local)
        return local

    def retry_card(self, card_id: str) -> dict[str, Any]:
        local = self.store.card(card_id)
        if not local or local.get("dir") != "out":
            raise FriendError("friend_card_not_found")
        peer_id = str(local.get("to", ""))
        self._relationship(peer_id)
        if isinstance(local.get("wire"), dict):
            local = self._attempt_delivery(peer_id, local, explicit_retry=True)
        return self.public_card(self.store.card(card_id) or local)

    @staticmethod
    def public_card(row: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in row.items() if key in {
            "version", "card_id", "card", "from", "to", "dir", "created_at", "delivery_state",
            "delivered", "fetch_state", "fetched_library_id", "sha256_verified", "error", "expires_at"
        }}

    def receive_content_card(self, wire: dict[str, Any]) -> dict[str, Any]:
        relationship = self.store.relationship(str(wire.get("relationship_id", "")))
        if not relationship or relationship.get("peer_id") != wire.get("from") or wire.get("to") != self.peer_id:
            raise FriendError("friend_auth_failed")
        plain = peer_box.open_sealed(self.messaging_private, str(relationship["messaging_pub"]), str(wire["nonce"]), str(wire["ciphertext"]))
        inner = json.loads(plain.decode("utf-8"))
        if (
            inner.get("version") != "ryn.shared-content-card.v1"
            or not _CARD_ID.fullmatch(str(inner.get("card_id", "")))
            or not isinstance(inner.get("card"), dict)
        ):
            raise FriendError("friend_card_invalid")
        clean = self._clean_card(dict(inner["card"]))
        row = {
            "version": inner["version"],
            "card_id": inner["card_id"],
            "created_at": str(inner.get("created_at", ""))[:64],
            "request_sha256": hashlib.sha256(canonical_json(wire)).hexdigest(),
            "card": clean,
            "from": wire["from"],
            "to": self.peer_id,
            "dir": "in",
            "relationship_id": relationship["relationship_id"],
            "fetch_state": "available" if clean["fetch_available"] else "metadata_only",
        }
        try:
            self.store.put_card(row)
        except ValueError as exc:
            if str(exc) != 'friend_card_erased':
                raise
            # Acknowledge previously received data without restoring its payload.
            return {'card_id': inner['card_id'], 'erased': True}
        return self.store.card(inner["card_id"]) or row

    def content_cards(self) -> list[dict[str, Any]]:
        rows = self.store.list_cards()
        from .card_cleanup import controls
        deleted = set(controls(self.store.state())['deleted'])
        path = self.home / "friends" / "content-cards.jsonl"
        if path.exists():
            known = {str(row.get("card_id", "")) for row in rows}
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    legacy = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (isinstance(legacy, dict) and str(legacy.get("card_id", "")) not in known
                        and str(legacy.get('card_id', '')) not in deleted):
                    rows.append(legacy)
        return [self.public_card(row) for row in sorted(rows, key=lambda row: str(row.get("created_at", "")), reverse=True)[:500]]

    def serve_content_card(
        self, request: dict[str, Any], relationship: dict[str, Any]
    ) -> dict[str, Any]:
        card_id = str(request.get("card_id", ""))
        active = self.store.relationship(str(relationship.get("relationship_id", "")))
        if not active or active.get("peer_id") != relationship.get("peer_id"):
            raise FriendError("active_friend_required")
        row = self.store.card(card_id)
        if (
            request.get("v") != 1
            or not row
            or row.get("dir") != "out"
            or row.get("from") != self.peer_id
            or row.get("to") != relationship.get("peer_id")
            or row.get("relationship_id") != relationship.get("relationship_id")
            or request.get("from") != relationship.get("peer_id")
            or request.get("to") != self.peer_id
            or request.get("relationship_id") != relationship.get("relationship_id")
        ):
            raise FriendError("friend_card_not_found")
        card = self._clean_card(dict(row.get("card") or {}))
        if not card["fetch_available"] or not self.resolve_content:
            raise FriendError("friend_card_content_unavailable")
        resource = self.resolve_content(str(card["library_id"]))
        data = bytes((resource or {}).get("data", b""))
        if (
            len(data) != card["size_bytes"]
            or hashlib.sha256(data).hexdigest() != card["sha256"]
        ):
            raise FriendError("friend_card_content_changed")
        response = {
            "version": "ryn.shared-content-fetch.v1",
            "card_id": card_id,
            "filename": card["filename"],
            "mime": card["mime"],
            "size_bytes": len(data),
            "sha256": card["sha256"],
            "data_base64": base64.b64encode(data).decode("ascii"),
        }
        nonce, ciphertext = peer_box.seal(
            self.messaging_private,
            str(relationship["messaging_pub"]),
            canonical_json(response),
        )
        return {
            "ok": True,
            "v": 1,
            "relationship_id": relationship["relationship_id"],
            "from": self.peer_id,
            "to": relationship["peer_id"],
            "nonce": nonce,
            "ciphertext": ciphertext,
        }

    def fetch_content_card(self, card_id: str, *, repair: bool = False) -> dict[str, Any]:
        row = self.store.card(card_id)
        if not row or row.get("dir") != "in":
            raise FriendError("friend_card_not_found")
        if row.get("fetch_state") == "fetched" and row.get("fetched_library_id") and not repair:
            if self.verify_import:
                try:
                    self.verify_import(str(row["fetched_library_id"]))
                except (OSError, ValueError):
                    self.store.patch_card(card_id, {"fetch_state": "unavailable", "sha256_verified": False})
                    raise FriendError("friend_copy_unavailable") from None
            return {
                "ok": True,
                "card_id": card_id,
                "library_id": row["fetched_library_id"],
                "sha256_verified": True,
                "already_fetched": True,
            }
        peer_id = str(row.get("from", ""))
        relationship, secret = self._relationship(peer_id)
        if row.get("relationship_id") != relationship.get("relationship_id"):
            raise FriendError("friend_card_not_found")
        card = self._clean_card(dict(row.get("card") or {}))
        if not card["fetch_available"] or not self.import_content:
            raise FriendError("friend_card_content_unavailable")
        generation = self.import_generation() if self.import_generation else None
        request = {
            "v": 1,
            "relationship_id": relationship["relationship_id"],
            "card_id": card_id,
            "from": self.peer_id,
            "to": peer_id,
        }
        try:
            wire = self._request_wire(
                relationship,
                secret,
                "/api/peer/friends/content-card/fetch",
                request,
                max_response_bytes=MAX_SHARED_RESPONSE_BYTES,
            )
            if (
                wire.get("v") != 1
                or wire.get("relationship_id") != relationship["relationship_id"]
                or wire.get("from") != peer_id
                or wire.get("to") != self.peer_id
            ):
                raise FriendError("friend_card_fetch_invalid")
            plain = peer_box.open_sealed(
                self.messaging_private,
                str(relationship["messaging_pub"]),
                str(wire["nonce"]),
                str(wire["ciphertext"]),
            )
            content = json.loads(plain.decode("utf-8"))
            data = base64.b64decode(str(content.get("data_base64", "")), validate=True)
        except FriendError:
            raise
        except Exception as exc:
            raise FriendError("friend_card_fetch_failed") from exc
        if (
            content.get("version") != "ryn.shared-content-fetch.v1"
            or content.get("card_id") != card_id
            or int(content.get("size_bytes", -1)) != len(data)
            or len(data) != card["size_bytes"]
            or str(content.get("sha256", "")) != card["sha256"]
            or hashlib.sha256(data).hexdigest() != card["sha256"]
        ):
            raise FriendError("friend_card_hash_mismatch")
        imported = self.import_content(
            {
                "peer_id": peer_id,
                "card_id": card_id,
                "repair": repair,
                "generation": generation,
                "filename": card["filename"],
                "mime": card["mime"],
                "data": data,
                "card": card,
            }
        )
        library_id = str(imported.get("library_id", ""))
        if not library_id:
            raise FriendError("friend_card_import_failed")
        self.store.patch_card(
            card_id,
            {
                "fetch_state": "fetched",
                "fetched_library_id": library_id,
                "fetched_at": self.clock().isoformat(),
                "sha256_verified": True,
            },
        )
        return {
            "ok": True,
            "card_id": card_id,
            "library_id": library_id,
            "library_item": imported,
            "sha256_verified": True,
            "already_fetched": False,
        }

    def revoke(self, relationship_id: str, *, notify: bool = True) -> dict[str, Any]:
        relationship = self.store.relationship(relationship_id, active_only=False)
        if not relationship:
            raise FriendError("friend_not_found")
        if relationship.get("status") == "revoked":
            return self.public_relationship(relationship)
        secret = self.store.secret(relationship_id)
        unsigned = {"kind": "ryn.friend-revocation.v1", "relationship_id": relationship_id, "from": self.peer_id, "to": relationship["peer_id"], "timestamp": int(self.clock().timestamp())}
        wire = {**unsigned, "proof": sign_payload(unsigned, private_key_bytes=self.identity_private).to_dict()}
        revoked = self.store.revoke(
            relationship_id, self.clock().isoformat(), retain_secret=bool(notify), notice=wire if notify else None
        )
        if notify and secret:
            self.retry_revocation(relationship_id)
            revoked = self.store.relationship(relationship_id, active_only=False)
        return self.public_relationship(revoked or relationship)

    def retry_revocation(self, relationship_id: str, *, automatic: bool = False) -> dict[str, Any]:
        relationship = self.store.relationship(relationship_id, active_only=False)
        if not relationship or relationship.get("status") != "revoked":
            raise FriendError("friend_not_found")
        wire = relationship.get("revocation_wire")
        secret = self.store.pending_revocation_secret(relationship_id)
        if not isinstance(wire, dict) or not secret:
            return {"delivered": relationship.get("revocation_delivery") == "delivered"}
        delivered = self._send_wire(relationship, secret, "/api/peer/friends/revoke", wire)
        mailbox_id = ""
        if not delivered and self.queue_mail and (not automatic or not relationship.get("revocation_mailbox_id")):
            try:
                queued = self.queue_mail(relationship, "/api/peer/friends/revoke", wire)
                mailbox_id = str(queued.get("message_id", ""))
            except Exception:
                pass  # Local access is already revoked; keep the durable notice for retry.
        self.store.set_revocation_delivery(
            relationship_id,
            state_value="delivered" if delivered else "pending",
            wire=None if delivered else wire,
            mailbox_id=mailbox_id,
        )
        return {"delivered": delivered}

    def receive_revoke(self, wire: dict[str, Any]) -> None:
        proof = SignedPayload.from_dict(wire["proof"])
        verify_signed_payload(proof)
        unsigned = {key: value for key, value in wire.items() if key != "proof"}
        if proof.payload != unsigned or proof.public_key != wire.get("from") or wire.get("to") != self.peer_id:
            raise FriendError("friend_auth_failed")
        record = self.store.relationship(str(wire["relationship_id"]), active_only=False)
        if not record or record["peer_id"] != wire["from"]:
            raise FriendError("friend_auth_failed")
        self.store.revoke(str(wire["relationship_id"]), self.clock().isoformat())

    def receive_receipt(self, receipt_data: dict[str, Any], sender: str) -> None:
        receipt = SignedPayload.from_dict(receipt_data)
        verify_signed_payload(receipt)
        payload = receipt.payload
        if (receipt.public_key != sender or payload.get("kind") != "ryn.friend-receipt.v1"
            or payload.get("receiver") != sender or payload.get("sender") != self.peer_id):
            raise FriendError("friend_receipt_invalid")
        if payload.get("path") == "/api/peer/friends/revoke":
            record = self.store.relationship(str(payload.get("relationship_id", "")), active_only=False)
            wire = (record or {}).get("revocation_wire")
            if (record and record["peer_id"] == sender and record.get("revocation_delivery") == "delivered"
                and record.get("revocation_request_sha256") == payload.get("request_sha256")):
                return
            if (not record or record["peer_id"] != sender or not isinstance(wire, dict)
                or hashlib.sha256(canonical_json(wire)).hexdigest() != payload.get("request_sha256")):
                raise FriendError("friend_receipt_invalid")
            self.store.set_revocation_delivery(record["relationship_id"], state_value="delivered")
            return
        if payload.get("path") != "/api/peer/friends/message":
            if payload.get("path") != "/api/peer/friends/content-card":
                raise FriendError("friend_receipt_invalid")
            for row in self.store.list_cards():
                if (row.get("dir") == "out" and row.get("to") == sender
                    and row.get("relationship_id") == payload.get("relationship_id")
                    and row.get("request_sha256") == payload.get("request_sha256")):
                    self.store.patch_card(row["card_id"], {"delivered": True, "delivery_state": "delivered", "wire": None, "error": ""})
                    return
            raise FriendError("friend_receipt_invalid")
        with file_transaction(self.store.root / ".delivery.lock"):
            latest = {str(row.get("msg_id")): row for row in self.messages.history(sender)}
            for local in latest.values():
                if (local.get("dir") == "out" and local.get("request_sha256") == payload.get("request_sha256")
                    and (local.get("wire") or {}).get("relationship_id") == payload.get("relationship_id")):
                    local.update(delivered=True, delivery_state="delivered", wire=None, error="")
                    self.messages.append(sender, local)
                    return

    def history(self, peer_id: str) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for row in self.messages.history(peer_id):
            latest[str(row.get("msg_id", ""))] = row
        return [{key: value for key, value in row.items() if key in {
            "msg_id", "kind", "text", "ts", "dir", "from", "to", "delivered", "delivery_state",
            "attachment", "error", "expires_at"
        }} for row in latest.values()]

    def retry(self, peer_id: str, *, automatic: bool = False, limit: int = 10) -> dict[str, int]:
        self._relationship(peer_id)
        attempted = delivered = 0
        latest = {str(row.get("msg_id")): row for row in self.messages.history(peer_id)}
        cards = [row for row in self.store.list_cards() if row.get("dir") == "out" and row.get("to") == peer_id]
        for row in sorted([*latest.values(), *cards], key=lambda row: float(row.get("last_attempt_unix", 0))):
            if attempted >= limit:
                break
            wire = row.get("wire")
            allowed = {"queued", "mailbox"} if automatic else {"queued", "mailbox", "failed"}
            if row.get("delivery_state") not in allowed or not isinstance(wire, dict):
                continue
            attempted += 1
            row = self._attempt_delivery(peer_id, row, explicit_retry=not automatic)
            if row.get("delivered"):
                delivered += 1
        return {"attempted": attempted, "delivered": delivered}
