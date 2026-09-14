"""Censorship-resistant registry and peer-discovery strategies.

The registry is Rynmesh's single coordination choke-point: one SNI/IP block
cuts peer discovery. This module adds three defences that stack:

1. **Multi-registry fallback chain** (``FallbackRegistryChain``): try registries
   in order; the first one that responds wins.  Operators set a comma-separated
   list in ``RYNMESH_REGISTRY_URLS`` (e.g. primary hosted + a CDN-fronted mirror
   + a community relay).  A blocked primary does not prevent discovery.

2. **Out-of-band bootstrap** (``bootstrap_peers_from_path`` /
   ``bootstrap_peers_from_url``): load signed peer records from a local file or
   a URL (e.g. a CDN-hosted JSON blob, a shared Pastebin, a QR code).  Records
   are verified with the same Ed25519 gate; no trust is added just because you
   can reach the URL.  Call this when all registries are unreachable.

3. **Peer exchange / gossip** (``PeerExchangeClient``): once you have *any* peer,
   ask it for its own peer list via ``GET /api/v1/peers`` (added to the peer
   server in this PR).  This lets the mesh grow without the central registry: if
   you can reach one peer, you can reach all of them.

Together these mean the mesh can start from:
   - a registry (normal case), OR
   - an out-of-band file/URL (blocked registry), OR
   - any single known peer's IP (full registry blackout).
"""

from __future__ import annotations

import json
import logging
import ssl
from pathlib import Path
from typing import Any

from .crypto import SignedPayload
from .registry import HttpPeerRegistry, PeerRegistry, RegistryError, verify_peer_record

log = logging.getLogger("rynmesh.registry_resilience")

#: HTTP statuses a mailbox route uses to render a verdict about *this message*
#: (see `_MAILBOX_STATUS` in `registry_http`, plus 413 from a proxy in front of
#: it). A chain must not retry these on the next mirror. Every other status —
#: 404 from a mirror without the network key or without the mailbox routes,
#: 401/403, 501, any 5xx — describes the mirror, so the chain moves on.
MAILBOX_VERDICT_STATUSES = frozenset({400, 409, 413, 429})

# ---------------------------------------------------------------------------
# 1. Multi-registry fallback chain
# ---------------------------------------------------------------------------


