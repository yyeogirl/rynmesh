# Issue #35 — cross-NAT peer mailbox (work plan)

Status: in progress (system track). Tracks
[#35](https://github.com/yeogirlyun/rynmesh/issues/35). Unblocks
[Simple Pair & Share](product/user/USER_SIMPLE_PAIR_AND_SHARE_WORK_PLAN.md)
across home networks (#30) and revocation notices to offline friends.

## Problem

Two friends on different home networks cannot complete an invite: the
accepter's node has to reach the inviter's node, and neither is directly
reachable. The private LLM service already solves the same problem with a
registry-hosted "work order" mailbox (`llm.relay-poll`), but that mechanism is
LLM-specific (capability/operation schema, no ack, orders accumulate as files,
6 h client-side expiry) and `PeerMessenger` has no store-and-forward path at
all — an offline peer just gets `delivered: false`.

## Design

### Envelope (`rynmesh/mailbox.py`)

One signed, sealed, bounded message addressed to a peer id (peer ids are the
base64 Ed25519 public keys, so the registry can verify both parties):

```
outer (SignedPayload by sender's identity key) over:
{
  "version": "rynmesh.mailbox.v1",
  "kind": "<application kind, e.g. friend.invite.accept.v1>",
  "message_id": <32 hex>, "from_peer_id", "to_peer_id",
  "created_at", "expires_at",            # RFC 3339 UTC; TTL ≤ 24 h, default 1 h
  "ephemeral_pub", "nonce", "ciphertext" # peer_box seal to the recipient's X25519 key
}
```

`seal_mailbox_message(...)`/`open_mailbox_message(...)` mirror
`llm_package/task_protocol.py` (fresh ephemeral X25519 key per message,
ChaCha20-Poly1305, HKDF). `open` verifies the outer signature, `from_peer_id ==
signer`, `to_peer_id == me`, expiry, kind (optional), and that the inner body
repeats `message_id`. Hard cap: 64 KiB serialized envelope.

A poll is a signed request, not a bare GET:

```
{"kind": "mailbox_poll", "peer_id", "issued_at", "nonce", "ack": [message_id...], "limit"}
```

verified with `signer == peer_id`, `issued_at` within ±300 s, nonce not seen
before (bounded cache). Acked ids are deleted before the response is built.

### Registry (`rynmesh/mailbox_store.py`, routes in `registry_http.py`)

- `FileMailboxStore(root)`: `<root>/mailbox/<to_peer_id sha256[:2]>/<to_peer_id sha256>/<message_id>.json`.
  `deposit(signed)` validates the envelope (signature, size, TTL bounds,
  future `created_at` tolerance), dedupes by `message_id`, enforces
  256 pending per recipient and a per-sender token bucket (120/min), sweeps
  expired files on access. `poll(signed_poll)` verifies, deletes acks, sweeps,
  returns up to `limit` (≤ 50) oldest-first.
- Routes: `POST /api/v1/mailbox/deposit` and `POST /api/v1/mailbox/poll`
  (both behind the existing network-key middleware; bodies are the signed
  payloads, like work orders). Errors map to 400 (invalid), 409 (duplicate),
  429 (cap/rate), never echo envelope contents.
- `PeerRegistry` protocol, `FilePeerRegistry`, `HttpPeerRegistry` gain
  `deposit_mailbox(signed)` and `poll_mailbox(signed) -> list[SignedPayload]`.

The registry stores ciphertext plus routing metadata only. No invite secret,
network key, or local-control token is ever placed in an envelope; the pairing
acceptance payload carries a proof (`HMAC(invite_secret, accepter_peer_id)`),
never the secret itself — that contract is documented for the user track.

### Node (`rynmesh/mailbox_client.py`, worker `mailbox.poll`)

- `MailboxClient(store, messaging_key, resolve_messaging_pub)`:
  `deposit(to_peer_id, kind, body, *, ttl_s=3600, to_messaging_pub=None)`;
  `register_handler(kind, fn)`; `poll_once() -> int` (activity count for the
  worker): verify + open each envelope, dispatch by kind, ack on success; a
  handler exception leaves the message unacked for up to 3 attempts, then it
  is acked and counted as dropped (kind + id logged, never the body).
  Replay guard: bounded seen-id cache persisted at `<home>/mailbox/seen.json`.
- `install_mailbox(app, *, store, messaging_key, resolve_pubkey, workers)`
  registers the worker (`BackoffPolicy` busy 2 s, idle 5→60 s ×1.5, error ×2
  max 120 s, `replace=True`) only when the node has a registry, and exposes
  `app.state.mailbox` for other packages. `GET /api/local/mailbox/status`
  returns bounded metadata (pending handled counts, last poll, error class).
- Messaging-key discovery for unreachable peers: auto-register publishes
  `metadata.messaging_pub` in the `PeerRecord`; `_resolve_pubkey` falls back to
  the registry record when the direct `/api/peer/pubkey` call fails. Pairing
  invites additionally carry the inviter's messaging key, so acceptance never
  depends on discovery.

### Store-and-forward for peer messages

`PeerMessenger.send` gets an optional `fallback` callable: when direct
delivery fails and the sealed header fits the envelope cap, it is deposited as
kind `peer.message.v1` and the local record gains `via: "mailbox"` with
`delivered: false`. The node registers a handler for that kind that runs the
existing `receive` path and publishes to the SSE stream. Larger attachments
keep today's behaviour (undelivered, local copy kept).

### Acceptance test topology

- In-process two-node test: two `RynmeshStore`s, a real registry HTTP server
  thread (pattern from `tests/test_rynmesh.py::test_http_relay_client_streams_artifacts_through_registry_server`),
  node A deposits `friend.invite.accept.v1` for node B, B polls, handler
  fires, ack removes it, replay rejected, expired ignored, foreign poll
  rejected.
- Docker E2E: `scripts/llm_e2e.py mailbox-run` sends a peer message from the
  consumer to the provider with direct delivery disabled
  (`RYNMESH_MESSAGING_FORCE_MAILBOX=1`) and asserts it arrives through the
  registry mailbox; wired into the `llm-e2e` CI job.
- Distinct-egress physical run stays manual (#22).

## Out of scope

Hole punching / ICE for pairing (the LLM P2P path stays LLM-only for now),
push notifications, mailbox for large blobs (use the relay blob store),
the pairing handlers themselves (user track, #30).

## Tasks

1. `mailbox.py` + `mailbox_store.py` + registry routes + registry protocol and
   clients + tests (unit + `TestClient` round trip).
2. `mailbox_client.py` + worker + `install_mailbox` wiring + messaging-pub
   publication/fallback + status route + tests (unit + real-server two-node).
3. `PeerMessenger` fallback + `peer.message.v1` handler + `mailbox-run` E2E +
   CI step + docs (`docs/PEER_MAILBOX.md`, roadmap, issue #30 contract note).

## Acceptance

- [ ] Node A behind one network, node B behind another, both only reaching
      the registry: an `friend.invite.accept.v1` envelope deposited by A is
      delivered to B's handler exactly once and acked (in-process test +
      Docker E2E).
- [ ] Registry never stores or logs plaintext bodies; envelopes are
      ciphertext + routing metadata; poll requires the recipient's signature.
- [ ] Replayed, expired, oversized, foreign-recipient, and unsigned envelopes
      are rejected with safe errors; per-recipient and per-sender caps hold.
- [ ] A peer message to an unreachable peer is queued through the mailbox and
      arrives when the peer polls; the sender's record shows `via: "mailbox"`.
- [ ] `mailbox.poll` appears in `/api/local/llm/service/status`-style worker
      status and survives registry outages with bounded backoff.
- [ ] `python -m ruff check rynmesh/ tests/`, full pytest, and the `llm-e2e`
      job pass.
