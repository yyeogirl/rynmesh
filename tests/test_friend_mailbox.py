from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_friends import _pair

from rynmesh.friends.mailbox import wire_mailbox
from rynmesh.mailbox_client import MailboxClient
from rynmesh.registry import FilePeerRegistry


@pytest.mark.parametrize("kind", ["message", "card"])
def test_concurrent_same_message_sends_use_one_mailbox_slot(tmp_path, kind):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    mesh, alice, bob = _pair(tmp_path)
    alice_mail, _ = mailboxes(tmp_path, alice, bob)
    mesh.online.clear()
    def offline(*args, **kwargs):
        time.sleep(0.1)  # Leave concurrent callers in flight before mailbox deposit.
        raise OSError("offline")
    alice.post_json = offline
    ready = Barrier(6)
    def send(_):
        ready.wait(timeout=5)
        if kind == "message":
            return alice.send_message(bob.peer_id, text="One logical message", message_id="5" * 32)
        return alice.send_content_card(bob.peer_id, {"title": "One logical card"}, card_id="5" * 32)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(send, range(6)))
    assert all(row["delivery_state"] == "mailbox" for row in results)
    slot_count = len(list(alice_mail._store.registry.mailbox.mailbox_dir.rglob("*.json")))
    assert slot_count == 1
    assert len(alice.history(bob.peer_id) if kind == "message" else alice.content_cards()) == 1


def mailboxes(tmp_path, alice, bob):
    registry = FilePeerRegistry(tmp_path / "mailbox-registry")
    clients = []
    for node in (alice, bob):
        store = SimpleNamespace(peer_id=node.peer_id, private_key_bytes=node.identity_private, registry=registry)
        client = MailboxClient(store=store, messaging_key=node.messaging_private,
                               home=node.home, resolve_messaging_pub=lambda key: "")
        wire_mailbox(mailbox=client, service=lambda node=node: node)
        clients.append(client)
    return clients


