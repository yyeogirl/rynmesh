"""Friend operations and signed receipts over the existing encrypted mailbox."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from ..mailbox import MailboxError

OPERATION_KIND = "friend.operation.v1"
RECEIPT_KIND = "friend.receipt.v1"
MESSAGE_PATH = "/api/peer/friends/message"


def wire_mailbox(*, mailbox: Any, service: Callable) -> None:
    """Late-bound service callbacks survive route-package reinstallation."""

    def queue(record: dict, path: str, wire: dict, *, expires_at: str | None = None) -> dict:
        ttl = 3600
        if expires_at:
            ttl = min(ttl, int((datetime.fromisoformat(expires_at) - service().clock()).total_seconds()))
            if ttl <= 0:
                raise MailboxError("message_expired")
        return mailbox.deposit(
            record["peer_id"], OPERATION_KIND, {"path": path, "wire": wire},
            ttl_s=ttl, to_messaging_pub=record["messaging_pub"],
        )

    service().queue_mail = queue

    def operation(envelope, body):
        current = service()
        wire, path = body.get("wire"), body.get("path")
        if (not isinstance(wire, dict) or wire.get("from") != envelope.from_peer_id
            or wire.get("to") != current.peer_id):
            raise ValueError("friend_mailbox_sender_mismatch")
        # The mailbox signature binds the sender; the recipient still enforces
        # the live relationship and decrypts using its pinned messaging key.
        if path not in {MESSAGE_PATH, "/api/peer/friends/revoke", "/api/peer/friends/content-card"}:
            raise ValueError("friend_mailbox_operation_unsupported")
        if path in {MESSAGE_PATH, "/api/peer/friends/content-card"}:
            if path == MESSAGE_PATH:
                current.receive_message(wire)
            else:
                current.receive_content_card(wire)
            record, _ = current._relationship(envelope.from_peer_id)
        else:
            record = current.store.relationship(str(wire.get("relationship_id", "")), active_only=False)
            if not record or record["peer_id"] != envelope.from_peer_id:
                raise ValueError("friend_mailbox_sender_mismatch")
            current.receive_revoke(wire)
        receipt = current.delivery_receipt(path, wire)
        mailbox.deposit(envelope.from_peer_id, RECEIPT_KIND, receipt,
                        ttl_s=3600, to_messaging_pub=record["messaging_pub"])

    def receipt(envelope, body):
        if not isinstance(body.get("receipt"), dict):
            raise ValueError("friend_receipt_invalid")
        service().receive_receipt(body["receipt"], envelope.from_peer_id)

    mailbox.register_handler(OPERATION_KIND, operation, replace=True)
    mailbox.register_handler(RECEIPT_KIND, receipt, replace=True)
