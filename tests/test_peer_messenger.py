from __future__ import annotations

import json
import time

import pytest

from rynmesh.services import peer_box
from rynmesh.services.messaging_store import MessagingStore
from rynmesh.services.peer_messenger import (
    MAX_INLINE_BYTES,
    MAX_MAILBOX_HEADER_BYTES,
    MessengerError,
    PeerMessenger,
)


def _messenger(tmp_path, name, peers):
    """peers: dict name->(peer_id, x25519_pub_b64, priv). Wires a loopback transport."""
    me = peers[name]
    inbox = {}  # peer_id -> list of headers delivered
    def transport(peer_id, header):       # "POST" to a peer
        inbox.setdefault(peer_id, []).append(header); return 200
    def resolve_pubkey(peer_id):
        return next(p[1] for p in peers.values() if p[0] == peer_id)
    m = PeerMessenger(
        my_peer_id=me[0], my_priv=me[2],
        store=MessagingStore(tmp_path / name),
        resolve_pubkey=resolve_pubkey, transport=transport,
        now=lambda: "2026-06-06T00:00:00+00:00",
        new_id=lambda: "fixed-id",
    )
    return m, inbox

def _peers(tmp_path):
    a = peer_box.load_or_create_messaging_key(tmp_path / "ka")
    b = peer_box.load_or_create_messaging_key(tmp_path / "kb")
    return {"A": ("peerA", peer_box.public_key_b64(a), a),
            "B": ("peerB", peer_box.public_key_b64(b), b)}

def test_send_then_receive_text(tmp_path):
    peers = _peers(tmp_path)
    a, inbox = _messenger(tmp_path, "A", peers)
    b, _ = _messenger(tmp_path, "B", peers)
    rec = a.send("peerB", text="hello")
    assert rec["dir"] == "out" and rec["text"] == "hello" and rec["delivered"] is True
    header = inbox["peerB"][0]
    got = b.receive(header)
    assert got["dir"] == "in" and got["text"] == "hello" and got["from"] == "peerA"
    assert b.history("peerA")[0]["text"] == "hello"
    assert a.history("peerB")[0]["text"] == "hello"

def test_send_receive_attachment(tmp_path):
    peers = _peers(tmp_path)
    a, inbox = _messenger(tmp_path, "A", peers)
    b, _ = _messenger(tmp_path, "B", peers)
    a.send("peerB", text="pic", attachment={"filename": "x.png", "mime": "image/png", "bytes": b"\x89PNG"})
    got = b.receive(inbox["peerB"][0])
    assert got["kind"] == "image" and got["attachment"]["filename"] == "x.png"
    assert b._store.load_attachment(got["msg_id"]) == b"\x89PNG"  # bytes stored locally, not in history line

def test_oversize_attachment_rejected(tmp_path):
    peers = _peers(tmp_path)
    a, _ = _messenger(tmp_path, "A", peers)
    with pytest.raises(MessengerError):
        a.send("peerB", attachment={"filename": "big", "mime": "application/octet-stream",
                                     "bytes": b"x" * (MAX_INLINE_BYTES + 1)})


# ---- store-and-forward fallback -------------------------------------------


def _sender(tmp_path, peers, *, transport, fallback=None):
    """Messenger A with an injectable transport and store-and-forward hook."""

    me = peers["A"]

    def resolve_pubkey(peer_id):
        return next(p[1] for p in peers.values() if p[0] == peer_id)

    return PeerMessenger(
        my_peer_id=me[0], my_priv=me[2],
        store=MessagingStore(tmp_path / "A-fallback"),
        resolve_pubkey=resolve_pubkey, transport=transport,
        now=lambda: "2026-06-06T00:00:00+00:00",
        new_id=lambda: "fixed-id",
        fallback=fallback,
    )


def _recording_fallback(result):
    """A fallback that records its calls and returns (or raises) `result`."""

    calls = []

    def fallback(peer_id, header):
        calls.append((peer_id, header))
        if isinstance(result, Exception):
            raise result
        return result

    return fallback, calls


