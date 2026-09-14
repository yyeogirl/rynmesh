"""Two real nodes exchange mailbox mail through a real registry server.

Nothing is stubbed: a uvicorn registry on a loopback port, two `create_app`
nodes with their own homes and identities, a shared network key on the wire,
and auto-registration carrying each node's messaging key into its signed
record. This is the path a pairing message actually takes when neither peer
can reach the other's endpoint.
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from typing import Any

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("fastapi")

NETWORK_KEY = "two-node-mailbox-key"
NETWORK_ID = "mailboxnet"
KIND = "friend.invite.accept.v1"
PEER_MESSAGE_KIND = "peer.message.v1"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _node(tmp_path, name: str, monkeypatch):
    """A node app on its own home, built with the environment as it stands."""
    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    home = tmp_path / name
    monkeypatch.setenv("RYNMESH_HOME", str(home))
    store = RynmeshStore(home=home, network_dir=tmp_path / f"{name}-net")
    return create_app(store), store


@contextmanager
def _registry_server(tmp_path, monkeypatch):
    """A real registry on a loopback port, with the node env pointed at it.

    Yields the `FilePeerRegistry` behind it so a test can inspect the spool on
    disk. Nodes are given no peer endpoint: they are reachable only through the
    registry, which is exactly the case the mailbox exists for.
    """

    uvicorn = pytest.importorskip("uvicorn")
    from rynmesh.registry import FilePeerRegistry
    from rynmesh.registry_http import create_app as create_registry_app

    port = _free_port()
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", NETWORK_KEY)
    monkeypatch.setenv("RYNMESH_REGISTRY_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("RYNMESH_NETWORK_ID", NETWORK_ID)
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "1")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    monkeypatch.delenv("RYNMESH_PEER_PORT", raising=False)
    monkeypatch.delenv("RYNMESH_PEER_ENDPOINT", raising=False)
    monkeypatch.delenv("RYNMESH_LOCAL_TOKEN", raising=False)

    registry = FilePeerRegistry(tmp_path / "registry")
    server = uvicorn.Server(
        uvicorn.Config(
            create_registry_app(registry), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started, "registry server did not start"
        yield registry, port
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_two_nodes_exchange_mail_through_a_registry_server(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    with _registry_server(tmp_path, monkeypatch) as (registry, port):
        alice_app, alice_store = _node(tmp_path, "alice", monkeypatch)
        bob_app, bob_store = _node(tmp_path, "bob", monkeypatch)

        received: list[tuple[str, dict]] = []

        def handler(envelope: Any, body: dict) -> None:
            received.append((envelope.from_peer_id, body))

        bob_app.state.mailbox.register_handler(KIND, handler)

        with TestClient(alice_app) as alice_client, TestClient(bob_app) as bob_client:
            # Auto-registration ran in each lifespan; the messaging key is in
            # the signed record, which is the only way Alice can seal for Bob.
            peers = alice_store.discover_peers(network_id=NETWORK_ID, include_self=True)["peers"]
            advertised = {
                item["peer_id"]: (item.get("metadata") or {}).get("messaging_pub", "")
                for item in peers
            }
            assert advertised.get(bob_store.peer_id), "Bob's messaging key was not advertised"
            assert advertised.get(alice_store.peer_id)
            # Neither node published a reachable endpoint, so the direct
            # /api/peer/pubkey lookup cannot be what resolves the key below.
            assert not [
                endpoint
                for item in peers
                for endpoint in item.get("endpoints", [])
                if str(endpoint).startswith("http")
            ]

            receipt = alice_app.state.mailbox.deposit(
                bob_store.peer_id, KIND, {"invite_id": "xyz", "accepted": True}
            )
            assert receipt["message_id"]

            assert bob_app.state.mailbox.poll_once() == 1
            assert received == [(alice_store.peer_id, {"invite_id": "xyz", "accepted": True})]

            # The ack rides the next poll and empties the registry-side box.
            assert bob_app.state.mailbox.poll_once() == 0
            assert sorted((registry.root / "mailbox").rglob("*.json")) == []

            status = bob_client.get("/api/local/mailbox/status").json()
            assert status["handled_total"] == 1
            assert status["dropped_total"] == 0
            assert status["last_error"] == ""
            # `create_app` also registers the peer-message relay handler.
            from rynmesh.friends.mailbox import OPERATION_KIND, RECEIPT_KIND
            assert status["handlers"] == sorted([KIND, PEER_MESSAGE_KIND, OPERATION_KIND, RECEIPT_KIND])
            assert status["worker"]["name"] == "mailbox.poll"

            # The peer surface really is keyed: the same registry refuses a
            # caller without the shared header.
            import urllib.error
            import urllib.request

            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/mailbox/poll", data=b"{}", method="POST"
            )
            with pytest.raises(urllib.error.HTTPError) as unkeyed:
                urllib.request.urlopen(request, timeout=10)
            assert unkeyed.value.code == 404

            assert alice_client.get("/api/local/mailbox/status").json()["handled_total"] == 0


def test_a_peer_message_survives_a_dead_direct_transport(tmp_path, monkeypatch) -> None:
    """Alice's chat message reaches Bob through the mailbox, not the wire.

    `RYNMESH_MESSAGING_FORCE_MAILBOX=1` makes Alice's direct transport report
    failure without trying, so the only route left is store-and-forward.
    """

    from fastapi.testclient import TestClient

    monkeypatch.setenv("RYNMESH_MESSAGING_FORCE_MAILBOX", "1")
    with _registry_server(tmp_path, monkeypatch) as (registry, _port):
        alice_app, alice_store = _node(tmp_path, "alice", monkeypatch)
        bob_app, bob_store = _node(tmp_path, "bob", monkeypatch)

        with TestClient(alice_app) as alice_client, TestClient(bob_app) as bob_client:
            sent = alice_client.post(
                "/api/local/messages/send",
                json={"peer_id": bob_store.peer_id, "text": "carried by the mailbox"},
            ).json()
            assert sent["delivered"] is False
            assert sent["via"] == "mailbox"

            assert bob_app.state.mailbox.poll_once() == 1

            history = bob_client.get(
                "/api/local/messages", params={"peer_id": alice_store.peer_id}
            ).json()
            assert [(item["dir"], item["text"], item["from"]) for item in history] == [
                ("in", "carried by the mailbox", alice_store.peer_id)
            ]
            # Bob decrypted it, so the plaintext never travelled: the registry
            # only ever held the sealed envelope, and the box is drained now.
            assert bob_app.state.mailbox.poll_once() == 0
            assert sorted((registry.root / "mailbox").rglob("*.json")) == []

            assert bob_client.get("/api/local/mailbox/status").json()["handled_total"] == 1
            assert alice_client.get(
                "/api/local/messages", params={"peer_id": bob_store.peer_id}
            ).json()[0]["via"] == "mailbox"


def test_the_poll_worker_publishes_to_the_sse_stream_from_its_own_thread(
    tmp_path, monkeypatch
) -> None:
    """The supervised worker's publish has to cross a thread boundary safely.

    `BackgroundWorkerRegistry` runs `run_once` through `asyncio.to_thread`, so
    the mailbox handler — and the SSE fan-out it calls — execute off the event
    loop. `asyncio.Queue` is not thread-safe there: the put has to be handed
    back to the loop that owns the subscriber queues.

    The loop runs in debug mode on purpose. That turns the non-thread-safe
    wakeup into the RuntimeError it really is, instead of a silent race that
    happens to work whenever something else wakes the loop in time.
    """

    import asyncio

    from fastapi.testclient import TestClient

    import rynmesh.mailbox_routes as mailbox_routes

    monkeypatch.setenv("RYNMESH_MESSAGING_FORCE_MAILBOX", "1")
    monkeypatch.setattr(mailbox_routes, "MAILBOX_POLL_INITIAL_DELAY_S", 0.05)
    with _registry_server(tmp_path, monkeypatch) as (_registry, _port):
        alice_app, alice_store = _node(tmp_path, "alice", monkeypatch)
        bob_app, bob_store = _node(tmp_path, "bob", monkeypatch)

        # Bob registers (that is what publishes his messaging key), then goes
        # away again so the message is waiting in the registry when the worker
        # under test starts. Nothing is in his box during this window.
        with TestClient(bob_app):
            pass

        with TestClient(alice_app) as alice_client:
            sent = alice_client.post(
                "/api/local/messages/send",
                json={"peer_id": bob_store.peer_id, "text": "published from a worker thread"},
            ).json()
            assert sent["via"] == "mailbox"

        async def scenario() -> dict:
            # The real lifespan: it captures the loop and starts the workers.
            async with bob_app.router.lifespan_context(bob_app):
                queue: asyncio.Queue = asyncio.Queue()
                bob_app.state.message_subscribers.append(queue)
                assert bob_app.state.loop is asyncio.get_running_loop()
                try:
                    return await queue.get()
                finally:
                    bob_app.state.message_subscribers.remove(queue)

        # Driven from a daemon thread rather than awaited with a timeout: an
        # unsafe cross-thread wakeup leaves the getter's future resolved but
        # never scheduled, which no in-loop timeout can cancel its way out of.
        # The join bound turns that into a failed assertion instead of a hang.
        outcome: list[Any] = []
        runner = threading.Thread(
            target=lambda: outcome.append(asyncio.run(scenario(), debug=True)),
            daemon=True,
        )
        runner.start()
        runner.join(timeout=60)
        assert outcome, "the worker's publish never reached the SSE subscriber"
        [record] = outcome
        assert record["text"] == "published from a worker thread"
        assert record["from"] == alice_store.peer_id
        assert record["dir"] == "in"
        # The loop handle is released on shutdown, so a late publish falls back
        # to the plain put rather than touching a closed loop.
        assert bob_app.state.loop is None


def _queued_header(registry, recipient_store) -> dict:
    """The sealed peer-message header sitting in the recipient's registry box.

    Opening it with the recipient's own messaging key is exactly what the poll
    worker would do; pulling it out here lets a test deliver the *same* message
    twice by two different routes.
    """

    import json as _json

    from rynmesh.crypto import SignedPayload
    from rynmesh.mailbox import open_mailbox_message
    from rynmesh.services import peer_box

    [path] = sorted((registry.root / "mailbox").rglob("*.json"))
    stored = _json.loads(path.read_text(encoding="utf-8"))
    key = peer_box.load_or_create_messaging_key(recipient_store.home / "messaging.x25519")
    _envelope, body = open_mailbox_message(
        SignedPayload.from_dict(stored["signed"]),
        my_peer_id=recipient_store.peer_id,
        messaging_private_key=key,
        kind=PEER_MESSAGE_KIND,
    )
    return body


def _post_direct(client, header: dict):
    from rynmesh.transport import network_key_header

    return client.post("/api/peer/msg", json=header, headers=network_key_header())


@pytest.mark.parametrize("direct_first", [True, False])
def test_the_same_message_by_both_routes_lands_once(
    tmp_path, monkeypatch, direct_first: bool
) -> None:
    """A direct POST whose response was lost, then the mailbox retry — or vice versa.

    Both orders must leave exactly one history line: `receive` is the single
    point that decides a message has already been stored.
    """

    from fastapi.testclient import TestClient

    monkeypatch.setenv("RYNMESH_MESSAGING_FORCE_MAILBOX", "1")
    with _registry_server(tmp_path, monkeypatch) as (registry, _port):
        alice_app, alice_store = _node(tmp_path, "alice", monkeypatch)
        bob_app, bob_store = _node(tmp_path, "bob", monkeypatch)

        with TestClient(alice_app) as alice_client, TestClient(bob_app) as bob_client:
            sent = alice_client.post(
                "/api/local/messages/send",
                json={"peer_id": bob_store.peer_id, "text": "exactly once"},
            ).json()
            assert sent["via"] == "mailbox"

            header = _queued_header(registry, bob_store)

            if direct_first:
                assert _post_direct(bob_client, header).status_code == 200
                assert bob_app.state.mailbox.poll_once() == 1  # handled, as a no-op
            else:
                assert bob_app.state.mailbox.poll_once() == 1
                assert _post_direct(bob_client, header).status_code == 200

            history = bob_client.get(
                "/api/local/messages", params={"peer_id": alice_store.peer_id}
            ).json()
            assert [(item["dir"], item["text"]) for item in history] == [
                ("in", "exactly once")
            ]
            # The second arrival is not an error: it was handled, not dropped.
            status = bob_client.get("/api/local/mailbox/status").json()
            assert status["handled_total"] == 1 and status["dropped_total"] == 0


def test_a_relayed_header_cannot_claim_another_sender(tmp_path, monkeypatch) -> None:
    """A depositor may only relay messages that say they are from itself."""

    from fastapi.testclient import TestClient

    from rynmesh.mailbox_routes import PEER_MESSAGE_KIND as KIND_UNDER_TEST

    monkeypatch.setenv("RYNMESH_MESSAGING_FORCE_MAILBOX", "1")
    with _registry_server(tmp_path, monkeypatch) as (registry, _port):
        alice_app, alice_store = _node(tmp_path, "alice", monkeypatch)
        bob_app, bob_store = _node(tmp_path, "bob", monkeypatch)
        carol_app, carol_store = _node(tmp_path, "carol", monkeypatch)

        with TestClient(alice_app), TestClient(bob_app) as bob_client, TestClient(carol_app):
            # Carol seals a well-formed peer-message header that names Alice as
            # the sender, and deposits it in Bob's box under her own signature.
            carol_app.state.mailbox.deposit(
                bob_store.peer_id,
                KIND_UNDER_TEST,
                {"v": 1, "from": alice_store.peer_id, "to": bob_store.peer_id,
                 "nonce": "AAAAAAAAAAAAAAAA", "ciphertext": "AAAA", "from_pub": "AAAA"},
            )
            assert carol_store.peer_id != alice_store.peer_id

            # Handler raises -> three attempts, then a drop; nothing is written
            # to Bob's history. The drop's ack rides the fourth poll.
            for _ in range(4):
                bob_app.state.mailbox.poll_once()
            status = bob_client.get("/api/local/mailbox/status").json()
            assert status["handled_total"] == 0
            assert status["dropped_total"] == 1
            assert bob_client.get(
                "/api/local/messages", params={"peer_id": alice_store.peer_id}
            ).json() == []
            assert sorted((registry.root / "mailbox").rglob("*.json")) == []
