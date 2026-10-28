"""Node-side mailbox: poll worker client, app wiring, messaging-key discovery."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from rynmesh.crypto import SignedPayload
from rynmesh.mailbox import (
    MAX_ACK_IDS,
    MailboxEnvelope,
    MailboxError,
    build_poll_request,
    seal_mailbox_message,
)
from rynmesh.mailbox_client import MailboxClient
from rynmesh.registry import FilePeerRegistry

pytest.importorskip("cryptography")

KIND = "friend.invite.accept.v1"


class _Node:
    """A real store on its own home, pointed at a shared registry."""

    def __init__(self, tmp_path: Path, name: str, registry: Any) -> None:
        from rynmesh.services import peer_box
        from rynmesh.store import RynmeshStore

        self.home = tmp_path / name
        self.store = RynmeshStore(home=self.home, network_dir=tmp_path / f"{name}-net")
        self.store.registry = registry
        self.messaging_key = peer_box.load_or_create_messaging_key(self.home / "messaging.x25519")
        self.messaging_pub = peer_box.public_key_b64(self.messaging_key)

    @property
    def peer_id(self) -> str:
        return self.store.peer_id

    def client(self, **overrides: Any) -> MailboxClient:
        options: dict[str, Any] = dict(
            store=self.store,
            messaging_key=self.messaging_key,
            home=self.home,
            resolve_messaging_pub=lambda peer_id: (_ for _ in ()).throw(
                AssertionError("messaging pub should have been supplied explicitly")
            ),
        )
        options.update(overrides)
        return MailboxClient(**options)


class _Recorder:
    """A handler that remembers what it was given (and can be made to fail)."""

    def __init__(self, failures: int = 0) -> None:
        self.calls: list[tuple[MailboxEnvelope, dict]] = []
        self.failures = failures

    def __call__(self, envelope: MailboxEnvelope, body: dict) -> None:
        self.calls.append((envelope, body))
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("SECRET_HANDLER_FAILURE_MARKER")


class _StubRegistry:
    """Hands back envelopes verbatim — a registry that skipped its own checks."""

    def __init__(self, messages: list[SignedPayload]) -> None:
        self.messages = list(messages)
        self.acked: list[str] = []
        self.polls = 0

    def poll_mailbox(self, signed_poll: SignedPayload) -> list[SignedPayload]:
        self.polls += 1
        acks = set(signed_poll.payload.get("ack", []))
        self.acked.extend(sorted(acks))
        self.messages = [
            item
            for item in self.messages
            if getattr(item, "payload", {}).get("message_id") not in acks
        ]
        return list(self.messages)

    def deposit_mailbox(self, signed: SignedPayload) -> dict[str, Any]:
        self.messages.append(signed)
        return {"message_id": signed.payload["message_id"], "pending": len(self.messages)}


def _pending_files(registry: FilePeerRegistry) -> list[Path]:
    return sorted((registry.root / "mailbox").rglob("*.json"))


def _at(moment: datetime):
    return lambda: moment


# ------------------------------------------------------------------ 1. deliver


def test_deposit_is_delivered_opened_dispatched_and_acked(tmp_path) -> None:
    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)

    sender = alice.client()
    receiver = bob.client()
    handler = _Recorder()
    receiver.register_handler(KIND, handler)

    receipt = sender.deposit(
        bob.peer_id, KIND, {"invite_id": "abc", "note": "yes"},
        to_messaging_pub=bob.messaging_pub,
    )
    assert receipt["message_id"]
    assert len(_pending_files(registry)) == 1

    assert receiver.poll_once() == 1
    [(envelope, body)] = handler.calls
    assert envelope.kind == KIND
    assert envelope.from_peer_id == alice.peer_id
    assert body == {"invite_id": "abc", "note": "yes"}

    # The ack rides the next poll, which is also what empties the spool.
    assert receiver.poll_once() == 0
    assert _pending_files(registry) == []
    assert len(handler.calls) == 1

    status = receiver.status()
    assert status["handled_total"] == 1
    assert status["dropped_total"] == 0
    assert status["handlers"] == [KIND]


def test_register_handler_claims_a_kind_once(tmp_path) -> None:
    registry = FilePeerRegistry(tmp_path / "registry")
    bob = _Node(tmp_path, "bob", registry)
    client = bob.client()

    first, second = _Recorder(), _Recorder()
    client.register_handler(KIND, first)
    with pytest.raises(ValueError, match="already registered"):
        client.register_handler(KIND, second)
    client.register_handler(KIND, second, replace=True)
    assert client.status()["handlers"] == [KIND]
    with pytest.raises(ValueError):
        client.register_handler("  ", first)


# ------------------------------------------------------------------- 2. replay


def test_a_redelivered_message_is_dropped_by_the_seen_cache(tmp_path) -> None:
    """A second registry (or a re-deposit) must not run a handler twice."""

    first = FilePeerRegistry(tmp_path / "registry-a")
    second = FilePeerRegistry(tmp_path / "registry-b")
    alice = _Node(tmp_path, "alice", first)
    bob = _Node(tmp_path, "bob", first)

    handler = _Recorder()
    receiver = bob.client()
    receiver.register_handler(KIND, handler)

    signed = seal_mailbox_message(
        kind=KIND,
        body={"invite_id": "abc"},
        from_private_key_bytes=alice.store.private_key_bytes,
        to_peer_id=bob.peer_id,
        to_messaging_pub=bob.messaging_pub,
    )
    first.deposit_mailbox(signed)
    assert receiver.poll_once() == 1
    assert len(handler.calls) == 1
    assert receiver.poll_once() == 0  # flush the ack; the first box is empty

    # The identical envelope, deposited into a registry that never saw it.
    second.deposit_mailbox(signed)
    bob.store.registry = second
    assert receiver.poll_once() == 0
    assert len(handler.calls) == 1
    assert receiver.status()["dropped_total"] == 1
    # ...and it was acked, so the second registry drains too.
    assert receiver.poll_once() == 0
    assert _pending_files(second) == []


def test_the_seen_cache_survives_a_client_restart(tmp_path) -> None:
    first = FilePeerRegistry(tmp_path / "registry-a")
    second = FilePeerRegistry(tmp_path / "registry-b")
    alice = _Node(tmp_path, "alice", first)
    bob = _Node(tmp_path, "bob", first)

    signed = seal_mailbox_message(
        kind=KIND,
        body={"invite_id": "abc"},
        from_private_key_bytes=alice.store.private_key_bytes,
        to_peer_id=bob.peer_id,
        to_messaging_pub=bob.messaging_pub,
    )
    first.deposit_mailbox(signed)
    original = bob.client()
    original.register_handler(KIND, _Recorder())
    assert original.poll_once() == 1

    seen = bob.home / "mailbox" / "seen.json"
    assert seen.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(seen.stat().st_mode) == 0o600
        assert stat.S_IMODE(seen.parent.stat().st_mode) == 0o700
    stored = json.loads(seen.read_text(encoding="utf-8"))
    assert list(stored["entries"]) == [signed.payload["message_id"]]

    second.deposit_mailbox(signed)
    bob.store.registry = second
    restarted = bob.client()
    handler = _Recorder()
    restarted.register_handler(KIND, handler)
    assert restarted.poll_once() == 0
    assert handler.calls == []
    assert restarted.status()["dropped_total"] == 1


def test_the_seen_cache_is_bounded_and_prunes_expired_entries(tmp_path) -> None:
    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)

    receiver = bob.client(seen_capacity=2)
    handler = _Recorder()
    receiver.register_handler(KIND, handler)
    for index in range(3):
        registry.deposit_mailbox(
            seal_mailbox_message(
                kind=KIND,
                body={"n": index},
                from_private_key_bytes=alice.store.private_key_bytes,
                to_peer_id=bob.peer_id,
                to_messaging_pub=bob.messaging_pub,
            )
        )
    assert receiver.poll_once() == 3

    delivered = [envelope.message_id for envelope, _ in handler.calls]
    entries = json.loads((bob.home / "mailbox" / "seen.json").read_text(encoding="utf-8"))
    assert set(entries["entries"]) == set(delivered[1:])  # oldest evicted first

    # Reloading applies the same bound, and drops the entry that expires first.
    tightened = bob.client(seen_capacity=1)
    tightened._save_seen()
    survivors = json.loads(
        (bob.home / "mailbox" / "seen.json").read_text(encoding="utf-8")
    )["entries"]
    assert len(survivors) == 1
    assert set(survivors) <= set(entries["entries"])
    assert survivors[next(iter(survivors))] == max(entries["entries"].values())

    # An entry whose envelope has expired is not kept forever either.
    later = bob.client(seen_capacity=2, now=_at(datetime.now(timezone.utc) + timedelta(days=2)))
    assert later.status()["handlers"] == []
    later._save_seen()
    assert json.loads(
        (bob.home / "mailbox" / "seen.json").read_text(encoding="utf-8")
    )["entries"] == {}


# ----------------------------------------------------------------- 3. attempts


def test_a_failing_handler_is_retried_until_it_succeeds(tmp_path) -> None:
    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)

    alice.client().deposit(
        bob.peer_id, KIND, {"invite_id": "abc"}, to_messaging_pub=bob.messaging_pub
    )
    receiver = bob.client()
    handler = _Recorder(failures=2)
    receiver.register_handler(KIND, handler)

    assert receiver.poll_once() == 0
    assert len(_pending_files(registry)) == 1, "a failed handler must not ack"
    assert receiver.poll_once() == 0
    assert len(_pending_files(registry)) == 1
    assert receiver.poll_once() == 1
    assert len(handler.calls) == 3

    assert receiver.poll_once() == 0
    assert _pending_files(registry) == []
    status = receiver.status()
    assert status["handled_total"] == 1
    assert status["dropped_total"] == 0


def test_a_poison_message_is_dropped_after_max_attempts(tmp_path) -> None:
    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)

    alice.client().deposit(
        bob.peer_id, KIND, {"invite_id": "abc"}, to_messaging_pub=bob.messaging_pub
    )
    receiver = bob.client(max_attempts=3)
    handler = _Recorder(failures=99)
    receiver.register_handler(KIND, handler)

    for _ in range(3):
        assert receiver.poll_once() == 0
    assert len(handler.calls) == 3
    status = receiver.status()
    assert status["dropped_total"] == 1
    assert status["handled_total"] == 0

    # Acked on the give-up poll, so it is gone and never seen again.
    assert receiver.poll_once() == 0
    assert _pending_files(registry) == []
    assert len(handler.calls) == 3


def test_the_attempt_counter_survives_a_restart(tmp_path) -> None:
    """A poison message must not reset its retry budget by outliving the process.

    Delivery is at-least-once, so a message whose handler always raises comes
    back on every poll. If the counter lived only in memory, restarting the node
    (or crashing on it) would start the three attempts over — forever, for as
    long as the message's TTL allows.
    """

    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)

    alice.client().deposit(
        bob.peer_id, KIND, {"invite_id": "abc"}, to_messaging_pub=bob.messaging_pub
    )
    handler = _Recorder(failures=99)

    first = bob.client(max_attempts=3)
    first.register_handler(KIND, handler)
    for _ in range(2):
        assert first.poll_once() == 0
    assert len(handler.calls) == 2
    assert first.status()["dropped_total"] == 0
    stored = json.loads((bob.home / "mailbox" / "seen.json").read_text(encoding="utf-8"))
    [(message_id, counter)] = stored["attempts"].items()
    assert counter["count"] == 2
    # Counts and expiries only: nothing about the message itself.
    assert set(counter) == {"count", "expires"}

    # A brand-new client on the same home picks the budget up where it was.
    restarted = bob.client(max_attempts=3)
    restarted.register_handler(KIND, handler)
    assert restarted.poll_once() == 0
    assert len(handler.calls) == 3, "the third attempt is the last one"
    assert restarted.status()["dropped_total"] == 1

    # Acked on the give-up poll; the spool is empty and the counter is gone.
    assert restarted.poll_once() == 0
    assert _pending_files(registry) == []
    assert len(handler.calls) == 3
    assert json.loads(
        (bob.home / "mailbox" / "seen.json").read_text(encoding="utf-8")
    )["attempts"] == {}
    assert message_id not in json.loads(
        (bob.home / "mailbox" / "seen.json").read_text(encoding="utf-8")
    )["attempts"]


def test_a_message_that_will_not_decrypt_is_retried_then_given_up_on(
    tmp_path, caplog
) -> None:
    """A rotated messaging key must not destroy mail on the first look.

    An envelope that verifies but will not open is a different failure from a
    bad envelope: the shell is fine and it really is addressed to us, so acking
    it straight away would delete mail the right key could still read. It gets
    the same retry budget a failing handler gets — and then, so it cannot sit at
    the head of the box forever, it is acked and dropped.
    """

    import logging

    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)
    stranger = _Node(tmp_path, "stranger", registry)

    # Addressed to Bob, but sealed to somebody else's messaging key — exactly
    # what a rotation leaves behind for messages already in flight.
    alice.client().deposit(
        bob.peer_id, KIND, {"secret": "SECRET_BODY_MARKER"},
        to_messaging_pub=stranger.messaging_pub,
    )
    receiver = bob.client(max_attempts=3)
    handler = _Recorder()
    receiver.register_handler(KIND, handler)

    with caplog.at_level(logging.DEBUG, logger="rynmesh.mailbox_client"):
        for poll in range(3):
            assert receiver.poll_once() == 0
            if poll < 2:
                assert len(_pending_files(registry)) == 1, "not acked while retries remain"

    assert handler.calls == []
    status = receiver.status()
    # Every look counts, so a key rotation stays visible in the status.
    assert status["undecryptable"] == 3
    assert status["dropped_total"] == 1
    assert status["handled_total"] == 0

    # The give-up ack rides the next poll, and the box is empty afterwards.
    assert receiver.poll_once() == 0
    assert _pending_files(registry) == []
    assert receiver.status()["undecryptable"] == 3

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "SECRET_BODY_MARKER" not in text
    assert "MailboxError" in text
    # One line per poll, not one per message.
    assert len([1 for record in caplog.records if "could not be opened" in record.getMessage()]) == 3


def test_undecryptable_mail_at_the_head_of_a_box_does_not_hide_newer_mail(tmp_path) -> None:
    """A poll serves the oldest 50 and has no cursor.

    So a wall of never-acked messages at the head of a box would hide every
    newer message behind it for a full TTL. The retry budget is what keeps the
    queue moving.
    """

    from rynmesh.mailbox import MAX_POLL_LIMIT
    from rynmesh.mailbox_store import FileMailboxStore

    registry = FilePeerRegistry(tmp_path / "registry")
    # A wall this size is many senders' worth in a real mesh (the per-sender
    # quota is 16); one sender is simply the cheapest way to build it.
    registry._mailbox = FileMailboxStore(registry.root, max_pending_per_sender=10_000)
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)
    stranger = _Node(tmp_path, "stranger", registry)

    sender = alice.client()
    # A full poll batch of mail Bob cannot open, all older than the good one.
    for index in range(MAX_POLL_LIMIT):
        sender.deposit(
            bob.peer_id, KIND, {"n": index}, to_messaging_pub=stranger.messaging_pub
        )
    sender.deposit(bob.peer_id, KIND, {"readable": True}, to_messaging_pub=bob.messaging_pub)
    assert len(_pending_files(registry)) == MAX_POLL_LIMIT + 1

    receiver = bob.client(max_attempts=3)
    handler = _Recorder()
    receiver.register_handler(KIND, handler)

    delivered_on = None
    for poll in range(1, 5):
        if receiver.poll_once():
            delivered_on = poll
            break
    assert delivered_on is not None, "the readable message never got through"
    assert delivered_on <= 4
    assert [body for _envelope, body in handler.calls] == [{"readable": True}]
    status = receiver.status()
    assert status["handled_total"] == 1
    assert status["dropped_total"] == MAX_POLL_LIMIT
    assert status["undecryptable"] == 3 * MAX_POLL_LIMIT


# ------------------------------------------------------- 4. unknown / expired


def test_an_unknown_kind_is_dropped_and_acked(tmp_path) -> None:
    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)

    alice.client().deposit(
        bob.peer_id, "some.kind.nobody.handles", {"x": 1}, to_messaging_pub=bob.messaging_pub
    )
    receiver = bob.client()
    handler = _Recorder()
    receiver.register_handler(KIND, handler)

    assert receiver.poll_once() == 0
    assert handler.calls == []
    assert receiver.status()["dropped_total"] == 1
    assert receiver.poll_once() == 0
    assert _pending_files(registry) == []


def test_an_expired_envelope_is_never_delivered(tmp_path) -> None:
    """Even a registry that serves stale mail cannot get it past the client."""

    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)
    born = datetime.now(timezone.utc) - timedelta(hours=2)

    signed = seal_mailbox_message(
        kind=KIND,
        body={"invite_id": "abc"},
        from_private_key_bytes=alice.store.private_key_bytes,
        to_peer_id=bob.peer_id,
        to_messaging_pub=bob.messaging_pub,
        ttl_s=60,
        now=_at(born),
    )
    stub = _StubRegistry([signed])
    bob.store.registry = stub
    receiver = bob.client()
    handler = _Recorder()
    receiver.register_handler(KIND, handler)

    assert receiver.poll_once() == 0
    assert handler.calls == []
    assert receiver.status()["dropped_total"] == 1
    receiver.poll_once()
    assert stub.acked == [signed.payload["message_id"]]

    # The real registry refuses to hand it over at all.
    with pytest.raises(MailboxError, match="expired"):
        registry.deposit_mailbox(signed)


# ---------------------------------------------------------------- 5. isolation


def test_mail_for_another_peer_never_reaches_this_node(tmp_path) -> None:
    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)
    carol = _Node(tmp_path, "carol", registry)

    alice.client().deposit(
        carol.peer_id, KIND, {"invite_id": "for-carol"}, to_messaging_pub=carol.messaging_pub
    )
    receiver = bob.client()
    handler = _Recorder()
    receiver.register_handler(KIND, handler)

    assert receiver.poll_once() == 0
    assert handler.calls == []
    assert receiver.status()["pending_last"] == 0
    assert len(_pending_files(registry)) == 1, "Carol's mail is untouched"

    carol_client = carol.client()
    carol_handler = _Recorder()
    carol_client.register_handler(KIND, carol_handler)
    assert carol_client.poll_once() == 1
    assert carol_handler.calls[0][1] == {"invite_id": "for-carol"}


def test_a_mislabelled_envelope_from_the_registry_is_rejected(tmp_path) -> None:
    """A registry that pushes someone else's envelope at us gets a drop."""

    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)
    carol = _Node(tmp_path, "carol", registry)

    signed = seal_mailbox_message(
        kind=KIND,
        body={"invite_id": "for-carol"},
        from_private_key_bytes=alice.store.private_key_bytes,
        to_peer_id=carol.peer_id,
        to_messaging_pub=carol.messaging_pub,
    )
    bob.store.registry = _StubRegistry([signed])
    receiver = bob.client()
    handler = _Recorder()
    receiver.register_handler(KIND, handler)

    assert receiver.poll_once() == 0
    assert handler.calls == []
    assert receiver.status()["dropped_total"] == 1


