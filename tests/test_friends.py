from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from rynmesh.crypto import canonical_json
from rynmesh.friends.crypto import FriendCryptoError, auth_headers, parse_invite, validate_endpoint
from rynmesh.friends.service import FriendError, FriendService
from rynmesh.friends.store import FriendStore
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


class Mesh:
    def __init__(self) -> None:
        self.nodes: dict[str, FriendService] = {}
        self.online: set[str] = set()
        self.last_headers: dict[str, str] = {}

    def add(self, endpoint: str, node: FriendService) -> None:
        self.nodes[endpoint] = node
        self.online.add(endpoint)

    def post(
        self,
        endpoint: str,
        path: str,
        payload: dict,
        headers: dict | None,
        **_kwargs,
    ) -> dict:
        if endpoint not in self.online:
            raise OSError("offline")
        node = self.nodes[endpoint]
        if path == "/api/peer/friends/accept":
            return node.accept(payload)
        assert headers is not None
        self.last_headers = dict(headers)
        relationship = node.verify_request(
            path=path, body=canonical_json(payload), headers=headers
        )
        if path == "/api/peer/friends/message":
            node.receive_message(payload)
            return node.delivery_receipt(path, payload)
        if path == "/api/peer/friends/content-card":
            node.receive_content_card(payload)
            return node.delivery_receipt(path, payload)
        if path == "/api/peer/friends/content-card/fetch":
            return node.serve_content_card(payload, relationship)
        if path == "/api/peer/friends/revoke":
            node.receive_revoke(payload)
            return node.delivery_receipt(path, payload)
        raise AssertionError(path)


def _node(tmp_path, name: str, port: int, mesh: Mesh) -> FriendService:
    home = tmp_path / name
    store = RynmeshStore(home=home, network_dir=tmp_path / "network", node_name=name)
    msg = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    endpoint = f"http://127.0.0.1:{port}"
    service = FriendService(
        home=home,
        peer_id=store.peer_id,
        node_name=name,
        endpoint=endpoint,
        identity_private=store.private_key_bytes,
        messaging_private=msg,
        post_json=mesh.post,
        allow_loopback=True,
    )
    mesh.add(endpoint, service)
    return service


def test_invite_listing_expires_without_an_accept_attempt_and_preserves_terminal_states(tmp_path):
    mesh = Mesh()
    alice = _node(tmp_path, 'alice', 18781, mesh)
    bob = _node(tmp_path, 'bob', 18782, mesh)
    now = datetime.now(UTC)
    alice.clock = lambda: now
    expired = alice.create_invite()
    cancelled = alice.create_invite()
    alice.cancel_invite(cancelled['invite']['invite_id'])
    used = alice.create_invite()
    bob.join(used['invite_uri'])
    before = alice.store.state()
    assert next(row for row in alice.list_invites() if row['invite_id'] == expired['invite']['invite_id'])['status'] == 'active'
    alice.clock = lambda: now + timedelta(minutes=16)
    listed = {row['invite_id']: row for row in alice.list_invites()}
    assert listed[expired['invite']['invite_id']]['status'] == 'expired'
    assert listed[cancelled['invite']['invite_id']]['status'] == 'cancelled'
    assert listed[used['invite']['invite_id']]['status'] == 'used'
    assert all('secret_hash' not in row for row in listed.values())
    assert alice.store.state() == before


def _pair(tmp_path):
    mesh = Mesh()
    alice = _node(tmp_path, "Alice", 18081, mesh)
    bob = _node(tmp_path, "Bob", 18082, mesh)
    invite = alice.create_invite()
    preview = bob.inspect_invite(invite["invite_uri"])
    assert preview["node_name"] == "Alice"
    bob_friend = bob.join(invite["invite_uri"])
    alice_friend = alice.store.relationship_for_peer(bob.peer_id)
    assert alice_friend and bob_friend["peer_id"] == alice.peer_id
    assert alice.store.secret(alice_friend["relationship_id"]) == bob.store.secret(
        bob_friend["relationship_id"]
    )
    return mesh, alice, bob


