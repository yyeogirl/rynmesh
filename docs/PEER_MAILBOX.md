# Peer Mailbox — store-and-forward for offline nodes

Delivered in issue #35. Modules: `rynmesh/mailbox.py` (envelope and poll
protocol), `rynmesh/mailbox_store.py` (registry-side spool),
`rynmesh/mailbox_client.py` (node-side client and dispatcher),
`rynmesh/mailbox_routes.py` (node wiring), `rynmesh/registry_http.py` (the two
registry routes).

## What it is

Two RynMesh nodes normally talk directly: the sender resolves the recipient's
endpoint from the registry and POSTs sealed bytes to it. That fails whenever the
recipient is offline, asleep, or behind a NAT with no reachable endpoint — which
is the common case for laptops and phones, and the *only* case for two nodes
that have never met.

The mailbox is a small, registry-hosted spool that holds sealed messages until
the recipient collects them. Its properties, all enforced in code:

- **Registry-hosted.** The registry is the one host both peers can already
  reach. It stores and forwards; it never becomes a party to the conversation.
- **Sealed.** The body is encrypted to the recipient's X25519 messaging key with
  a fresh ephemeral key per message. The registry holds ciphertext only. It
  cannot read a body, and re-labelling an envelope does not work: `kind` and
  `message_id` are repeated *inside* the seal and re-checked on open. The seal
  is domain-separated (HKDF info `rynmesh-mailbox-v1`) from the direct
  peer-message channel, so a ciphertext captured off the registry cannot be
  replayed into `/api/peer/msg`, or the reverse.
- **Metadata is not hidden.** The envelope is plaintext by design: the registry
  sees the sender and recipient peer ids, the `kind`, the message id, the
  timestamps and the size. Only the body is sealed.
- **Best-effort, not guaranteed.** A hostile or broken registry cannot read,
  forge or alter mail, but it *can* withhold, delay or reorder it, and a client
  has no way to detect that. Treat delivery as best-effort within the TTL: never
  depend on a message arriving, and never on two messages arriving in order.
- **Signed.** Every deposit and every poll is an Ed25519-signed payload.
  `from_peer_id` must equal the signer, so an envelope cannot claim a sender it
  does not hold the key for.
- **Short TTL.** Default one hour, hard maximum 24 hours. The mailbox is a relay
  buffer, not an inbox — expired mail is swept, not archived.
- **Ack on poll.** The recipient acknowledges message ids on its *next* poll.
  Delivery is at-least-once; the client's seen-cache makes duplicates harmless.
- **Tombstones.** An acked message leaves a `<message_id>.acked` marker holding
  only its `expires_at`, so a captured envelope cannot be re-deposited and
  re-delivered inside its TTL. Tombstones expire with the message and do not
  count against the recipient's pending budget, but they are bounded too: at
  most 2048 per box, oldest evicted first, so a box churning faster than its TTL
  cannot grow markers without limit.

## Envelope and poll shapes

A deposit is a `SignedPayload` whose payload is the envelope:

```json
{
  "version": "rynmesh.mailbox.v1",
  "kind": "friend.invite.accept.v1",
  "message_id": "<32 lowercase hex>",
  "from_peer_id": "<base64 Ed25519 pub>",
  "to_peer_id": "<base64 Ed25519 pub>",
  "created_at": "2026-09-03T10:00:00Z",
  "expires_at": "2026-09-03T11:00:00Z",
  "ephemeral_pub": "<base64, 32 bytes>",
  "nonce": "<base64, 12 bytes>",
  "ciphertext": "<base64>"
}
```

The plaintext under `ciphertext` is canonical JSON
`{"message_id": ..., "kind": ..., "body": {...}}` — the two repeated fields are
what bind the sealed body to the envelope the registry is serving.

A poll is a `SignedPayload` of kind `mailbox_poll`:

```json
{"kind": "mailbox_poll", "peer_id": "...", "issued_at": "...",
 "nonce": "...", "ack": ["<message_id>", "..."], "limit": 50}
```

Routes (both under `/api/v1`, so a network key hides them as 404):

| Route | Body | Response |
|---|---|---|
| `POST /api/v1/mailbox/deposit` | signed envelope | `{message_id, expires_at, pending}` |
| `POST /api/v1/mailbox/poll` | signed poll | `{"messages": [signed envelope, ...]}` |

Status codes: `409` duplicate or replayed poll, `429` recipient full, sender
quota exceeded, or sender rate-limited, `400` anything else. The detail is a
short stable code (`duplicate`, `rate_limited`, `recipient_full`,
`sender_quota`, `expired`, …) and never interpolates peer-supplied text.

## Caps

