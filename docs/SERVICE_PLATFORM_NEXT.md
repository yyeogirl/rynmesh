# Service platform: the next feature set

Status: accepted plan, 2026-08-26; ownership split adopted 2026-09-03.
Sequenced after the post-merge review of `feature/local-llm-dual-node`
(fixes landed in `941e68b`).

## Ownership

Two tracks, visible as labels on the
[work board](https://www.rynmesh.ai/contribute/):

- **`track:system`** — node lifecycle, transport, registry, settlement. Delivered
  by the maintainers directly; reviews of contributed system PRs are taken over
  the line by the maintainers rather than iterated in review rounds.
- **`track:user-facing`** — webapp experiences and product features. Open to
  the core engineer and any contributor: comment `/claim` on the issue,
  `/approve` gates design-sensitive work, reservations expire after seven
  quiet days.

Delivered so far on the system track: the background-worker registry (§2.2,
#27 via #33), Transport-routed peer POSTs (§2.4, #28 via #32), and the
ledger-backed Task Balance (§3, #29).

The local-LLM package proved the shape: encrypted node-to-node tasks, signed
discovery, idempotent settlement, and a task-first catalog. The next set of
work turns that one hand-built service into a platform, finishes the Private
AI experience, and starts the Friend Mesh milestone that gives the network its
first social pull.

## 1. Finish Private AI (preview → dependable)

1. **Strict public P2P acceptance** — the one remaining P0 gate. Run the
   documented two-machine test from a genuinely distinct public egress and
   record the evidence (`LOCAL_LLM_DEVELOPMENT_STATUS.md` has the runbook).
2. **Streaming responses** — the adapter already probes streaming support;
   surface token streaming over the direct path so chat feels live instead of
   40-second silences. Relay/P2P fall back to whole-message delivery.
3. **In-chat provider/model switching** — the conversation store already keys
   by (peer, package); let the user change provider mid-conversation with a
   visible cost/capability comparison.
4. **Digest integration** — "Ask about this item" from For You opens Private
   AI with the article text as grounded context. First real cross-feature use
   of a mesh service.
5. **Bundled native inference runtime (delivered, #34)** — managed/GGUF-import
   modes default to a bundled or downloaded `llama-server` runtime with no
   Docker dependency; Docker remains an opt-in backend for server operators.
   See `docs/ISSUE_34_NATIVE_RUNTIME_WORK_PLAN.md`.

## 2. Service Experience Framework (from review findings)

The four service screens each hand-roll discovery, ordering, and polling; the
node bolts per-service loops into its lifespan. Generalize the seams the LLM
package proved:

1. **Webapp**: service descriptors (capability, operation, region, pricing
   metadata) + shared `useProviderDiscovery` / `useServiceOrder` hooks owning
   polling cadence and provider identity. Screens become thin renderers;
   Video Rendering and Secure Web Access stop hardcoding capability strings
   and regions in components.
2. **Node** (delivered, #33): a background-worker registry (`worker, interval, backoff`) that
   service packages append to, replacing the hand-wired `_llm_relay_poll` /
   `_llm_publish_refresh` pair; the next service must not copy-paste them.
3. **Params policy**: capability param policies now live in
   `jobs.CAPABILITY_PARAM_POLICIES`; video and egress register theirs so the
   no-bodies-in-registry invariant is enforced per capability, not per hack.
4. **Peer transport** (delivered, #32): route LLM peer POSTs through `Transport`/
   `HttpPeerClient` so size caps, redirect blocking, and fronted/CDN transport
   profiles apply to service traffic (today raw urllib bypasses them, and
   censorship-resistant transports cannot carry LLM tasks at all).

## 3. One settlement ledger (delivered, #29)

`TaskBalanceLedger` is a second, parallel balance system next to the signed
`FileCreditLedger`. Before a second paid-ish service ships, extend the credit
ledger with hold → settle/release event kinds (a development-only category,
clearly non-monetary) and make Task Balance a view over it. Every service then
shares one escrow path, one idempotency story, and one auditable history.

## 4. Friend Mesh (P2 start)

The single-user experience is compelling; the network effect starts here:

0. **Peer mailbox (delivered, #35)** — a sealed, signed, short-TTL registry
   spool so invites, acceptances, revocations and chat reach a node that is
   offline or has no reachable endpoint. See `docs/PEER_MAILBOX.md`.
1. Invite links / QR with explicit network + endpoint review before trust.
2. Friend-attributed content ranked in For You with inspectable provenance.
3. Revocation and small-mesh diagnostics that non-technical users can drive.

Private AI makes this concrete immediately: inviting a friend means their node
can use your model — the first tangible answer to "why connect nodes at all?"

## Explicitly not now

- Transferable credits, pricing, or anything money-like (P5 gate unchanged).
- Open untrusted-peer operation (P4 gate unchanged).