def test_junk_from_the_registry_cannot_wedge_the_box(tmp_path) -> None:
    """One unopenable item must not stop the rest of the batch."""

    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)

    good = seal_mailbox_message(
        kind=KIND,
        body={"invite_id": "good"},
        from_private_key_bytes=alice.store.private_key_bytes,
        to_peer_id=bob.peer_id,
        to_messaging_pub=bob.messaging_pub,
    )
    stub = _StubRegistry([])
    stub.messages = ["not an envelope at all", good]
    bob.store.registry = stub
    receiver = bob.client()
    handler = _Recorder()
    receiver.register_handler(KIND, handler)

    assert receiver.poll_once() == 1
    assert handler.calls[0][1] == {"invite_id": "good"}
    assert receiver.status()["dropped_total"] == 1


# ------------------------------------------------------------------- 6. status


def test_status_shape_and_error_redaction(tmp_path) -> None:
    registry = FilePeerRegistry(tmp_path / "registry")
    bob = _Node(tmp_path, "bob", registry)
    client = bob.client()

    assert client.status() == {
        "handled_total": 0,
        "dropped_total": 0,
        "undecryptable": 0,
        "pending_last": 0,
        "last_poll_at": "",
        "last_error": "",
        "handlers": [],
    }

    class _Broken:
        def poll_mailbox(self, signed_poll: SignedPayload) -> list[SignedPayload]:
            raise ConnectionResetError("SECRET_REGISTRY_URL_MARKER")

    bob.store.registry = _Broken()
    with pytest.raises(ConnectionResetError):
        client.poll_once()
    status = client.status()
    assert status["last_error"] == "ConnectionResetError"
    assert "SECRET_REGISTRY_URL_MARKER" not in json.dumps(status)

    bob.store.registry = None
    assert client.poll_once() == 0
    assert client.status()["last_error"] == "no_registry"
    with pytest.raises(MailboxError, match="no_registry"):
        client.deposit("x", KIND, {}, to_messaging_pub="")


