# Rynmesh three-node P2P peer transit

Status: implementation contract for `rynmesh.peer-transit.v1`

## 1. Objective

Rynmesh must prefer a direct ICE/UDP path between a source peer and a target
peer.  When that path is unavailable or materially worse, it may route the
session through another ordinary Rynmesh peer:

```text
healthy:   peer 1 =============================== peer 3

degraded:  peer 1 ===== ICE/UDP ===== peer 2 ===== ICE/UDP ===== peer 3
                                      transit peer
```

Both `1 <-> 2` and `2 <-> 3` are separately negotiated ICE connections using
host or server-reflexive candidates.  The registry is a signed signaling
mailbox and STUN discovers public mappings.  Neither carries application data.
TURN candidates are not configured or accepted by this protocol.

Peer 2 is not a privileged cloud relay.  It advertises a normal signed
`rynmesh.peer-transit.v1` capacity and may be replaced by any eligible peer
that can establish both P2P legs.

An online target or transit worker refreshes its signed capacity every 15
minutes. Discovery treats records older than one hour as stale. A failed
refresh is retried on the next worker poll, and file-backed capacity records
are replaced atomically so readers never observe a half-written heartbeat.

## 2. Terminology

- **direct**: one ICE connection from source to target.
- **peer transit**: two direct ICE connections joined by an ordinary peer.
- **TURN relay**: an ICE candidate whose type is `relay`; prohibited here.
- **control plane**: signed capacity, offer, answer, and session metadata in the
  registry mailbox.
- **data plane**: encrypted application frames sent over nominated ICE pairs.

Evidence must use separate fields so that peer transit is never mistaken for
TURN:

```json
{
  "path_mode": "peer_transit",
  "ice_relay_candidate_used": false,
  "transit_peer_id": "<peer-2>"
}
```

## 3. Security model

### 3.1 Identity

Session-open records are canonical JSON signed by the source Ed25519 node key.
The target verifies that the signature key equals `source_peer_id`.  Session
results are signed by the responding node. Every result poll binds the signed
record to the exact work-order ID, network ID, expected provider peer ID and
expected requester peer ID before it trusts either a status or an ICE signal;
a different signed peer cannot race an observed order with a forged result.
The registry enforces the same binding before storing a result, rejects results
without an existing order, and creates work-order IDs exclusively so another
signed requester cannot overwrite an observed order. An order leaves the
provider's open queue only when a result with the order's exact provider,
requester, network and ID is present.

### 3.2 End-to-end encryption

The source creates an ephemeral X25519 key for every transit session.  It
derives a session key with the target's advertised X25519 messaging public key.
The target derives the same key from its local messaging private key and the
source ephemeral public key.

Each data frame uses ChaCha20-Poly1305 with a nonce derived from direction and a
strictly increasing 64-bit sequence number.  Associated data binds:

- protocol version;
- session ID;
- source peer ID;
- target peer ID;
- direction;
- sequence number;
- final-frame flag.

Peer 2 forwards the encrypted frame unchanged.  It knows the two peer IDs,
session ID, timing and byte counts, but cannot decrypt request or response
payloads.

### 3.3 Abuse boundaries

- exactly one transit hop (`hop_limit=1`);
- no arbitrary IP, hostname, URL or port forwarding;
- the target is an authenticated Rynmesh peer ID;
- bounded frame size, session duration, buffered bytes and concurrent sessions;
- monotonic sequence validation and duplicate rejection;
- expired, mismatched, recursively relayed and unsigned sessions are rejected;
- signed work results from an unexpected provider/requester, order or network
  are ignored before their status or signaling fields are interpreted;
- work-order IDs are immutable, orphan results are rejected, and a result from
  an unrelated signed peer cannot hide an order from its intended provider;
- peer 2 never writes payload frames to disk or application logs.

## 4. Protocol

The control capability is `rynmesh.peer-transit.v1`.

### 4.1 Capacity

Each node willing to receive peer-transit traffic advertises:

```json
{
  "capabilities": ["rynmesh.peer-transit.v1"],
  "max_concurrent": 8,
  "metadata": {
    "protocol_version": "rynmesh.peer-transit.v1",
    "roles": ["target", "transit"],
    "messaging_public_key": "<base64 X25519 public key>"
  }
}
```

`max_concurrent` is an enforced worker limit, not descriptive metadata. The
long-running worker dispatches eligible orders through a bounded executor,
tracks work-order IDs already in flight so polling cannot duplicate them, and
waits for active handlers during shutdown. Capacity refresh and discovery stay
on the worker's control loop while each session owns its own two ICE
connections.

