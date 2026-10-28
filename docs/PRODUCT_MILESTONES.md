# Rynmesh Product Milestones

Status: active roadmap, reviewed 2026-08-16. This document describes the
user-facing product milestones and the contribution areas that move them
forward. Protocol-level goals and safety gates remain in
[`RYNMESH_VISION.md`](RYNMESH_VISION.md).

Rynmesh is alpha software. “Implemented” below means the behavior exists in
the repository and is covered by the current release process; it does not mean
the behavior has completed production hardening or broad field validation.

The evidence each milestone must produce before it is claimed, and what a
feature ships with beyond working code, are defined in
[`TESTING_STRATEGY.md`](TESTING_STRATEGY.md). Its §5 records where coverage is
currently short of the claims on this page.

## Current release: P1 Ryn Companion

The personal-assistant milestone is implemented and available in the public
`v0.6.2` release for macOS on Apple Silicon and Intel.

### Implemented

- A self-contained Tauri desktop application starts and monitors its bundled
  Ryn node daemon. A Python, Node.js, Ollama, account, API key, registry, or
  connected peer is not required for the default experience.
- Background discovery starts automatically from a built-in public catalog.
  It covers YouTube, Reddit, research, technology and world news, podcasts,
  public-domain audiobooks and books, images, and comics. Sources are refreshed
  independently and cached content remains available during temporary source
  failures.
- The For You feed ranks real public content, reports discovery health and the
  next refresh, and displays unread recommendation notifications.
- The local recommendation profile learns from topic and platform choices,
  written direction, opening content, More, Less, Hide, and source feedback.
- The content viewer supports articles, YouTube video, feed-provided audio, and
  images, with original-source links for unsupported or failed rendering.
- Reading history, bookmarks, progress, read-later links, and page watchers are
  stored locally and can be exported or erased.
- Search & Ask, digest briefings, and item summaries use an optional
  `ModelProvider`. Ollama is supported locally; Anthropic is available only
  after explicit cloud-model permission and owner-supplied credentials. The
  recommendation feed itself does not require a model.
- Recommendation evidence packets identify the reviewed material, ranking
  signals, review depth, safety status, provenance status, and limitations.
- The local control API, webapp, MCP tools, peer discovery, encrypted peer
  messaging, signed content publication and fetching, provenance validation,
  safety receipts, and non-transferable credit ledger are implemented.
- The release pipeline produces a wheel, source archive, checksums, installer,
  and native macOS DMGs. CI verifies the backend, web build, packaged-node UI,
  and both desktop architectures.

### P1 hardening still needed

These are active contribution areas, not claims that the personal assistant is
missing entirely:

1. **Webapp regression tests.** The Python backend has broad automated
   coverage, while the React critical path currently relies on typechecking,
   production builds, and manual use. Add deterministic component and
   interaction tests for first launch, discovery health, preferences,
   feedback, notifications, and the content viewer.
2. **Recommendation-path consolidation.** Home and For You still span the
   Daily Digest and older recommendation contracts. Converge on one contract,
   one feedback vocabulary, and one source of profile state; remove the
   dormant Recommendations screen after migration.
3. **Source observability and recovery.** Make per-source health, last success,
   cache use, failure reasons, and retries understandable and actionable.
4. **Learning transparency.** Show what positive and negative signals Ryn has
   learned, why the ranking changed, and allow individual feedback actions to
   be undone.
5. **Viewer completeness and privacy.** Improve accessibility, error handling,
   PDF and generic-document support, media fallbacks, and the policy for
   node-mediated versus direct third-party media requests.
6. **Desktop distribution.** Add Windows and Linux packaging. Add Apple
   Developer ID signing and notarization when the project has the required
   maintainer credentials.
7. **Safety hardening.** The current keyword scanner is an alpha protocol
   implementation. Stronger safety packs, evidence retention, quarantine, and
   moderation/appeal behavior are required before operating an open network of
   untrusted peers.

P1 success is measured with voluntary, privacy-preserving evidence: successful
installation, reliable first recommendations, repeat feed use, feedback use,
and actionable failure reports. Rynmesh does not require centralized behavioral
telemetry to work.