def test_pending_acks_are_capped_per_poll(tmp_path) -> None:
    registry = FilePeerRegistry(tmp_path / "registry")
    bob = _Node(tmp_path, "bob", registry)
    client = bob.client()

    ids = [f"{index:032x}" for index in range(MAX_ACK_IDS + 10)]
    for message_id in ids:
        client._ack(message_id)
    client._ack(ids[0])  # deduped, not queued twice

    stub = _StubRegistry([])
    bob.store.registry = stub
    assert client.poll_once() == 0
    assert len(stub.acked) == MAX_ACK_IDS
    assert client.poll_once() == 0
    assert sorted(stub.acked) == sorted(ids)


# ------------------------------------------------------------------- 7. wiring


def _worker_registry():
    from rynmesh.background_workers import BackgroundWorkerRegistry

    return BackgroundWorkerRegistry()


def test_install_mailbox_registers_the_supervised_poll_worker(tmp_path) -> None:
    fastapi = pytest.importorskip("fastapi")

    from rynmesh.mailbox_routes import install_mailbox

    registry = FilePeerRegistry(tmp_path / "registry")
    bob = _Node(tmp_path, "bob", registry)
    workers = _worker_registry()
    app = fastapi.FastAPI()

    client = install_mailbox(
        app,
        store=bob.store,
        messaging_key=bob.messaging_key,
        home=bob.home,
        resolve_pubkey=lambda peer_id: bob.messaging_pub,
        workers=workers,
    )
    assert app.state.mailbox is client
    [spec] = [item for item in workers.specs() if item.name == "mailbox.poll"]
    assert spec.initial_delay_s == 3.0
    assert spec.policy.busy_delay_s == 2.0
    assert spec.policy.idle_initial_s == 5.0
    assert spec.policy.idle_multiplier == 1.5
    assert spec.policy.idle_max_s == 60.0
    assert spec.policy.error_multiplier == 2.0
    assert spec.policy.error_max_s == 120.0

    # The supervisor reads busy/idle from a bool or a WorkerRunResult, so the
    # counts have to arrive as one.
    alice = _Node(tmp_path, "alice", registry)
    client.register_handler(KIND, _Recorder())
    alice.client().deposit(
        bob.peer_id, KIND, {"invite_id": "abc"}, to_messaging_pub=bob.messaging_pub
    )
    assert spec.run_once().activity is True
    assert spec.run_once().activity is False


