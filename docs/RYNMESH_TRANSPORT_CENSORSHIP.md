# Rynmesh Transport & Censorship Resistance

How should Rynmesh nodes talk to each other across hostile networks (e.g. the
Great Firewall of China, GFW), and should we replace HTTPS with our own private
protocol?

## TL;DR

- **Yes, the GFW *can* block HTTPS** to a *specific* endpoint (by SNI, IP, or
  DNS) — but only once that endpoint is on its radar.
- **Inventing our own non-HTTPS protocol would make Rynmesh _more_ blockable,
  not less.** The GFW actively detects and drops unknown, fully-encrypted,
  random-looking protocols. Obscurity is the opposite of censorship-resistance.
- The durable answer is **not one bespoke protocol** but a **pluggable
  transport layer** whose default looks like ordinary HTTPS to a host the
  censor cannot afford to block. Rynmesh's "private protocol" already exists and
  belongs at the **application layer** (Ed25519-signed, content-addressed
  objects) — the transport underneath should be *boring and camouflaged*, not
  exotic.

## 1. Threat model: how the GFW actually blocks

Censorship is not "it sees HTTPS and blocks it." HTTPS is ~the majority of all
traffic; blocking HTTPS wholesale is impossible. The GFW blocks *identifiable*
things:

1. **DNS poisoning/injection** — returns bogus answers for blocked domains.
2. **SNI filtering** — the TLS ClientHello carries the server name in
   *cleartext* (TLS 1.2, and 1.3 without Encrypted Client Hello). The GFW reads
   it and RST-resets connections to blocked names. This is the dominant TLS
   censorship method.
3. **IP blocklisting + RST injection** — known proxy/server IPs are reset or
   null-routed.
4. **Active probing** — after a suspicious flow, the GFW itself connects to your
   server and fingerprints it (this is how it has historically detected and
   killed Shadowsocks and obfs4 servers: they answered unknown handshakes in a
   distinctive way).
5. **DPI protocol fingerprinting** — recognizes OpenVPN / IPsec / WireGuard /
   Tor handshakes by their distinctive bytes and blocks them.
6. **Fully-encrypted-traffic detection** — (Wu et al., *USENIX Security 2023*)
   the GFW deploys heuristics over the first packet bytes (entropy / popcount /
   fraction of printable ASCII) to flag traffic that looks like *pure random
   bytes with no recognizable structure*, and probabilistically blocks it. **A
   naive custom encrypted protocol is exactly this.**
7. **Throttling** — rather than a hard block, it can degrade a protocol until
   it is unusable (it has done this to TLS-based circumvention).

The exposed surface for Rynmesh is specifically the **cross-border** path: a
node inside the censored region talking to a node/registry/relay outside it.
Domestic-only mesh traffic is not the GFW's concern.

## 2. Why a bespoke "no-HTTPS" protocol loses

- **A fixed binary protocol on a fixed port is a unique fingerprint.** Once
  Rynmesh is noticed, that fingerprint is trivially classified and blocked
  network-wide. HTTPS doesn't have that problem because it is shared with the
  whole internet.