| Cap | Value | Where |
|---|---|---|
| Envelope size | 64 KiB | `MAX_ENVELOPE_BYTES` |
| Poll response | 1.5 MiB | `MAX_POLL_RESPONSE_BYTES` (under the 2 MiB client read limit) |
| Poll batch | 50 messages | `MAX_POLL_LIMIT` |
| Ack ids per poll | 200 | `MAX_ACK_IDS` |
| TTL | 1 h default, 24 h max | `DEFAULT_TTL_S`, `MAX_TTL_S` |
| Kind length and charset | 96 chars, `[A-Za-z0-9][A-Za-z0-9._-]*` | `MAX_KIND_LEN`, `_require_kind` |
| Clock skew | 300 s | `POLL_SKEW_S` |
| Pending per recipient | 256 | `FileMailboxStore(max_pending_per_recipient=…)` |
| Pending per (sender, recipient) | 16 | `FileMailboxStore(max_pending_per_sender=…)` |
| Tombstones per box | 2048 (oldest evicted) | `MAX_TOMBSTONES_PER_BOX` |
| Deposits per sender | 120/min (token bucket) | `FileMailboxStore(sender_rate_per_minute=…)` |

**These caps are not admission control — the network key is.** Any peer holding
`RYNMESH_NETWORK_KEY` may deposit into any box. The per-recipient cap bounds
disk; the per-(sender, recipient) cap is what stops one such peer from filling
somebody else's box and starving every other sender for a full TTL. Neither is a
substitute for keeping the network key private.

The sender bucket is per-process and in-memory: a multi-replica registry grants
`N x rate` in aggregate and resets on restart.

## The poll worker and its backoff

`install_mailbox` registers a supervised background worker named `mailbox.poll`
— only when the node has a registry, since there is otherwise nothing to poll.

| Setting | Value |
|---|---|
| Initial delay | 3 s |
| Busy delay | 2 s |
| Idle | 5 s, x1.5, capped at 60 s |
| Error | x2, capped at 120 s |

A poll counts as *busy* when it handled or dropped anything, so a box full of
replays or unknown kinds still drains at the busy delay instead of backing off
while it fills.

Per message, `poll_once` does: seen-cache replay → ack and drop; bad envelope
(signature, recipient, expiry, size, charset) → ack and drop; **decrypt failed →
count `undecryptable`, retry (no ack) up to three attempts, then ack and drop**;
unknown kind → ack and drop; handler raised → retry (no ack) up to three
attempts, then ack and drop; handler returned → remember, ack, count as handled.

The decrypt-failure case is deliberately not an immediate drop. It means the
envelope is valid and really is addressed to this node but the seal will not
open — a messaging key rotated while the message was in flight, most likely.
Acking on the first look would delete mail the right key could still read, so it
gets the same three-attempt budget a failing handler gets.

It cannot be retried indefinitely, though. A poll serves the oldest 50 envelopes
and has no cursor, so a wall of never-acked messages at the head of a box would
hide every newer message behind it for the full TTL. The budget is what keeps
the queue moving. Every look still counts under `undecryptable` — the counter is
occurrences, not distinct messages — so a key rotation stays visible in
`status()` either way, and one log line per poll records the count and the
exception class, nothing more.

The seen cache lives at `<home>/mailbox/seen.json` (0600 in a 0700 directory,
bounded at 5000 ids, pruned by expiry). It holds two maps: `entries`
(message id → expiry, the replay guard) and `attempts` (message id → retry count
and expiry). The attempt counters are persisted for the same reason the seen
cache is: without them, restarting the node would hand a poison message a fresh
three attempts on every restart, for as long as its TTL runs.

## `GET /api/local/mailbox/status`

Loopback-only, like the rest of `/api/local`.

```json
{
  "handled_total": 3,
  "dropped_total": 0,
  "undecryptable": 0,
  "pending_last": 0,
  "last_poll_at": "2026-09-03T10:04:11Z",
  "last_error": "",
  "handlers": ["friend.invite.accept.v1", "peer.message.v1"],
  "worker": {"name": "mailbox.poll", "running": true, "...": "..."},
  "registry_dropped": 0
}
```

`undecryptable` counts every *look* at an envelope that verified but would not
open (see above), so one message failing its three attempts adds three. A number
that keeps climbing is the signature of a messaging key that rotated under mail
still in flight. `registry_dropped`
is `HttpPeerRegistry.dropped_mailbox_messages` — envelopes the node's own
registry client refused on the way in, because they failed verification or were
addressed to somebody else. It is present only when the configured registry
counts them (a fallback chain reports the sum over its mirrors; a file-backed
registry omits the key). A rising number means a registry is serving mail this
node will not accept.

`last_error` is an exception *class* name or the sentinel `no_registry` — never
a message, a path, or anything derived from a body. No count, field, or log line
in this subsystem carries message content.

## Registering a handler from a package

```python
def on_invite_accept(envelope, body: dict) -> None:
    # `envelope.from_peer_id` is proven by the deposit signature — prefer it
    # over anything the body claims about who sent this.
    ...

app.state.mailbox.register_handler("friend.invite.accept.v1", on_invite_accept)
```

Raising from a handler means "not handled": the message is retried on the next
two polls and then dropped. Returning normally acks it. **Handlers must be
idempotent** — delivery is at-least-once, and the seen cache is an optimization,
not a guarantee.

To send:

```python
app.state.mailbox.deposit(peer_id, "friend.invite.accept.v1", body)
```