class FallbackRegistryChain:
    """Try ``registries`` in order; the first to succeed wins.

    Every *non-fatal* ``RegistryError`` advances to the next; if all fail,
    the last exception is re-raised.  The caller can mix
    ``HttpPeerRegistry`` and ``FilePeerRegistry`` instances freely.

    Usage::

        from rynmesh.registry_resilience import make_fallback_chain
        node.registry = make_fallback_chain()   # reads RYNMESH_REGISTRY_URLS
    """

    def __init__(self, registries: list[PeerRegistry]) -> None:
        if not registries:
            raise ValueError("FallbackRegistryChain requires at least one registry")
        self.registries = registries

    # --- PeerRegistry protocol surface (core: publish / list_peers) ---------

    def publish(self, signed_record: SignedPayload) -> dict[str, Any]:
        return self._try_all("publish", signed_record)

    def list_peers(
        self,
        *,
        network_id: str = "rynmesh-main",
        max_age_hours: float | None = None,
    ) -> list[SignedPayload]:
        return self._try_all("list_peers", network_id=network_id, max_age_hours=max_age_hours)

    # --- Extended protocol surface (jobs etc.; delegate to first alive) -------

    def publish_job_capacity(self, signed_record: SignedPayload) -> dict[str, Any]:
        return self._try_all("publish_job_capacity", signed_record)

    def list_job_capacities(self, **kwargs: Any) -> list[SignedPayload]:
        return self._try_all("list_job_capacities", **kwargs)

    def submit_work_order(self, signed_order: SignedPayload) -> dict[str, Any]:
        return self._try_all("submit_work_order", signed_order)

    def list_work_orders(self, **kwargs: Any) -> list[SignedPayload]:
        return self._try_all("list_work_orders", **kwargs)

    def publish_work_result(self, signed_result: SignedPayload) -> dict[str, Any]:
        return self._try_all("publish_work_result", signed_result)

    def list_work_results(self, **kwargs: Any) -> list[SignedPayload]:
        return self._try_all("list_work_results", **kwargs)

    # Mailbox traffic falls through on transport failure like everything else,
    # but *only* on transport failure — see `_try_mailbox`.
    def deposit_mailbox(self, signed: SignedPayload) -> dict[str, Any]:
        return self._try_mailbox("deposit_mailbox", signed)

    def poll_mailbox(self, signed_poll: SignedPayload) -> list[SignedPayload]:
        return self._try_mailbox("poll_mailbox", signed_poll)

    # -------------------------------------------------------------------------

    def _try_all(self, method: str, *args: Any, **kwargs: Any) -> Any:
        last: Exception = RegistryError("no registries configured")
        for reg in self.registries:
            fn = getattr(reg, method, None)
            if fn is None:
                continue
            try:
                return fn(*args, **kwargs)
            except RegistryError as exc:
                log.warning("registry %s.%s failed: %s; trying next", type(reg).__name__, method, type(exc).__name__)
                last = exc
        raise last

    def _try_mailbox(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Fall through on a dead registry, never on a verdict about the message.

        A local backend raises `MailboxError` ("duplicate", "rate_limited",
        "poll_skew", ...) which is not a `RegistryError` and so was never
        retried. `HttpPeerRegistry` turns the same verdicts into a
        `RegistryError` carrying the HTTP status, and retrying *that* on the
        next mirror would deposit one message into every registry in the chain
        — the exact fan-out the local path is careful to avoid.

        Only the statuses in `MAILBOX_VERDICT_STATUSES` end the attempt. The
        rest of the 4xx range says something about the *mirror*, not about the
        message: 404 is what a registry without the network key (or without the
        mailbox routes at all) returns, and 401/403/501 are the same kind of
        answer. Those are exactly the case the chain exists for, so they fall
        through — as do transport failures (no status) and 5xx.
        """

        last: Exception = RegistryError("no registries configured")
        for reg in self.registries:
            fn = getattr(reg, method, None)
            if fn is None:
                continue
            try:
                return fn(*args, **kwargs)
            except RegistryError as exc:
                if getattr(exc, "status", None) in MAILBOX_VERDICT_STATUSES:
                    raise
                log.warning(
                    "registry %s.%s failed: %s; trying next", type(reg).__name__, method, type(exc).__name__
                )
                last = exc
        raise last


def make_fallback_chain(*, extra: list[PeerRegistry] | None = None) -> FallbackRegistryChain:
    """Build a ``FallbackRegistryChain`` from the environment.

    Reads ``RYNMESH_REGISTRY_URLS`` (comma-separated list of https:// URLs)
    plus any ``extra`` registries supplied directly.  If the env var is empty,
    falls back to ``RYNMESH_REGISTRY_URL`` for a single-entry chain.
    """
    import os

    from .registry import FilePeerRegistry, default_registry_dir

    urls_raw = os.environ.get("RYNMESH_REGISTRY_URLS", "").strip()
    if not urls_raw:
        urls_raw = os.environ.get("RYNMESH_REGISTRY_URL", "").strip()
    url_list = [u.strip() for u in urls_raw.split(",") if u.strip()]

    registries: list[PeerRegistry] = []
    for url in url_list:
        registries.append(HttpPeerRegistry(url))
    if not registries:
        # Last resort: local file registry so the chain is never empty.
        import tempfile

        registries.append(FilePeerRegistry(default_registry_dir(Path(tempfile.mkdtemp()))))
    if extra:
        registries.extend(extra)
    return FallbackRegistryChain(registries)


# ---------------------------------------------------------------------------
# 2. Out-of-band peer bootstrap (file / URL)
# ---------------------------------------------------------------------------


def bootstrap_peers_from_path(path: str | Path) -> list[SignedPayload]:
    """Load and verify signed peer records from a local file.

    The file must be a JSON array of SignedPayload dicts (the same format as
    the registry HTTP API returns).  Records that fail signature verification
    are skipped with a warning; invalid signature == attacker-supplied data.

    Example file (share via QR/USB/email/paste when the registry is blocked)::

        [{"alg": "ed25519", "public_key": "…", "signature": "…",
          "payload": {"peer_id": "…", "endpoints": ["https://…"], …}}]
    """
    raw = Path(path).read_text(encoding="utf-8")
    return _parse_peer_list(json.loads(raw), source="file")


def bootstrap_peers_from_url(url: str, *, timeout_s: float = 15.0) -> list[SignedPayload]:
    """Load and verify signed peer records from an HTTPS URL.

    Useful for CDN-hosted bootstrap lists (Cloudflare Pages, S3, etc.) that the
    censor cannot block without collateral damage.  The URL is verified via the
    system CA store; an attacker who MITMs it still cannot forge signatures.
    """
    import urllib.request

    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; Rynmesh bootstrap)"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:
        raw = resp.read(4 * 1024 * 1024).decode("utf-8")  # 4 MB cap
    return _parse_peer_list(json.loads(raw), source="url")


def _parse_peer_list(data: Any, *, source: str) -> list[SignedPayload]:
    source = source if source in {"file", "url"} else "provided"
    if not isinstance(data, list):
        raise RegistryError(f"bootstrap: expected a JSON array, got {type(data).__name__}")
    records: list[SignedPayload] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            signed = SignedPayload.from_dict(item)
            verify_peer_record(signed)  # Ed25519 gate
        except (KeyError, ValueError, TypeError, RegistryError) as exc:
            log.warning("bootstrap %s: skipping invalid record: %s", source, type(exc).__name__)
            continue
        records.append(signed)
    log.info("bootstrap %s: loaded %d valid peer records", source, len(records))
    return records


# ---------------------------------------------------------------------------
# 3. Peer exchange (gossip via existing peer HTTP)
# ---------------------------------------------------------------------------


class PeerExchangeClient:
    """Ask a known peer for its peer list (``GET /api/v1/peers``).

    This lets a node bootstrap from a single known IP when registries are
    unreachable: one peer → many peers → the whole reachable mesh.

    The peer server must be running a version that exposes the
    ``/api/v1/peers`` route (added to ``peer_http.create_app`` in this PR).
    Records are Ed25519-verified before being returned.
    """

    def __init__(self, endpoint: str, *, timeout_s: float = 15.0) -> None:
        from .peer_http import HttpPeerClient

        self._client = HttpPeerClient(endpoint, timeout_s=timeout_s)

    def exchange(self, *, network_id: str = "rynmesh-main") -> list[SignedPayload]:
        """Return verified signed peer records from the remote peer."""
        try:
            payload = self._client._json(f"/api/v1/peers?network_id={network_id}")
        except Exception as exc:
            raise RegistryError(f"peer_exchange: {exc}") from exc
        peer_list = payload.get("peers", [])
        return _parse_peer_list(peer_list, source=self._client.endpoint)