- **Headerless high-entropy streams trip the fully-encrypted detector (#6).**
  The more "private and encrypted from byte 0" our protocol is, the more it
  looks like exactly what the GFW already probabilistically drops.
- **Active probing (#4) kills distinctive servers.** Any server that responds
  to an unknown handshake in a unique way can be enumerated and blocked.
- **It is an arms race we cannot win bespoke.** The tools that still work in
  China (Shadowsocks-2022/AEAD, VLESS+XTLS-**REALITY**, meek, Snowflake) all
  converged on the *same* conclusion: stop looking unique, start looking like
  ubiquitous allowed traffic. A small team reinventing a wire protocol will be
  fingerprinted faster than it can iterate.

**The principle that actually works:** make your traffic *indistinguishable
from traffic the censor will not block*, and put your real endpoint behind
*collateral damage* (a shared CDN/host the censor cannot afford to take down).

## 3. Where Rynmesh's "private protocol" should live

Rynmesh already has the right idea in the right place: every artifact is
**content-addressed and Ed25519-signed** (manifests, provenance chains, safety
receipts, credits, peer records). That is the protocol that gives Rynmesh its
integrity and trust — and it is **transport-agnostic**. A manifest verifies the
same whether it arrived over HTTPS, a WebSocket, QUIC, a relay blob, or a USB
stick.

So the rule is:

- **Application layer (keep, strengthen):** signed, content-addressed objects.
  This is our real "private protocol." It needs no transport secrecy to be
  trustworthy.
- **Transport layer (make pluggable + camouflaged):** the bytes on the wire
  should imitate boring, allowed traffic. Never a custom exotic format.

## 4. Recommended architecture: a pluggable transport seam

Today `peer_http.py`, `relay.py`, and `registry.py` each hardcode HTTP via
`httpx`/`urllib`. Step one is to introduce a small `Transport` abstraction so
the wire format is swappable without touching call sites:

```
Transport (interface)
  dial(endpoint) -> Conn         # client
  serve(handler)                 # server
  # frames carry the existing signed/content-addressed payloads
```

Then transports plug in, in increasing cost/strength:

| Tier | Transport | What the censor sees | Cost | Notes |
|------|-----------|----------------------|------|-------|
| 0 | **Plain HTTPS (TLS 1.3)** *(today)* | a TLS session to your host's SNI/IP | none | works until specifically targeted; add ECH to hide SNI |
| 1 | **WebSocket-over-TLS behind a CDN** | a TLS session to a *major CDN* | low | high collateral damage; the visible name is the CDN, not you |
| 2 | **TLS handshake mimicry (REALITY-style)** | a real handshake "borrowed" from a popular site | high | no domain to block, active-probe resistant; most robust |
| 3 | **QUIC / HTTP-3 masquerade** | UDP that looks like browser HTTP/3 | medium | different fingerprint surface; GFW throttles some QUIC |
| 4 | **Pluggable-Transport bridge (Snowflake / meek)** | WebRTC / fronted HTTPS | medium | reuse Tor's PT ecosystem for worst-case regions instead of reinventing |

Design notes:

- **Default stays boring HTTPS.** Obfuscating transports are an *opt-in,
  per-region/per-operator* policy — not on by default everywhere.
- **ECH (Encrypted Client Hello)** is the modern, standards-track way to hide
  the SNI that SNI-filtering (#2) relies on; prefer it over dead domain
  fronting.
- **Don't roll your own crypto handshake.** Use TLS (and let REALITY/uTLS-style
  libraries provide the mimicry) so our handshake matches a real browser's.

## 5. Discovery & relay are the bigger censorship targets

Wire obfuscation is necessary but not sufficient. The GFW blocks *coordination
points* more easily than peer-to-peer flows:

- **The registry** (`registry_http.py`) is a fixed domain/IP — a single SNI/IP
  block cuts discovery. Mitigations: multiple registries, signed peer lists
  distributable out-of-band (QR/file/another channel), a domain-fronted/ECH
  registry, and gossip so the mesh survives losing the registry.
- **The relay** (`relay.py`, used for NAT-safe exchange) is likewise a fixed
  endpoint. Same mitigations + multiple relays.
- Rynmesh already has good seams here: the **offline peer cache** + the
  `PeerDirectory` collaborator + content-addressed **relay blobs** + gossip-able
  signed peer records. The censorship story is mostly about *operating* these
  redundantly and reachably, not new protocol invention.

## 6. What NOT to do

- ❌ Invent a new on-the-wire encryption protocol (re-trips detector #6, loses
  the arms race, weaker crypto than TLS).
- ❌ Run on a fixed nonstandard port.
- ❌ Emit headerless, high-entropy streams from byte 0.
- ❌ Rely on classic domain fronting (the big CDNs disabled it ~2018) — use ECH.

## 7. Implementation status

**Done (this change):** `rynmesh/transport.py` introduces the `Transport`
interface and a default, **zero-dependency** `StdlibHttpsTransport`.
`HttpPeerClient` now performs all peer GET, bounded download, and JSON POST I/O
through it, so the wire format is swappable without touching peer logic. The
Private AI direct task, settlement, and cancellation POSTs use this same client;
selecting `camouflage`, `fronted`, `cdn-ws`, `reality`, `meek`, or `ech` therefore
applies to both discovery reads and service writes. The application trust model
(signed, content-addressed objects plus encrypted LLM envelopes) is unchanged
and remains the real "private protocol."

What the default transport already does:

- **Camouflage profile (default):** browser-like `User-Agent` + `Accept*`
  headers, TLS minimum 1.2, browser ALPN (`h2`, `http/1.1`). No longer
  self-identifies as `Rynmesh/0.1` on the wire.
- **Redirect suppression:** peers can't bounce the client to an unvalidated /
  censor-controlled host (SSRF-safe).
- **Outbound proxy:** `RYNMESH_HTTPS_PROXY` / `RYNMESH_HTTP_PROXY` route peer
  traffic through an HTTP-CONNECT proxy — the hook for a Tor / pluggable-
  transport bridge in hostile regions.
- **Active-probe resistance (server):** set `RYNMESH_NETWORK_KEY` and the peer
  surface (`/api/v1/*`, `/health`) requires a salted-hash auth header; an
  unauthenticated probe (e.g. a censor fingerprinting the port) gets an
  indistinguishable generic **404** — the server never reveals it runs Rynmesh.
- **SNI / connect-host / Host split (`FrontedHttpsTransport`):** when
  `RYNMESH_TLS_SNI` or `RYNMESH_CONNECT_HOST` is set, peer traffic dials one
  host, presents a *different* (benign) SNI in the cleartext ClientHello, and
  sends the real backend as the `Host` header. This directly defeats the GFW's
  dominant TLS method (SNI filtering, §1.2) and is the practical substitute for
  ECH — see note below. Pure stdlib (`socket` + `ssl` + `http.client`).
- **Plugin registry:** `register_transport(name, factory)` + `RYNMESH_TRANSPORT`
  let heavier transports drop in by name.
- **Bounded POST responses:** `Transport.post_bytes(...)` enforces a caller-set
  response limit just like GET/download. Required mesh authentication is added
  after caller headers, so a service call cannot accidentally replace it.
- **Meek POST envelope:** write requests use the versioned
  `rynmesh.transport.request.v1` JSON envelope inside the outer bridge POST. The
  encrypted application body is base64 encoded in the envelope; it is never
  exposed as an outer CDN header. A Rynmesh meek relay must understand this
  envelope before `RYNMESH_TRANSPORT=meek` can carry write traffic.

> **On ECH (Encrypted Client Hello):** ECH is the standards-track way to encrypt
> the SNI entirely. It is **not yet usable from Python** — it needs OpenSSL
> 3.5+ ECH APIs that CPython's `ssl` module does not expose. Until it lands, the
> SNI/connect/Host split above is the practical stand-in (present a benign SNI
> rather than hiding it). When CPython exposes ECH, it becomes one more setting
> on this same transport — no call-site changes.

### Operator config

| Env var | Effect |
|---------|--------|
| `RYNMESH_TRANSPORT` | `camouflage` (default) · `direct` (legacy id) · `hardened` (TLS 1.3 only) · a registered plugin name |
| `RYNMESH_NETWORK_KEY` | shared key; enables active-probe resistance on the peer surface (set on every node in the private mesh) |
| `RYNMESH_HTTPS_PROXY` / `RYNMESH_HTTP_PROXY` | route peer traffic via a CONNECT proxy / PT / Tor bridge |
| `RYNMESH_TLS_SNI` | cleartext SNI presented in the TLS ClientHello — set to a benign/allowed name; cert is validated against it. Activates the fronting transport. |
| `RYNMESH_CONNECT_HOST` | IP/host the TCP socket actually dials (e.g. a CDN edge / unblocked IP). Activates the fronting transport. |

Recommended hardened deployment for a censored region: run the peer server on
**:443** behind a real reverse proxy / CDN with a valid certificate, set
`RYNMESH_NETWORK_KEY` mesh-wide, and (where needed) point
`RYNMESH_HTTPS_PROXY` at a bridge.

**Built in this PR:**

- **CDN-WebSocket (`cdn-ws`)** — tunnels peer HTTP over `wss://` to a CDN edge.
  What the censor sees: a browser WebSocket upgrade to `cdn.example.com`. The
  edge is shared by millions of sites; blocking it causes enormous collateral
  damage. Zero third-party deps (stdlib WebSocket framing over `ssl.wrap_socket`).
  Config: `RYNMESH_TRANSPORT=cdn-ws RYNMESH_CDN_WS_URL=wss://ryn.cdn.example.com/ryn-ws`.

- **Multi-registry fallback + out-of-band bootstrap + gossip** (`registry_resilience`):
  - `FallbackRegistryChain` / `make_fallback_chain()` — try `RYNMESH_REGISTRY_URLS`
    (comma-separated) in order; a blocked primary does not prevent discovery.
  - `bootstrap_peers_from_path` / `bootstrap_peers_from_url` — load Ed25519-
    verified peer records from a file or URL (CDN-hosted JSON, QR code, USB)
    when all registries are unreachable.
  - `GET /api/v1/peers` gossip endpoint on the peer server — ask any reachable
    peer for its peer list; one peer → many peers → the whole mesh.
  - Together: mesh can bootstrap from a registry, OR a CDN-hosted file, OR any
    single known peer IP. Full registry blackout does not kill the mesh.

**Still not built (next):** XTLS-REALITY-style handshake mimicry (needs a
uTLS/curl_cffi-class fingerprint library — optional plugin), Snowflake/meek PT
bridge, ECH once CPython exposes it.

## References (publicly documented censorship research)

- Wu et al., *How the Great Firewall of China Detects and Blocks Fully
  Encrypted Traffic*, USENIX Security 2023.
- GFWReport.org — ongoing measurement of GFW SNI/IP/probe behavior.
- Tor Project Pluggable Transports (obfs4, meek, Snowflake) design docs.
- XTLS-REALITY (Xray/V2Ray) and uTLS — TLS handshake mimicry.
- RFC 9180 (HPKE) / TLS Encrypted Client Hello (ECH) drafts.