The file-backed registry keeps immutable canonical work orders and results for
audit, but workers do not rescan that complete history on every poll. An
auxiliary per-provider `open-work-orders` index contains only availability
markers for orders that have not produced a signed result. A poll always reads
and verifies the canonical signed order and checks its latest signed result;
the marker is never treated as trusted data. Publishing any result removes the
marker, stale markers are repaired on read, and a versioned one-time rebuild
recovers registries created before the index existed. This keeps long-running
poll cost proportional to active work rather than historical session count
without retaining the history in process memory.

Marker deletion is retry-safe on Windows: a sharing violation leaves only an
untrusted stale marker, which the next poll rechecks against the canonical
signed result and removes. Workers expose a lock-protected control-error
snapshot. Hermetic acceptance and the persistent soak require zero main
relay/target control-loop errors, so a transient registry exception cannot be
hidden behind otherwise successful payload transfers.

Concurrent acceptance waits up to five seconds for `finished` callbacks from
the exact signed sessions whose client results have already returned. This is
an observation drain, not a start barrier: it neither delays nor synchronizes
the data plane. A missing callback still fails the report, preventing a
millisecond reporting race from silently dropping a real worker timeline.

### 4.2 Source-to-transit order

The source submits a signed work order to peer 2 containing:

- session ID and target peer ID;
- source-to-transit ICE offer;
- source ephemeral X25519 public key;
- timeout and size limits;
- `hop_limit=1`.

Peer 2 publishes its ICE answer and creates a second signed order for peer 3.

### 4.3 Transit-to-target order

The second order contains the immutable source session identity, the
transit-to-target ICE offer and the original signed session-open record.  Peer 3
verifies the source signature rather than trusting peer 2 to describe peer 1.
It publishes its ICE answer to peer 2.

### 4.4 Data exchange

Once both pairs are nominated:

1. peer 1 sends encrypted request frames to peer 2;
2. peer 2 forwards each frame unchanged to peer 3;
3. peer 3 verifies/decrypts and produces encrypted response frames;
4. peer 2 forwards the response unchanged to peer 1;
5. all three peers emit body-free evidence.

Hop reliability uses bounded chunking, hash validation and acknowledgements.
Each connection sends at most eight unacknowledged UDP fragments per reliable
message window. This keeps 20 simultaneous two-hop streams from starving ICE
consent traffic or overflowing a peer's UDP receive queue while preserving
fair progress across sessions. Application streams are resumable at a verified
chunk boundary; a half-written artifact is never committed as complete.

Large files are divided into 64 MiB resume segments by default, aligned to the
64 KiB application chunk size. The source keeps one random `transfer_id` for
the complete file and signs a manifest for every segment containing its byte
range, segment SHA-256, cumulative-prefix SHA-256, final-file SHA-256 and source
identity. The target fsyncs the segment, recomputes the complete prefix, and
atomically persists a checkpoint before returning a signed receipt. A later
segment therefore starts only at a boundary independently verified by both
ends.

Both partial state and the final published filename are namespaced by the
authenticated source identity together with `transfer_id`. Two sources that
reuse the same transfer ID and filename therefore cannot collide with or
replace one another's completed artifact.

Every resumed segment negotiates a fresh ICE session. The source makes at most
three resume attempts by default and never trusts bytes beyond the last signed
receipt. The target truncates an unconfirmed tail, serializes writers for the
same source/transfer pair, and treats an exact duplicate segment as an
idempotent verification rather than appending it twice. Only a final segment
whose total size and complete-file hash match is renamed from `.part`; the
checkpoint is then removed. Evidence records all segment boundaries, fresh
session IDs, failed attempts and signed target receipts.

## 5. Route selection

The route manager maintains these states:

```text
DIRECT -> DEGRADED -> PEER_TRANSIT -> RECOVERING -> DIRECT
```

Default policy, configurable through environment or local settings:

- three consecutive direct failures trigger transit immediately;
- direct loss above 8% for 30 seconds enters `DEGRADED`;
- direct P95 latency above the configured ceiling enters `DEGRADED`;
- transit is selected only when its score is at least 25% better;
- a selected transit path is held for at least 60 seconds;
- direct must pass five probes over at least 120 seconds before recovery;
- a cooldown prevents oscillation after any path change.

The initial implementation switches at request or verified chunk boundaries.
It does not silently move a partially authenticated frame between paths.

## 6. Scope

The first production slice covers generic Rynmesh request/response bytes and
streamed artifacts.  Content, preview and peer messaging call sites can then use
the routed transport.

The private LLM strict-public-P2P acceptance remains direct-only and continues
to require `relay_used=false`.  Enabling peer transit for LLM tasks requires an
explicit mode and separate evidence; it must not weaken the existing audit.
`net.egress` remains a separate SOCKS/overlay dataplane.

## 7. Acceptance contract

Automated evidence is necessary but physical three-network evidence is the
release gate for public NAT traversal.
The requirement-to-evidence status is tracked in
`P2P_PEER_TRANSIT_ACCEPTANCE_MATRIX.md`.