def test_a_batch_of_only_drops_still_counts_as_activity(tmp_path) -> None:
    """A box full of unhandled kinds must drain at the busy delay, not idle."""

    fastapi = pytest.importorskip("fastapi")

    from rynmesh.mailbox_routes import install_mailbox

    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)
    workers = _worker_registry()

    client = install_mailbox(
        fastapi.FastAPI(),
        store=bob.store,
        messaging_key=bob.messaging_key,
        home=bob.home,
        resolve_pubkey=lambda peer_id: bob.messaging_pub,
        workers=workers,
    )
    [spec] = workers.specs()
    alice.client().deposit(
        bob.peer_id, "nobody.handles.this", {"x": 1}, to_messaging_pub=bob.messaging_pub
    )

    result = spec.run_once()
    assert result.activity is True, "a dropped message is still work"
    assert client.status()["handled_total"] == 0
    assert client.status()["dropped_total"] == 1
    assert client.last_poll_dropped == 1
    assert spec.run_once().activity is False
    assert client.last_poll_dropped == 0


def test_install_mailbox_skips_the_worker_without_a_registry(tmp_path) -> None:
    fastapi = pytest.importorskip("fastapi")

    from rynmesh.mailbox_routes import install_mailbox

    bob = _Node(tmp_path, "bob", FilePeerRegistry(tmp_path / "registry"))
    bob.store.registry = None
    workers = _worker_registry()
    app = fastapi.FastAPI()

    install_mailbox(
        app,
        store=bob.store,
        messaging_key=bob.messaging_key,
        home=bob.home,
        resolve_pubkey=lambda peer_id: "",
        workers=workers,
    )
    assert [spec.name for spec in workers.specs()] == []
    assert app.state.mailbox.poll_once() == 0