`deposit` resolves the recipient's messaging key itself: the TOFU cache, then a
direct `/api/peer/pubkey` call, then `metadata.messaging_pub` on the peer's
signed registry record. The last step is what makes two endpoint-less nodes able
to seal for each other at all.

## Kinds in use, and kinds reserved for pairing

### `peer.message.v1` (delivered)

Store-and-forward for 1:1 chat. The body *is* the sealed `PeerMessenger` header
that would have been POSTed to `/api/peer/msg`. When a direct send fails,
`PeerMessenger` offers the already-sealed header to a fallback that deposits it
under this kind; the local history record gets `via: "mailbox"` and keeps
`delivered: false`. On the receiving side the handler runs `messenger.receive`
— the same decryption, the same history write — and publishes the record to the
`/api/local/messages/stream` SSE feed, so a mailbox-carried message is
indistinguishable from a direct one once it lands.

The handler is idempotent, as every handler must be. `PeerMessenger.receive`
looks the sender's `msg_id` up in that peer's history first; if it is already
there it returns the stored record marked `duplicate: True` without appending,
re-saving the attachment, or re-publishing, and both `/api/peer/msg` and the
relay skip the SSE publish on that marker. This is not a theoretical case: a
direct POST whose *response* is lost after the recipient processed it leaves the
sender believing delivery failed, so the fallback queues the very same message
into the mailbox.

Two rules the relay enforces beyond the direct route:

- The header's `from` must equal the envelope's proven `from_peer_id`. A peer
  may only relay messages that say they are from itself. Note the scope: this
  binds what the *relay* writes — history lines and the TOFU key-cache entries
  it seeds. It is not a network-wide guarantee, because the unauthenticated
  direct `/api/peer/msg` route can still seed the same TOFU cache from a header
  it did not verify. That gap predates the mailbox and is tracked separately;
  the mailbox path simply does not widen it.
- A header that serializes above 47 KiB (`MAX_MAILBOX_HEADER_BYTES = 47 * 1024`)
  is never offered to the mailbox: it could not fit a 64 KiB envelope once
  sealed and base64'd. Large attachments stay direct-only.

### `friend.invite.accept.v1` (contract, user track)

```json
{
  "invite_id": "...",
  "accepter_peer_id": "<base64 Ed25519 pub>",
  "accepter_messaging_pub": "<base64 X25519 pub>",
  "proof": "<hex HMAC-SHA256(invite_secret, accepter_peer_id)>",
  "display_name": "..."
}
```

The proof demonstrates knowledge of the invite secret without transmitting it.
The inviter recomputes the HMAC over `accepter_peer_id` with its own copy of the
secret and compares in constant time.

### `friend.revoke.v1` (contract, user track)

```json
{"relationship_id": "...", "reason_code": "...", "revoked_at": "<RFC 3339>"}
```

`reason_code` is a short enum, not free text.

### The rule for every kind

**No invite secret, network key, or local-control token ever travels in an
envelope.** An envelope proves possession (a signature, an HMAC over a value the
holder already knows); it never carries the credential itself. A body is
end-to-end encrypted, but it is also stored on a third-party host for up to
24 hours, and a leaked credential outlives that.

## Upgrading: message history moved to the store home

Message history and attachments used to live under `$RYNMESH_HOME/messages`.
They now live under the *store's* home — `<store.home>/messages` — beside the
`messaging.x25519` key that decrypts them and the `mailbox/seen.json` the poll
worker keeps. The two are the same directory in every default install, and
nothing needs doing there. **If your node runs with `$RYNMESH_HOME` pointing
somewhere other than the store home, move the directory once, while the node is
stopped:** `mv "$RYNMESH_HOME/messages" <store-home>/messages`. Skipping the
move does not lose anything — the old files stay where they are — but the node
starts from an empty history, and a message it had already received could be
stored (and shown) a second time, because the dedupe check reads the history it
can see. The node logs a warning at startup when the two homes disagree.

## Environment switches

| Variable | Effect |
|---|---|
| `RYNMESH_REGISTRY_URL` | The registry that hosts the mailbox. No registry, no mailbox and no poll worker. |
| `RYNMESH_MESSAGING_FORCE_MAILBOX=1` | The node's direct peer-message transport returns 0 without attempting delivery, so every send takes store-and-forward. Test and E2E aid — never set it in production. |
| `RYNMESH_NETWORK_KEY` | Shared-mesh key. The mailbox routes live under `/api/v1`, so an unkeyed caller gets 404. |
| `RYNMESH_REGISTRY_DIR` | Registry data root; the spool is `<root>/mailbox`. |

## Verifying it end to end

`python scripts/llm_e2e.py mailbox-run` brings up the Docker stack with
`RYNMESH_MESSAGING_FORCE_MAILBOX=1` on the consumer only, sends a marker message
to the provider, asserts the send came back `via: "mailbox"` with
`delivered: false`, then waits for the marker to appear in the provider's
history and for the provider's mailbox status to report a handled message. It
runs in CI as *Deterministic peer-message mailbox flow*. Only the marker text is
printed; no message body ever reaches the output.
