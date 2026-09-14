"""Wire the peer mailbox into a node app: client, poll worker, status route.

Kept out of ``peer_http`` so the node module does not grow another feature's
plumbing. ``install_mailbox`` is the whole surface; ``with_registry_fallback``
is the second half of messaging-key discovery, used to wrap the node's direct
``/api/peer/pubkey`` lookup so a peer behind a NAT is still reachable.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import Request

from .background_workers import (
    BackgroundWorkerRegistry,
    BackgroundWorkerSpec,
    BackoffPolicy,
    WorkerRunResult,
)
from .mailbox_client import MailboxClient

log = logging.getLogger("rynmesh.mailbox_routes")

MAILBOX_WORKER_NAME = "mailbox.poll"

MAILBOX_POLL_POLICY = BackoffPolicy(
    busy_delay_s=2.0,
    idle_initial_s=5.0,
    idle_multiplier=1.5,
    idle_max_s=60.0,
    error_multiplier=2.0,
    error_max_s=120.0,
)
MAILBOX_POLL_INITIAL_DELAY_S = 3.0

#: Mailbox kind carrying a sealed :class:`PeerMessenger` header verbatim.
PEER_MESSAGE_KIND = "peer.message.v1"


def _poll_worker(client: MailboxClient) -> Callable[[], WorkerRunResult]:
    """One poll, reported to the supervisor as busy or idle."""

    def run_once() -> WorkerRunResult:
        handled = client.poll_once()
        return WorkerRunResult(activity=(handled + client.last_poll_dropped) > 0)

    return run_once


def install_mailbox(
    app: Any,
    *,
    store: Any,
    messaging_key: X25519PrivateKey,
    home: str | Path,
    resolve_pubkey: Callable[[str], str],
    workers: BackgroundWorkerRegistry,
    local_control: Callable[[Request], None] | None = None,
) -> MailboxClient:
    """Create the client, supervise its poll worker, expose its status.

    ``home`` is the configured node home; the *store's* home wins when they
    disagree. The store owns the identity these messages are sealed to, so
    splitting the two would put the seen cache beside a different identity.
    """

    resolved_home = Path(getattr(store, "home", None) or home)
    if Path(home) != resolved_home:
        # Named paths are private; the operator can compare them themselves.
        log.warning("RYNMESH_HOME differs from the store home; using the store home")

    client = MailboxClient(
        store=store,
        messaging_key=messaging_key,
        home=resolved_home,
        resolve_messaging_pub=resolve_pubkey,
    )
    app.state.mailbox = client
    app.state.mailbox_error = ""

    if getattr(store, "registry", None) is not None:
        # A node with no registry has nowhere to poll; registering the worker
        # anyway would just burn a task on a permanent no-op.
        workers.register(
            BackgroundWorkerSpec(
                name=MAILBOX_WORKER_NAME,
                # The supervisor reads activity from a bool/WorkerRunResult, so
                # the counts have to be turned into one for the busy/idle
                # backoff to work at all. Drops count as activity: a batch of
                # nothing but replays or unknown kinds still has to drain, and
                # backing off would leave the box filling faster than it empties.
                run_once=_poll_worker(client),
                initial_delay_s=MAILBOX_POLL_INITIAL_DELAY_S,
                policy=MAILBOX_POLL_POLICY,
                error_sink=lambda value: setattr(app.state, "mailbox_error", value),
            ),
            replace=True,
        )

    @app.get("/api/local/mailbox/status")
    def local_mailbox_status(request: Request) -> dict[str, Any]:
        # /api/local is gated by the node-auth middleware; this mirrors the
        # per-route re-check the routes in `peer_http` do on top of it.
        if local_control is not None:
            local_control(request)
        body: dict[str, Any] = {
            **client.status(),
            "worker": workers.status().get(MAILBOX_WORKER_NAME),
        }
        dropped = registry_dropped_messages(store)
        if dropped is not None:
            # Envelopes the node's own registry client refused on the way in. A
            # non-zero count means a registry is serving mail this node will not
            # accept — worth seeing next to the client's own drop counters.
            body["registry_dropped"] = dropped
        return body

    return client


def registry_dropped_messages(store: Any) -> int | None:
    """Envelopes ``HttpPeerRegistry`` dropped, or ``None`` if nothing counts them.

    A fallback chain has no counter of its own, so the mirrors' counts are
    summed. A file-backed registry has none at all and reports ``None``.
    """

    registry = getattr(store, "registry", None)
    if registry is None:
        return None
    direct = getattr(registry, "dropped_mailbox_messages", None)
    if isinstance(direct, int):
        return direct
    mirrors = getattr(registry, "registries", None)
    if not isinstance(mirrors, (list, tuple)):
        return None
    counts = [
        getattr(item, "dropped_mailbox_messages", None)
        for item in mirrors
        if isinstance(getattr(item, "dropped_mailbox_messages", None), int)
    ]
    return sum(counts) if counts else None


def peer_message_fallback(
    app: Any, *, store: Any
) -> Callable[[str, dict[str, Any]], bool] | None:
    """Build the ``PeerMessenger`` store-and-forward hook, or ``None``.

    Returns ``None`` when the node has no registry: there is no mailbox to
    deposit into, and a fallback that always fails would only add latency to
    every failed send. The client is read from ``app.state`` at call time
    because the messenger is constructed before :func:`install_mailbox` runs.
    """

    if getattr(store, "registry", None) is None:
        return None

    def fallback(peer_id: str, header: dict[str, Any]) -> bool:
        client = getattr(app.state, "mailbox", None)
        if client is None:
            return False
        # `deposit` returns a receipt or raises — a refusal (full box, rate
        # limit, unreachable registry) is the exception path, which the
        # messenger already reads as "not queued". So reaching this return at
        # all means the message is in the box.
        return bool(client.deposit(peer_id, PEER_MESSAGE_KIND, header))

    return fallback


def install_peer_message_relay(
    client: MailboxClient,
    messenger: Any,
    publish: Callable[[dict[str, Any]], None],
    *,
    pubkey_cache: dict[str, str] | None = None,
) -> None:
    """Deliver mailbox-carried peer messages through the normal receive path.

    The envelope body *is* the sealed header ``/api/peer/msg`` would have been
    posted, so this reuses ``messenger.receive`` (same decryption, same history
    write) and publishes to the SSE stream exactly as the direct route does. A
    malformed header raises, which is what the client's attempt/drop logic reads
    as a failed handler.
    """

    def handle(envelope: Any, body: dict[str, Any]) -> None:
        sender = str(body.get("from") or "")
        if sender != envelope.from_peer_id:
            # The envelope signature already proves who deposited this. Refusing a
            # header that claims someone else keeps the TOFU cache below (and the
            # history line) bound to the proven sender — a guarantee the
            # unauthenticated /api/peer/msg route cannot make.
            raise ValueError("sender_mismatch")
        from_pub = body.get("from_pub")
        if pubkey_cache is not None and from_pub:
            pubkey_cache.setdefault(sender, str(from_pub))  # TOFU, as /api/peer/msg does
        record = messenger.receive(body)
        # A direct send that timed out after the recipient processed it arrives
        # here a second time. `receive` refuses to store it twice; publishing it
        # again would still put a duplicate in the SSE stream.
        if not record.get("duplicate"):
            publish(record)

    client.register_handler(PEER_MESSAGE_KIND, handle, replace=True)


def registry_messaging_pub(store: Any, peer_id: str, *, network_id: str) -> str:
    """The peer's advertised X25519 messaging key, from its registry record."""

    try:
        discovered = store.discover_peers(network_id=network_id, include_self=False) or {}
    except Exception:
        # Discovery is a best-effort fallback; its failure must surface as the
        # caller's original "cannot resolve this peer" error, not a new one.
        return ""
    for record in discovered.get("peers", []) or ():
        if record.get("peer_id") != peer_id:
            continue
        metadata = record.get("metadata") or {}
        return str(metadata.get("messaging_pub") or "") if isinstance(metadata, dict) else ""
    return ""


def with_registry_fallback(
    resolve: Callable[[str], str],
    *,
    store: Any,
    cache: dict[str, str],
    network_id: Callable[[], str],
) -> Callable[[str], str]:
    """Fall back to the registry record when a peer has no reachable endpoint.

    Direct ``/api/peer/pubkey`` is the trusted path and stays first. Two nodes
    behind NATs never have an endpoint for each other, and for them the only
    published copy of the messaging key is the signed registry record.
    """

    def resolve_pubkey(peer_id: str) -> str:
        cached = cache.get(peer_id)
        if cached:
            return cached
        try:
            return resolve(peer_id)
        except Exception:
            pub = registry_messaging_pub(store, peer_id, network_id=network_id())
            if not pub:
                raise
            cache[peer_id] = pub  # TOFU, exactly as the direct lookup caches
            return pub

    return resolve_pubkey


__all__ = [
    "MAILBOX_POLL_INITIAL_DELAY_S",
    "MAILBOX_POLL_POLICY",
    "MAILBOX_WORKER_NAME",
    "PEER_MESSAGE_KIND",
    "install_mailbox",
    "install_peer_message_relay",
    "peer_message_fallback",
    "registry_dropped_messages",
    "registry_messaging_pub",
    "with_registry_fallback",
]