def test_install_mailbox_is_idempotent(tmp_path) -> None:
    fastapi = pytest.importorskip("fastapi")

    from rynmesh.mailbox_routes import install_mailbox

    bob = _Node(tmp_path, "bob", FilePeerRegistry(tmp_path / "registry"))
    workers = _worker_registry()
    app = fastapi.FastAPI()
    options = dict(
        store=bob.store,
        messaging_key=bob.messaging_key,
        home=bob.home,
        resolve_pubkey=lambda peer_id: "",
        workers=workers,
    )
    install_mailbox(app, **options)
    install_mailbox(app, **options)
    assert [spec.name for spec in workers.specs()] == ["mailbox.poll"]


def test_local_mailbox_status_route(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("RYNMESH_LOCAL_TOKEN", raising=False)
    monkeypatch.delenv("RYNMESH_ALLOW_REMOTE_CONTROL", raising=False)
    store = RynmeshStore(home=tmp_path / "home", network_dir=tmp_path / "network")
    app = create_app(store)
    app.state.mailbox.register_handler(KIND, _Recorder())

    with TestClient(app, base_url="https://testserver") as client:
        body = client.get("/api/local/mailbox/status").json()
        assert set(body) == {
            "handled_total",
            "dropped_total",
            "undecryptable",
            "pending_last",
            "last_poll_at",
            "last_error",
            "handlers",
            "worker",
        }
        # `create_app` also registers the peer-message relay handler.
        from rynmesh.friends.mailbox import OPERATION_KIND, RECEIPT_KIND
        assert body["handlers"] == sorted([KIND, "peer.message.v1", OPERATION_KIND, RECEIPT_KIND])
        assert body["worker"]["name"] == "mailbox.poll"
        assert body["worker"]["running"] is True

        tunnel = {"cf-connecting-ip": "203.0.113.9", "x-forwarded-for": "203.0.113.9"}
        assert client.get("/api/local/mailbox/status", headers=tunnel).status_code == 401


def test_the_status_route_surfaces_the_registrys_own_drop_count(tmp_path) -> None:
    """Mail the node's registry client refused is worth seeing next to its own."""

    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rynmesh.mailbox_routes import install_mailbox, registry_dropped_messages
    from rynmesh.registry import HttpPeerRegistry
    from rynmesh.registry_resilience import FallbackRegistryChain

    file_registry = FilePeerRegistry(tmp_path / "registry")
    bob = _Node(tmp_path, "bob", file_registry)
    # A file-backed registry counts nothing, so the key stays off the response.
    assert registry_dropped_messages(bob.store) is None

    http = HttpPeerRegistry("http://127.0.0.1:9")
    http.dropped_mailbox_messages = 4
    bob.store.registry = http

    app = fastapi.FastAPI()
    install_mailbox(
        app,
        store=bob.store,
        messaging_key=bob.messaging_key,
        home=bob.home,
        resolve_pubkey=lambda peer_id: bob.messaging_pub,
        workers=_worker_registry(),
    )
    with TestClient(app) as client:
        assert client.get("/api/local/mailbox/status").json()["registry_dropped"] == 4

    # A fallback chain has no counter of its own, so the mirrors' are summed.
    mirror = HttpPeerRegistry("http://127.0.0.1:10")
    mirror.dropped_mailbox_messages = 3
    bob.store.registry = FallbackRegistryChain([http, mirror])
    assert registry_dropped_messages(bob.store) == 7


def test_the_store_home_owns_the_messaging_key(tmp_path, monkeypatch) -> None:
    """One key, whatever $RYNMESH_HOME says.

    `create_app` is routinely handed a store whose home is not $RYNMESH_HOME.
    If the advertised key and the decrypting key came from different files,
    every message a peer sealed would be dropped as `open_failed`.
    """

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    store_home = tmp_path / "store-home"
    env_home = tmp_path / "env-home"
    env_home.mkdir()
    monkeypatch.setenv("RYNMESH_HOME", str(env_home))
    monkeypatch.setenv("RYNMESH_NETWORK_ID", "splitnet")
    monkeypatch.delenv("RYNMESH_LOCAL_TOKEN", raising=False)
    monkeypatch.delenv("RYNMESH_PEER_PORT", raising=False)
    monkeypatch.delenv("RYNMESH_PEER_ENDPOINT", raising=False)

    registry = FilePeerRegistry(tmp_path / "registry")
    store = RynmeshStore(home=store_home, network_dir=tmp_path / "network")
    store.registry = registry
    app = create_app(store)

    with TestClient(app) as client:
        served = client.get("/api/peer/pubkey").json()["x25519_pub"]
    assert served == store.messaging_public_key()
    assert not (env_home / "messaging.x25519").exists()

    store.register_node(network_id="splitnet")
    [record] = [
        item
        for item in store.discover_peers(network_id="splitnet", include_self=True)["peers"]
        if item["peer_id"] == store.peer_id
    ]
    advertised = record["metadata"]["messaging_pub"]
    assert advertised == served

    # A peer that only ever saw the advertised key can reach this node.
    alice = _Node(tmp_path, "alice", registry)
    handler = _Recorder()
    app.state.mailbox.register_handler(KIND, handler)
    alice.client().deposit(
        store.peer_id, KIND, {"invite_id": "split-home"}, to_messaging_pub=advertised
    )
    assert app.state.mailbox.poll_once() == 1
    assert handler.calls[0][1] == {"invite_id": "split-home"}
    # The seen cache followed the identity, not the environment.
    assert (store_home / "mailbox" / "seen.json").is_file()
    assert not (env_home / "mailbox").exists()


def test_install_mailbox_warns_when_the_homes_disagree(tmp_path, caplog) -> None:
    import logging

    fastapi = pytest.importorskip("fastapi")

    from rynmesh.mailbox_routes import install_mailbox

    bob = _Node(tmp_path, "bob", FilePeerRegistry(tmp_path / "registry"))
    with caplog.at_level(logging.DEBUG, logger="rynmesh.mailbox_routes"):
        install_mailbox(
            fastapi.FastAPI(),
            store=bob.store,
            messaging_key=bob.messaging_key,
            home=tmp_path / "somewhere-else",
            resolve_pubkey=lambda peer_id: "",
            workers=_worker_registry(),
        )
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "RYNMESH_HOME differs from the store home" in text
    assert str(tmp_path) not in text, "the warning must not name either path"
    assert not (tmp_path / "somewhere-else").exists(), "the store home is what is used"


# ---------------------------------------------------- 9. messaging-key lookup


def test_registration_advertises_the_messaging_key(tmp_path, monkeypatch) -> None:
    from rynmesh.mailbox_routes import registry_messaging_pub, with_registry_fallback

    monkeypatch.delenv("RYNMESH_PEER_PORT", raising=False)
    monkeypatch.delenv("RYNMESH_PEER_ENDPOINT", raising=False)
    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)

    alice.store.register_node(network_id="testnet")
    peers = bob.store.discover_peers(network_id="testnet", include_self=False)["peers"]
    [record] = [item for item in peers if item["peer_id"] == alice.peer_id]
    assert record["metadata"]["messaging_pub"] == alice.messaging_pub
    assert registry_messaging_pub(
        bob.store, alice.peer_id, network_id="testnet"
    ) == alice.messaging_pub
    assert registry_messaging_pub(bob.store, bob.peer_id, network_id="testnet") == ""

    cache: dict[str, str] = {}

    def _no_endpoint(peer_id: str) -> str:
        raise RuntimeError(f"no endpoint for peer {peer_id}")

    resolve = with_registry_fallback(
        _no_endpoint, store=bob.store, cache=cache, network_id=lambda: "testnet"
    )
    assert resolve(alice.peer_id) == alice.messaging_pub
    assert cache[alice.peer_id] == alice.messaging_pub  # TOFU-cached like the direct path
    # A peer with neither an endpoint nor a record raises the original error.
    with pytest.raises(RuntimeError, match="no endpoint"):
        resolve(_Node(tmp_path, "dave", registry).peer_id)