## P2: Friend Mesh

Goal: make a group of two to five trusted nodes more useful than one node while
preserving local control.

Implemented foundations:

- signed peer identity and registry-assisted discovery
- encrypted direct messages and small attachments
- signed publication and verified peer fetches
- credit and serve-receipt primitives
- peer mailbox (delivered, #35): sealed, signed, short-TTL registry
  store-and-forward so pairing and chat reach an offline or endpoint-less node
  (`docs/PEER_MAILBOX.md`)

Planned product work:

1. One-click invite links and QR joining with explicit network and endpoint
   review before acceptance.
2. Friend-attributed content ranked inside For You, with an inspectable record
   of the publisher and serving node.
3. Reliable small-mesh setup, connection diagnosis, revocation, and recovery.
4. Safe multi-user egress sharing with per-user, short-lived credentials.
5. A visible contribution history explaining how non-transferable reputation
   was earned. Credits remain reputation, not money.

P2 gate: the invite and revocation paths are safe for non-technical users, and
friend-origin content can be distinguished, verified, muted, and removed.

## P3: Working Agent and Services

Goal: let an owner-approved agent perform useful work across nodes within clear
limits.

Status: the first service landed early. The local-LLM package delivers an
encrypted node-to-node task protocol, provider publication and discovery, a
task-first Services catalog with a Private AI chat, strict-P2P transport
checks, and a development-only Task Balance ledger. Strict public-internet
acceptance and the items in `SERVICE_PLATFORM_NEXT.md` remain before this
graduates from preview.

Planned work:

1. A budgeted agent loop with permitted action types, per-period limits,
   confirmations, and a complete local audit trail.
2. A general service manifest and invocation protocol based on the existing
   work-order path, with metering and result verification. The LLM package is
   the reference implementation; the service-experience framework in
   `SERVICE_PLATFORM_NEXT.md` generalizes its seams.
3. Useful initial services such as local model generation (shipped as the
   Private AI preview), media transcoding, and network egress.
4. Agent-to-agent commissioning within the owner’s approval envelope.
5. Credit debits and credits for verified service work. Credits remain
   non-transferable during this milestone; the development Task Balance is
   folded into the credit ledger as part of this item.

## P4: Open-network hardening

Goal: become safe enough to consider interaction with nodes that are not
personally trusted.

Planned work includes sublinear trust weighting, anti-Sybil defenses,
collusion and brigading analysis, EigenTrust integration, newcomer discovery
allocation, authenticated registry writes, peer quarantine, stronger safety
packs, moderation and appeals, and optional third-party attestations.

Public network operation remains gated by the safety, legal, and accountable
stewardship requirements in `RYNMESH_VISION.md`. Publishing the source code and
desktop application does not mean an unrestricted public peer network is ready.

## P5: Economy maturation

Only after sustained demand for real node services should the project evaluate
service pricing, issuance epochs, decay, or transferable credits. Any
transferability requires anti-abuse maturity and legal review. The roadmap does
not promise a token, monetary value, or future redemption.

## Choosing a contribution

[GitHub Issues](https://github.com/yeogirlyun/rynmesh/issues) is the executable
backlog; this roadmap provides direction but does not reserve work. The current
accepted work is grouped in the
[P1 hardening milestone](https://github.com/yeogirlyun/rynmesh/milestone/1).
Contributors should choose an unassigned issue labeled `good first issue` or
`help wanted`, comment before beginning substantial work, and open a focused
pull request linked to that issue.

Recommended order for new contributors:

1. Webapp critical-path test foundation.
2. Source-health details and recovery UX.
3. Recommendation learning explanation and undo.
4. Recommendation-contract consolidation.
5. Content-viewer format, accessibility, and privacy hardening.
6. Linux and Windows desktop packaging.

Cryptography, node identity, authentication, credit issuance, registry trust,
VPN credential sharing, and transferable-credit design require a maintainer
design issue before implementation because mistakes can cross security and
compatibility boundaries.