def test_offline_message_is_only_delivered_after_signed_receipt_and_survives_retry(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    alice_mail, bob_mail = mailboxes(tmp_path, alice, bob)
    mesh.online.clear()
    sent = alice.send_message(bob.peer_id, text="For when you return", message_id="3" * 32)
    assert sent["delivery_state"] == "mailbox" and not sent["delivered"]
    assert bob.history(alice.peer_id) == []
    assert bob_mail.poll_once() == 1
    assert len(bob.history(alice.peer_id)) == 1
    assert alice.history(bob.peer_id)[0]["delivery_state"] == "mailbox"
    assert alice_mail.poll_once() == 1
    assert alice.history(bob.peer_id)[0]["delivery_state"] == "delivered"
    alice_mail.poll_once()
    bob_mail.poll_once()
    assert len(bob.history(alice.peer_id)) == 1


def test_oversize_mail_waits_for_direct_connection_and_expired_message_is_not_resent(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    mailboxes(tmp_path, alice, bob)
    mesh.online.clear()
    sent = alice.send_message(bob.peer_id, attachment={"filename": "large.bin", "bytes": b"x" * 100000})
    assert sent["delivery_state"] == "queued" and not sent["delivered"]
    later = datetime.now(UTC) + timedelta(hours=2)
    alice.clock = lambda: later
    assert alice.retry(bob.peer_id)["delivered"] == 0
    assert alice.history(bob.peer_id)[0]["delivery_state"] == "expired"
    assert bob.history(alice.peer_id) == []


def test_local_revocation_blocks_already_queued_mailbox_message(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    alice_mail, bob_mail = mailboxes(tmp_path, alice, bob)
    mesh.online.clear()
    alice.send_message(bob.peer_id, text="No longer permitted")
    bob.revoke(bob.list_friends()[0]["relationship_id"])
    assert bob_mail.poll_once() == 0
    assert bob.history(alice.peer_id) == []
    assert alice_mail.poll_once() == 1  # Bob's signed removal notice arrives independently.
    assert alice.history(bob.peer_id)[0]["delivery_state"] == "mailbox"


def test_offline_removal_notice_is_durable_and_requires_remote_receipt(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    alice_mail, bob_mail = mailboxes(tmp_path, alice, bob)
    mesh.online.clear()
    relation = alice.list_friends()[0]["relationship_id"]
    removed = alice.revoke(relation)
    assert removed["revocation_delivery"] == "pending"
    assert alice.store.secret(relation) is None
    assert alice.store.relationship(relation, active_only=False)["revocation_wire"]
    assert bob_mail.poll_once() == 1
    assert bob.store.relationship(relation) is None
    assert alice.store.relationship(relation, active_only=False)["revocation_delivery"] == "pending"
    assert alice_mail.poll_once() == 1
    assert alice.store.relationship(relation, active_only=False)["revocation_delivery"] == "delivered"
    assert alice.store.pending_revocation_secret(relation) is None


@pytest.mark.parametrize(("limit", "error"), [
    ("max_pending_per_recipient", "recipient_full"),
    ("max_pending_per_sender", "sender_quota"),
])
def test_full_mailbox_preserves_reason_and_retries_one_message_after_space_returns(tmp_path, limit, error):
    mesh, alice, bob = _pair(tmp_path)
    alice_mail, bob_mail = mailboxes(tmp_path, alice, bob)
    setattr(alice_mail._store.registry.mailbox, limit, 1)
    mesh.online.clear()
    alice.send_message(bob.peer_id, text="First pending", message_id="1" * 32)
    blocked = alice.send_message(bob.peer_id, text="Wait for room", message_id="2" * 32)
    assert blocked["delivery_state"] == "failed" and not blocked["delivered"]
    assert blocked["error"] == error
    assert bob_mail.poll_once() == 1
    bob_mail.poll_once()  # Acknowledge the first envelope and release its slot.
    alice_mail.poll_once()
    alice_mail.poll_once()  # The receipt mailbox also needs its acknowledged slot released.
    assert alice.retry(bob.peer_id)["attempted"] == 1
    assert bob_mail.poll_once() == 1
    alice_mail.poll_once()
    assert len(bob.history(alice.peer_id)) == 2
    assert alice.history(bob.peer_id)[1]["delivery_state"] == "delivered"
    assert alice.send_message(bob.peer_id, text="Wait for room", message_id="2" * 32)["delivered"]
    assert len(bob.history(alice.peer_id)) == 2


@pytest.mark.parametrize("kind", ["message", "card"])
def test_delayed_mailbox_retry_cannot_extend_original_message_expiry(tmp_path, kind):
    mesh, alice, bob = _pair(tmp_path)
    now = [datetime.now(UTC)]
    alice.clock = bob.clock = lambda: now[0]
    mesh.online.clear()
    def send():
        if kind == "message":
            return alice.send_message(bob.peer_id, text="Only for this hour", message_id="4" * 32)
        return alice.send_content_card(bob.peer_id, {"title": "Only for this hour"}, card_id="4" * 32)
    def history(node, peer):
        return node.history(peer.peer_id) if kind == "message" else node.content_cards()
    queued = send()
    assert queued["delivery_state"] == "queued"
    original_expiry = datetime.fromisoformat(queued["expires_at"])
    now[0] += timedelta(minutes=59)
    alice_mail, bob_mail = mailboxes(tmp_path, alice, bob)
    alice_mail._now = bob_mail._now = lambda: now[0]
    alice_mail._store.registry.mailbox._now = lambda: now[0]
    alice.retry(bob.peer_id)
    assert history(alice, bob)[0]["delivery_state"] == "mailbox"
    now[0] = original_expiry + timedelta(seconds=1)
    assert bob_mail.poll_once() == 0
    assert history(bob, alice) == []
    alice.retry(bob.peer_id)
    expired = history(alice, bob)[0]
    assert expired["delivery_state"] == "expired" and not expired["delivered"]
    assert send()["delivery_state"] == "expired"
    assert history(bob, alice) == []


def test_late_valid_receipt_can_resolve_an_expired_unconfirmed_send(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    now = [datetime.now(UTC)]
    alice.clock = bob.clock = lambda: now[0]
    alice_mail, bob_mail = mailboxes(tmp_path, alice, bob)
    alice_mail._now = bob_mail._now = lambda: now[0]
    alice_mail._store.registry.mailbox._now = lambda: now[0]
    mesh.online.clear()
    alice.send_message(bob.peer_id, text="Received before the deadline", message_id="6" * 32)
    now[0] += timedelta(minutes=59)
    assert bob_mail.poll_once() == 1
    now[0] += timedelta(minutes=2)
    alice.retry(bob.peer_id)
    assert alice.history(bob.peer_id)[0]["delivery_state"] == "expired"
    assert alice_mail.poll_once() == 1
    assert alice.history(bob.peer_id)[0]["delivery_state"] == "delivered"
    assert len(bob.history(alice.peer_id)) == 1