def test_registry_fallback_prefers_the_direct_lookup(tmp_path) -> None:
    from rynmesh.mailbox_routes import with_registry_fallback

    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)
    alice.store.register_node(network_id="testnet")

    cache: dict[str, str] = {"cached": "CACHED_PUB"}
    calls: list[str] = []

    def _direct(peer_id: str) -> str:
        calls.append(peer_id)
        return "DIRECT_PUB"

    resolve = with_registry_fallback(
        _direct, store=bob.store, cache=cache, network_id=lambda: "testnet"
    )
    assert resolve(alice.peer_id) == "DIRECT_PUB"
    assert resolve("cached") == "CACHED_PUB"
    assert calls == [alice.peer_id]


def test_deposit_resolves_the_messaging_key_when_not_supplied(tmp_path) -> None:
    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)

    sender = alice.client(resolve_messaging_pub=lambda peer_id: bob.messaging_pub)
    sender.deposit(bob.peer_id, KIND, {"invite_id": "resolved"})

    receiver = bob.client()
    handler = _Recorder()
    receiver.register_handler(KIND, handler)
    assert receiver.poll_once() == 1
    assert handler.calls[0][1] == {"invite_id": "resolved"}


def test_nothing_sensitive_reaches_the_logs(tmp_path, caplog) -> None:
    """Log records carry kind, message id and an exception class — nothing else."""

    import logging

    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)

    alice.client().deposit(
        bob.peer_id, KIND, {"secret": "SECRET_BODY_MARKER"}, to_messaging_pub=bob.messaging_pub
    )
    alice.client().deposit(
        bob.peer_id, "unhandled.kind", {"secret": "SECRET_BODY_MARKER"},
        to_messaging_pub=bob.messaging_pub,
    )
    receiver = bob.client(max_attempts=1)
    receiver.register_handler(KIND, _Recorder(failures=99))

    with caplog.at_level(logging.DEBUG, logger="rynmesh.mailbox_client"):
        receiver.poll_once()
        receiver.poll_once()
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert text, "the drop paths must be observable"
    assert "SECRET_BODY_MARKER" not in text
    assert "SECRET_HANDLER_FAILURE_MARKER" not in text
    assert "RuntimeError" in text
    assert KIND in text