### A. Healthy direct path

- source-to-target nominated candidates are `host`, `srflx` or `prflx` UDP;
- `path_mode=direct`;
- peer 2 data counters remain zero;
- source and target SHA-256 values match.

### B. Direct path blocked

- network policy blocks peer 1 from peer 3 but permits both peers to peer 2;
- peer 1 to peer 2 and peer 2 to peer 3 each nominate a non-TURN UDP pair;
- `path_mode=peer_transit` and `transit_peer_id` identifies peer 2;
- peer 2 ingress and egress counters cover the transferred payload;
- target output SHA-256 matches the source.

### C. Direct path degraded

- inject 250-350 ms delay, 50-100 ms jitter and 15-20% loss only on `1 <-> 3`;
- route manager changes to peer transit within 30 seconds when it is at least
  25% better;
- requests finish or retry idempotently without corruption or duplication.

The hermetic gate must exercise actual application datagrams on the nominated
local ICE/UDP direct pair, not merely pass synthetic metrics to the route state
machine. It records attempted and deliberately dropped datagrams, the scheduled
RTT range, the intact direct retry result, and a subsequent adaptive request
whose signed evidence and transit counters prove that peer 2 was selected.

### D. Recovery

- removing impairment causes a hysteresis-controlled return to direct;
- no route flapping occurs;
- transit byte counters stop increasing after the return.

### E. Transit failure

- terminating peer 2 is detected within the configured timeout;
- the request uses recovered direct connectivity, another eligible peer, or a
  bounded explicit failure;
- no permanent wait or invalid partial artifact occurs.

### F. No cloud payload relay

- no signaled or nominated candidate has ICE type `relay`;
- TURN server configuration is absent;
- signed remote signaling accepts only `host`, `srflx` or `prflx` UDP
  candidates; `relay`, non-direct and non-UDP candidates are rejected before
  they are added to the ICE agent, and programmatic signals are checked again;
- candidate and related addresses must be unicast IP literals, component 1 and
  valid UDP ports; hostnames, unspecified, multicast and IPv4 broadcast
  destinations are rejected before any DNS lookup or ICE connectivity check;
- after ICE establishment, registry and STUN access can be blocked while the
  transfer continues;
- registry traffic remains control-sized and contains no application body;
- packet counters show payload volume only on `1 <-> 2` and `2 <-> 3`.

### G. Confidentiality and integrity

- a unique plaintext marker is absent from peer 2 logs, storage and captured
  data-plane packets;
- tampered ciphertext, replayed sequence numbers, forged identity, expired
  sessions and `hop_limit != 1` are rejected;
- peer 2 cannot request arbitrary network destinations.

### H. Resource and performance gates

- path change completes within 10 seconds after a hard direct failure;
- the adaptive client bounds its direct attempt to 8 seconds by default and a
  real target-policy failure test must complete through peer 2 within 10
  seconds; a state-machine-only timing assertion is insufficient evidence;
- a transit session establishes within 5 seconds on a healthy test topology;
- one GiB streams without hash mismatch and with traced Python peak memory no
  greater than 128 MiB, independent of the transferred file size;
- 20 concurrent sessions, each carrying at least one MiB, complete without
  deadlock or connection loss; relay and target worker-handler timelines must
  independently prove a peak of 20 for the same signed session IDs;
- a 24-hour soak leaves no live-session or buffer leak;
- on a healthy local topology, protocol overhead is no more than 15% relative
  to the same two-hop forwarding path without application encryption.

## 8. Required evidence

The acceptance tool emits signed or independently derived JSON containing:

```json
{
  "source_peer_id": "<peer-1>",
  "transit_peer_id": "<peer-2>",
  "target_peer_id": "<peer-3>",
  "path_mode": "peer_transit",
  "hop_1": {"local_type": "host", "remote_type": "host"},
  "hop_2": {"local_type": "host", "remote_type": "host"},
  "ice_relay_candidate_used": false,
  "registry_payload_bytes": 0,
  "transit_rx_bytes": 0,
  "transit_tx_bytes": 0,
  "source_sha256": "sha256:<digest>",
  "target_sha256": "sha256:<digest>",
  "plaintext_found_on_transit": false,
  "result": "pass"
}
```

`scripts/audit_peer_transit.py` must reject missing evidence, TURN candidates,
identity discontinuity, byte-count contradictions, plaintext exposure and hash
mismatch. It also recomputes the real impairment loss ratio and rejects missing
delay coverage, duplicate/partial target artifacts, a direct-path transit-byte
increase, or an adaptive degraded request that did not use peer 2. The hermetic
three-identity, real local-UDP/ICE scenario is the
deterministic CI gate; a separate physical run with three public egress networks
is required before claiming public-NAT acceptance.