def test_a_failed_direct_send_is_queued_in_the_mailbox(tmp_path):
    peers = _peers(tmp_path)
    fallback, calls = _recording_fallback(True)

    def transport(peer_id, header):
        return 0  # no endpoint — exactly what the node returns when unreachable

    a = _sender(tmp_path, peers, transport=transport, fallback=fallback)
    rec = a.send("peerB", text="hello")

    assert rec["delivered"] is False
    assert rec["via"] == "mailbox"
    assert [peer for peer, _ in calls] == ["peerB"]
    # The fallback is handed the sealed header verbatim, plaintext-free.
    queued = calls[0][1]
    assert queued["from"] == "peerA" and queued["to"] == "peerB"
    assert "hello" not in json.dumps(queued)
    # The history line records the queue decision, and nothing more.
    assert a.history("peerB")[0]["via"] == "mailbox"


def test_a_transport_exception_also_reaches_the_mailbox(tmp_path):
    peers = _peers(tmp_path)
    fallback, calls = _recording_fallback(True)

    def transport(peer_id, header):
        raise ConnectionRefusedError("peer is down")

    a = _sender(tmp_path, peers, transport=transport, fallback=fallback)
    assert a.send("peerB", text="hi")["via"] == "mailbox"
    assert len(calls) == 1


def test_a_refused_mailbox_leaves_the_record_as_it_was(tmp_path):
    peers = _peers(tmp_path)
    fallback, calls = _recording_fallback(False)
    a = _sender(tmp_path, peers, transport=lambda p, h: 500, fallback=fallback)

    rec = a.send("peerB", text="hi")

    assert rec["delivered"] is False
    assert "via" not in rec
    assert len(calls) == 1


def test_a_raising_mailbox_leaves_the_record_as_it_was(tmp_path):
    peers = _peers(tmp_path)
    fallback, calls = _recording_fallback(RuntimeError("recipient_full"))
    a = _sender(tmp_path, peers, transport=lambda p, h: 500, fallback=fallback)

    rec = a.send("peerB", text="hi")

    assert rec["delivered"] is False and "via" not in rec
    assert len(calls) == 1


def test_an_oversized_header_is_never_offered_to_the_mailbox(tmp_path):
    peers = _peers(tmp_path)
    fallback, calls = _recording_fallback(True)
    a = _sender(tmp_path, peers, transport=lambda p, h: 0, fallback=fallback)

    # Base64 of this attachment alone exceeds the envelope budget, so the
    # registry could only ever reject it — it must not burn a rate-limit token.
    rec = a.send("peerB", text="big", attachment={
        "filename": "big.bin", "mime": "application/octet-stream",
        "bytes": b"x" * MAX_MAILBOX_HEADER_BYTES,
    })

    assert calls == []
    assert rec["delivered"] is False and "via" not in rec


def test_a_direct_success_never_touches_the_mailbox(tmp_path):
    peers = _peers(tmp_path)
    fallback, calls = _recording_fallback(True)
    a = _sender(tmp_path, peers, transport=lambda p, h: 200, fallback=fallback)

    rec = a.send("peerB", text="hi")

    assert rec["delivered"] is True and rec["via"] == "direct"
    assert calls == []


def test_without_a_fallback_a_failed_send_is_unchanged(tmp_path):
    peers = _peers(tmp_path)
    a = _sender(tmp_path, peers, transport=lambda p, h: 0)

    rec = a.send("peerB", text="hi")

    assert rec["delivered"] is False and "via" not in rec


# ---- at-least-once delivery: receive is idempotent -------------------------


def test_the_same_header_received_twice_makes_one_history_line(tmp_path):
    """The direct POST's response was lost; the sender retried the same message."""

    peers = _peers(tmp_path)
    a, inbox = _messenger(tmp_path, "A", peers)
    b, _ = _messenger(tmp_path, "B", peers)
    a.send("peerB", text="hello")
    header = inbox["peerB"][0]

    first = b.receive(header)
    second = b.receive(header)

    assert "duplicate" not in first
    assert second["duplicate"] is True
    assert {k: v for k, v in second.items() if k != "duplicate"} == first
    assert len(b.history("peerA")) == 1
    # The marker is a report to the caller, not something that lands in history.
    assert "duplicate" not in b.history("peerA")[0]


def test_a_duplicate_never_rewrites_the_stored_attachment(tmp_path):
    peers = _peers(tmp_path)
    a, inbox = _messenger(tmp_path, "A", peers)
    b, _ = _messenger(tmp_path, "B", peers)
    a.send("peerB", text="pic",
           attachment={"filename": "x.png", "mime": "image/png", "bytes": b"\x89PNG"})
    header = inbox["peerB"][0]

    got = b.receive(header)
    # Something else edited the blob between the two deliveries; a duplicate
    # must not silently restore it, because it must not write at all.
    b._store.save_attachment(got["msg_id"], b"EDITED")
    assert b.receive(header)["duplicate"] is True

    assert b._store.load_attachment(got["msg_id"]) == b"EDITED"
    assert len(b.history("peerA")) == 1