def test_a_hostile_message_id_is_not_written_to_the_log(tmp_path, caplog) -> None:
    import logging

    from rynmesh.crypto import sign_payload

    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)

    signed = seal_mailbox_message(
        kind=KIND,
        body={"x": 1},
        from_private_key_bytes=alice.store.private_key_bytes,
        to_peer_id=bob.peer_id,
        to_messaging_pub=bob.messaging_pub,
    )
    forged = sign_payload(
        {**signed.payload, "message_id": "LOG_INJECTION\nmailbox handled id=0"},
        private_key_bytes=alice.store.private_key_bytes,
    )
    bob.store.registry = _StubRegistry([forged])
    receiver = bob.client()
    receiver.register_handler(KIND, _Recorder())

    with caplog.at_level(logging.DEBUG, logger="rynmesh.mailbox_client"):
        assert receiver.poll_once() == 0
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "LOG_INJECTION" not in text
    assert "id=invalid" in text
    assert receiver.status()["dropped_total"] == 1


def test_a_hostile_kind_is_not_written_to_the_log(tmp_path, caplog) -> None:
    """A `kind` carrying an escape sequence never reaches the node's log.

    It cannot: the charset check runs inside `verify_mailbox_envelope`, so the
    message is rejected as a bad envelope — the one drop path that logs a
    class name and a validated id and nothing the sender chose.
    """

    import logging

    from rynmesh.crypto import sign_payload

    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)

    signed = seal_mailbox_message(
        kind=KIND,
        body={"x": 1},
        from_private_key_bytes=alice.store.private_key_bytes,
        to_peer_id=bob.peer_id,
        to_messaging_pub=bob.messaging_pub,
    )
    forged = sign_payload(
        {**signed.payload, "kind": "LOG_INJECTION\x1b[2J\nmailbox handled kind=ok"},
        private_key_bytes=alice.store.private_key_bytes,
    )
    bob.store.registry = _StubRegistry([forged])
    receiver = bob.client()
    receiver.register_handler(KIND, _Recorder())

    with caplog.at_level(logging.DEBUG, logger="rynmesh.mailbox_client"):
        assert receiver.poll_once() == 0
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert text, "the drop path must be observable"
    assert "LOG_INJECTION" not in text
    assert "\x1b" not in text
    assert "MailboxError" in text
    assert receiver.status()["dropped_total"] == 1


def test_ciphertext_and_bodies_never_reach_the_seen_cache(tmp_path) -> None:
    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)

    alice.client().deposit(
        bob.peer_id, KIND, {"secret": "SECRET_BODY_MARKER"}, to_messaging_pub=bob.messaging_pub
    )
    receiver = bob.client()
    receiver.register_handler(KIND, _Recorder())
    assert receiver.poll_once() == 1

    stored = (bob.home / "mailbox" / "seen.json").read_text(encoding="utf-8")
    assert "SECRET_BODY_MARKER" not in stored
    payload = json.loads(stored)
    assert set(payload) == {"version", "entries", "attempts"}
    assert all(isinstance(value, float) for value in payload["entries"].values())
    assert payload["attempts"] == {}


def test_a_poll_request_carries_only_signed_metadata(tmp_path) -> None:
    """Sanity check on what the client sends: no body, no key, just an ack list."""

    registry = FilePeerRegistry(tmp_path / "registry")
    bob = _Node(tmp_path, "bob", registry)
    signed = build_poll_request(private_key_bytes=bob.store.private_key_bytes, ack=["a" * 32])
    assert set(signed.payload) == {"kind", "peer_id", "issued_at", "nonce", "ack", "limit"}
    assert signed.payload["peer_id"] == bob.peer_id


# --------------------------------------------- 10. peer-message store-and-forward


class _FakeMessenger:
    """Records the headers it was asked to receive; returns a history record.

    Mirrors the real `PeerMessenger.receive` contract on the point that matters
    here: a message id it has already stored comes back marked `duplicate`.
    """

    def __init__(self) -> None:
        self.headers: list[dict] = []
        self.stored: dict[str, dict] = {}

    def receive(self, header: dict) -> dict:
        self.headers.append(header)
        msg_id = str(header.get("msg_id", "m1"))
        if msg_id in self.stored:
            return {**self.stored[msg_id], "duplicate": True}
        record = {"msg_id": msg_id, "dir": "in", "from": header["from"], "text": "hi"}
        self.stored[msg_id] = record
        return record