def test_pair_message_attachment_card_restart_and_revoke(tmp_path) -> None:
    mesh, alice, bob = _pair(tmp_path)
    before_roots = RynmeshStore(
        home=tmp_path / "Alice", network_dir=tmp_path / "network"
    ).trusted_root_peer_ids
    row = bob.send_message(alice.peer_id, text="hello")
    assert row["delivery_state"] == "delivered"
    assert alice.history(bob.peer_id)[-1]["text"] == "hello"
    assert "X-Ryn-Friend-MAC" in mesh.last_headers

    attachment = bob.send_message(
        alice.peer_id,
        attachment={"filename": "note.txt", "mime": "text/plain", "bytes": b"private"},
    )
    assert attachment["delivered"] is True
    received_attachment = alice.history(bob.peer_id)[-1]
    assert alice.messages.load_attachment(received_attachment["attachment"]["blob_id"]) == b"private"

    shared = b"# Private reading note\n\nOnly fetched after explicit review."
    imported: list[dict] = []
    bob.resolve_content = lambda _library_id: {
        "data": shared,
        "publisher_peer_id": bob.peer_id,
        "filename": "reading.md",
        "mime": "text/markdown",
        "manifest_ref": "sha256:source-manifest",
    }

    def import_shared(resource: dict) -> dict:
        imported.append(resource)
        return {"library_id": "import:received", "title": "reading.md"}

    alice.import_content = import_shared
    result = bob.send_content_card(
        alice.peer_id,
        {"library_id": "import:1", "title": "A card", "summary": "metadata only", "kind": "file"},
    )
    assert result["delivered"] is True
    received = next(row for row in alice.content_cards() if row["dir"] == "in")
    assert received["card"]["title"] == "A card"
    assert received["fetch_state"] == "available"
    assert received["card"]["sha256"]
    assert imported == []  # Receiving a metadata card never fetches its body.
    fetched = alice.fetch_content_card(received["card_id"])
    assert fetched["library_id"] == "import:received"
    assert fetched["sha256_verified"] is True
    assert imported[0]["data"] == shared
    assert alice.fetch_content_card(received["card_id"])["already_fetched"] is True

    denied = bob.send_content_card(
        alice.peer_id,
        {"library_id": "import:2", "title": "After revoke", "kind": "file"},
    )
    denied_card_id = denied["card_id"]

    alice_relation = alice.store.relationship_for_peer(bob.peer_id)
    assert alice_relation
    restarted = _node(tmp_path, "Alice", 18081, mesh)
    assert restarted.store.relationship_for_peer(bob.peer_id)
    assert restarted.history(bob.peer_id)

    bob.revoke(str(alice_relation["relationship_id"]))
    assert bob.store.relationship_for_peer(alice.peer_id) is None
    assert restarted.store.relationship_for_peer(bob.peer_id) is None
    with pytest.raises(FriendError, match="active_friend_required"):
        bob.send_message(alice.peer_id, text="blocked")
    with pytest.raises(FriendError, match="active_friend_required"):
        restarted.fetch_content_card(denied_card_id)
    after_roots = RynmeshStore(
        home=tmp_path / "Alice", network_dir=tmp_path / "network"
    ).trusted_root_peer_ids
    assert before_roots == after_roots == ()


def test_offline_message_is_queued_and_retry_delivers(tmp_path) -> None:
    mesh, alice, bob = _pair(tmp_path)
    mesh.online.remove(alice.endpoint)
    queued = bob.send_message(alice.peer_id, text="later")
    assert queued["delivery_state"] == "queued"
    mesh.online.add(alice.endpoint)
    assert bob.retry(alice.peer_id) == {"attempted": 1, "delivered": 1}
    assert alice.history(bob.peer_id)[-1]["text"] == "later"
    assert bob.history(alice.peer_id)[-1]["delivery_state"] == "delivered"