def test_two_threads_receiving_one_header_write_one_history_line(tmp_path):
    """The direct route and the mailbox handler run on different threads.

    `/api/peer/msg` is served on the event loop; the mailbox poll worker runs in
    a thread of its own. Both call `receive`, and both can be handed the same
    message. Without a per-conversation lock the two pass the dedupe check
    together and append twice.
    """

    import threading

    peers = _peers(tmp_path)
    a, inbox = _messenger(tmp_path, "A", peers)
    b, _ = _messenger(tmp_path, "B", peers)
    a.send("peerB", text="exactly once")
    header = inbox["peerB"][0]

    # Widen the window the lock has to close: with the check and the append
    # this far apart, an unlocked `receive` loses the race essentially always.
    original_append = b._store.append

    def slow_append(peer_id, record):
        time.sleep(0.05)
        return original_append(peer_id, record)

    b._store.append = slow_append

    results: list[dict] = []
    errors: list[BaseException] = []
    start = threading.Barrier(2)

    def worker() -> None:
        try:
            start.wait(timeout=5)
            results.append(b.receive(header))
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(b.history("peerA")) == 1, "one message, one history line"
    assert [record.get("duplicate") for record in sorted(
        results, key=lambda item: bool(item.get("duplicate"))
    )] == [None, True]


def test_a_conversation_lock_is_never_evicted_out_from_under_a_caller(tmp_path):
    """`lock.locked()` is not the same question as "is anybody using this?".

    A caller handed the lock but not yet inside `with` reads as unlocked, so an
    eviction keyed on `locked()` can drop the entry under it and hand the next
    caller a different lock for the same conversation. The entry is refcounted
    on hand-out instead, and only a zero-count entry may be evicted.
    """

    import threading

    from rynmesh.services.peer_messenger import MAX_CONVERSATION_LOCKS

    peers = _peers(tmp_path)
    b, _ = _messenger(tmp_path, "B", peers)

    handed_out = threading.Barrier(2)
    pressured = threading.Barrier(2)
    seen: list[threading.Lock] = []
    errors: list[BaseException] = []

    def claim() -> None:
        lock = b._claim_conversation("peerA")
        seen.append(lock)
        try:
            # Both threads hold a *slot* but neither has acquired the lock yet:
            # this is exactly the window `locked()` reads as idle. Nobody
            # releases until the second barrier, so this observation is stable.
            handed_out.wait(timeout=5)
            entry = b._conversation_locks["peerA"]
            assert entry[1] == 2, "both users must be counted"
            assert entry[0].locked() is False, "and neither has acquired it yet"
            # Pressure the map hard enough to force eviction while they wait.
            for index in range(MAX_CONVERSATION_LOCKS + 5):
                b._claim_conversation(f"filler-{index}")
                b._release_conversation(f"filler-{index}")
            pressured.wait(timeout=5)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
            pressured.abort()
        finally:
            b._release_conversation("peerA")

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(seen) == 2
    assert seen[0] is seen[1], "the in-use conversation was evicted mid-flight"
    assert b._conversation_locks["peerA"][1] == 0
    # The filler traffic really did press against the bound, and the idle
    # entries it left behind are the ones that got evicted.
    assert len(b._conversation_locks) <= MAX_CONVERSATION_LOCKS + 1


def test_a_sender_cannot_suppress_its_message_by_reusing_our_outbound_id(tmp_path):
    """Only inbound records dedupe; an id we used for an outbound send does not."""

    peers = _peers(tmp_path)
    a, inbox = _messenger(tmp_path, "A", peers)
    b, _ = _messenger(tmp_path, "B", peers)
    # B writes an outbound record to A under the id A's messenger also uses.
    b.send("peerA", text="mine")
    a.send("peerB", text="theirs")

    got = b.receive(inbox["peerB"][0])

    assert "duplicate" not in got
    assert [(item["dir"], item["text"]) for item in b.history("peerA")] == [
        ("out", "mine"), ("in", "theirs"),
    ]