def _relay_header(sender_peer_id: str, recipient_peer_id: str) -> dict:
    return {"v": 1, "from": sender_peer_id, "to": recipient_peer_id,
            "nonce": "n", "ciphertext": "c", "from_pub": "SENDER_PUB"}


def test_relayed_peer_messages_reach_receive_and_the_sse_stream(tmp_path) -> None:
    from rynmesh.mailbox_routes import PEER_MESSAGE_KIND, install_peer_message_relay

    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)

    messenger = _FakeMessenger()
    published: list[dict] = []
    cache: dict[str, str] = {}
    receiver = bob.client()
    install_peer_message_relay(receiver, messenger, published.append, pubkey_cache=cache)

    header = _relay_header(alice.peer_id, bob.peer_id)
    alice.client().deposit(bob.peer_id, PEER_MESSAGE_KIND, header,
                           to_messaging_pub=bob.messaging_pub)

    assert receiver.poll_once() == 1
    assert messenger.headers == [header]           # the sealed header, verbatim
    # `receive`'s record — not the envelope — is what reaches the SSE stream.
    assert published == [{"msg_id": "m1", "dir": "in", "from": alice.peer_id, "text": "hi"}]
    assert cache[alice.peer_id] == "SENDER_PUB"    # TOFU, as /api/peer/msg does


def test_a_message_already_received_directly_is_not_published_again(tmp_path) -> None:
    """The mailbox retry of a message the direct route already delivered."""

    from rynmesh.mailbox_routes import PEER_MESSAGE_KIND, install_peer_message_relay

    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)

    messenger = _FakeMessenger()
    published: list[dict] = []
    receiver = bob.client()
    install_peer_message_relay(receiver, messenger, published.append)

    header = _relay_header(alice.peer_id, bob.peer_id)
    messenger.receive(header)  # the direct POST landed first
    alice.client().deposit(bob.peer_id, PEER_MESSAGE_KIND, header,
                           to_messaging_pub=bob.messaging_pub)

    # Handled, not dropped — the message really was delivered, just not twice.
    assert receiver.poll_once() == 1
    assert receiver.status()["dropped_total"] == 0
    assert published == []
    assert len(messenger.stored) == 1


def test_a_relayed_header_claiming_another_sender_is_refused(tmp_path) -> None:
    from rynmesh.mailbox_routes import PEER_MESSAGE_KIND, install_peer_message_relay

    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)
    carol = _Node(tmp_path, "carol", registry)

    messenger = _FakeMessenger()
    published: list[dict] = []
    cache: dict[str, str] = {}
    receiver = bob.client()
    install_peer_message_relay(receiver, messenger, published.append, pubkey_cache=cache)

    # Carol deposits under her own signature a header that names Alice.
    carol.client().deposit(bob.peer_id, PEER_MESSAGE_KIND,
                           _relay_header(alice.peer_id, bob.peer_id),
                           to_messaging_pub=bob.messaging_pub)

    assert receiver.poll_once() == 0
    assert messenger.headers == [] and published == [] and cache == {}
    assert receiver.status()["dropped_total"] == 0  # still retrying, not yet dropped


def test_a_header_at_the_messenger_gate_still_fits_an_envelope(tmp_path) -> None:
    """The gate must sit *below* the real limit, not above it.

    A gate that admits headers the sealer then rejects would turn every large
    message into a wasted rate-limit token and a raised exception.
    """

    from rynmesh.mailbox_routes import PEER_MESSAGE_KIND
    from rynmesh.services.peer_messenger import MAX_MAILBOX_HEADER_BYTES

    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)

    header = _relay_header(alice.peer_id, bob.peer_id)
    pad = MAX_MAILBOX_HEADER_BYTES - len(json.dumps(header))
    header["ciphertext"] = "A" * (len(header["ciphertext"]) + pad)
    assert len(json.dumps(header)) == MAX_MAILBOX_HEADER_BYTES

    # Would raise `envelope_too_large` if the gate were set above the real cap.
    receipt = alice.client().deposit(bob.peer_id, PEER_MESSAGE_KIND, header,
                                     to_messaging_pub=bob.messaging_pub)
    assert receipt["message_id"]


def test_the_fallback_is_absent_without_a_registry(tmp_path) -> None:
    from rynmesh.mailbox_routes import peer_message_fallback

    class _App:
        state = type("S", (), {})()

    alice = _Node(tmp_path, "alice", None)
    assert peer_message_fallback(_App(), store=alice.store) is None


def test_the_fallback_deposits_the_header_into_the_recipients_box(tmp_path) -> None:
    from rynmesh.mailbox_routes import PEER_MESSAGE_KIND, peer_message_fallback

    registry = FilePeerRegistry(tmp_path / "registry")
    alice = _Node(tmp_path, "alice", registry)
    bob = _Node(tmp_path, "bob", registry)

    class _App:
        state = type("S", (), {})()

    app = _App()
    fallback = peer_message_fallback(app, store=alice.store)
    assert fallback is not None
    # Built before the client exists, exactly as `create_app` does it.
    assert fallback(bob.peer_id, _relay_header(alice.peer_id, bob.peer_id)) is False

    app.state.mailbox = alice.client(
        resolve_messaging_pub=lambda peer_id: bob.messaging_pub
    )
    assert fallback(bob.peer_id, _relay_header(alice.peer_id, bob.peer_id)) is True

    stored = _pending_files(registry)
    assert len(stored) == 1
    # Only ciphertext reaches the registry: the relayed header is inside the seal.
    assert "SENDER_PUB" not in stored[0].read_text(encoding="utf-8")

    receiver = bob.client()
    messenger = _FakeMessenger()
    from rynmesh.mailbox_routes import install_peer_message_relay

    install_peer_message_relay(receiver, messenger, lambda record: None)
    assert receiver.poll_once() == 1
    assert receiver.status()["handlers"] == [PEER_MESSAGE_KIND]