def test_invite_one_time_expiry_tamper_and_concurrent_winner(tmp_path) -> None:
    mesh = Mesh()
    alice = _node(tmp_path, "Alice", 18081, mesh)
    bob = _node(tmp_path, "Bob", 18082, mesh)
    carol = _node(tmp_path, "Carol", 18083, mesh)
    invite = alice.create_invite()
    bob.join(invite["invite_uri"])
    with pytest.raises(FriendError, match="invite_used"):
        carol.join(invite["invite_uri"])

    tampered = invite["invite_uri"][:-1] + ("A" if invite["invite_uri"][-1] != "A" else "B")
    with pytest.raises(FriendCryptoError):
        parse_invite(tampered, allow_loopback=True)

    def clock() -> datetime:
        return datetime.now(UTC) - timedelta(days=2)
    old = FriendService(
        home=tmp_path / "Old",
        peer_id=RynmeshStore(home=tmp_path / "Old", network_dir=tmp_path / "network").peer_id,
        node_name="Old",
        endpoint="http://127.0.0.1:18084",
        identity_private=RynmeshStore(home=tmp_path / "Old", network_dir=tmp_path / "network").private_key_bytes,
        messaging_private=peer_box.load_or_create_messaging_key(tmp_path / "Old" / "messaging.x25519"),
        post_json=mesh.post,
        allow_loopback=True,
        clock=clock,
    )
    expired = old.create_invite(ttl_minutes=1)["invite_uri"]
    with pytest.raises(FriendCryptoError, match="invite_expired"):
        parse_invite(expired, now=datetime.now(UTC), allow_loopback=True)

    fresh = alice.create_invite()
    signed, secret = parse_invite(fresh["invite_uri"], allow_loopback=True)
    wins: list[bool] = []
    barrier = threading.Barrier(3)

    stores = [alice.store, FriendStore(alice.home)]

    def consume(store: FriendStore) -> None:
        barrier.wait()
        wins.append(store.consume_invite(signed.payload["invite_id"], signed.payload["secret_hash"], datetime.now(UTC).isoformat()))

    threads = [threading.Thread(target=consume, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert sorted(wins) == [False, True]
    assert secret


def test_hmac_replay_timestamp_and_endpoint_guards(tmp_path) -> None:
    mesh, alice, bob = _pair(tmp_path)
    relationship = alice.store.relationship_for_peer(bob.peer_id)
    assert relationship
    secret = alice.store.secret(relationship["relationship_id"])
    assert secret
    payload = {"x": 1}
    body = canonical_json(payload)
    now = 1_800_000_000
    headers = auth_headers(
        secret,
        method="POST",
        path="/p",
        body=body,
        sender=bob.peer_id,
        receiver=alice.peer_id,
        relationship_id=relationship["relationship_id"],
        timestamp=now,
        nonce="unique",
    )
    original_clock = alice.clock
    alice.clock = lambda: datetime.fromtimestamp(now, UTC)
    try:
        with pytest.raises(FriendError):
            alice.verify_request(path="/wrong", body=body, headers=headers)
        with pytest.raises(FriendError):
            alice.verify_request(path="/p", body=canonical_json({"x": 2}), headers=headers)
        wrong_peer = {**headers, "X-Ryn-Friend-Peer": "wrong"}
        with pytest.raises(FriendError):
            alice.verify_request(path="/p", body=body, headers=wrong_peer)
        alice.verify_request(path="/p", body=body, headers={key.lower(): value for key, value in headers.items()})
        with pytest.raises(FriendError):
            alice.verify_request(path="/p", body=body, headers={key.lower(): value for key, value in headers.items()})
        stale = {**headers, "X-Ryn-Friend-Time": str(now - 121)}
        with pytest.raises(FriendError):
            alice.verify_request(path="/p", body=body, headers={key.lower(): value for key, value in stale.items()})
    finally:
        alice.clock = original_clock

    for endpoint in (
        "http://127.0.0.1:80",
        "http://169.254.169.254/latest",
        "http://0.0.0.0:80",
        "ftp://192.168.1.2/file",
        "https://example.com/friend",
    ):
        with pytest.raises(FriendCryptoError):
            validate_endpoint(endpoint)


def test_attachment_boundary_cancel_nonce_bound_and_torn_history(tmp_path) -> None:
    mesh, alice, bob = _pair(tmp_path)
    mesh.online.remove(alice.endpoint)
    exact = bob.send_message(
        alice.peer_id,
        attachment={"filename": "five.bin", "mime": "application/octet-stream", "bytes": b"x" * (5 * 1024 * 1024)},
    )
    assert exact["delivery_state"] == "queued"
    with pytest.raises(FriendError, match="attachment_too_large"):
        bob.send_message(
            alice.peer_id,
            attachment={"filename": "too-large.bin", "bytes": b"x" * (5 * 1024 * 1024 + 1)},
        )

    invite = bob.create_invite()
    cancelled = bob.cancel_invite(invite["invite"]["invite_id"])
    assert cancelled["status"] == "cancelled"
    with pytest.raises(FriendError):
        alice.join(invite["invite_uri"])

    relationship = bob.store.relationship_for_peer(alice.peer_id)
    assert relationship
    for index in range(520):
        accepted = bob.store.remember_nonce(relationship["relationship_id"], f"nonce-{index}", now=100, timestamp=100)
        assert accepted is (index < 512)
    assert len(bob.store.state()["nonces"][relationship["relationship_id"]]) == 512

    path = bob.messages._conv_path(alice.peer_id)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"torn":')
    assert any(row.get("msg_id") == exact["msg_id"] for row in bob.history(alice.peer_id))


def test_offline_revoke_is_local_first_and_retries_after_restart(tmp_path) -> None:
    mesh, alice, bob = _pair(tmp_path)
    relationship = bob.store.relationship_for_peer(alice.peer_id)
    assert relationship
    relationship_id = relationship["relationship_id"]
    mesh.online.remove(alice.endpoint)
    revoked = bob.revoke(relationship_id)
    assert revoked["status"] == "revoked"
    assert revoked["revocation_delivery"] == "pending"
    assert bob.store.secret(relationship_id) is None
    assert bob.store.pending_revocation_secret(relationship_id)
    with pytest.raises(FriendError, match="active_friend_required"):
        bob.send_message(alice.peer_id, text="stopped locally")

    restarted = _node(tmp_path, "Bob", 18082, mesh)
    mesh.online.add(alice.endpoint)
    assert restarted.retry_revocation(relationship_id) == {"delivered": True}
    assert restarted.store.pending_revocation_secret(relationship_id) is None
    assert alice.store.relationship_for_peer(bob.peer_id) is None
