# Requirements: Transport-backed Private AI writes (#28)

Status: implemented and formally accepted
Last reviewed: 2026-09-02

## Functional requirements

- **FR-28.1 — Bounded POST contract.** `Transport` shall expose a POST method
  accepting serialized bytes, headers, timeout, and maximum response bytes.
- **FR-28.2 — Bundled implementation parity.** Stdlib HTTPS, fronted HTTPS,
  CDN-WebSocket, REALITY, meek, and ECH shall implement the POST contract.
- **FR-28.3 — Peer JSON API.** `HttpPeerClient.post_json` shall serialize UTF-8
  JSON compactly, require a JSON-object response, and accept a caller-defined
  response limit.
- **FR-28.4 — LLM write routing.** Task creation, settlement acknowledgement,
  and cancellation shall call `HttpPeerClient` and the active Transport.
- **FR-28.5 — Plugin fail-closed behavior.** A selected Transport without POST
  support shall return a stable unsupported error and shall not use urllib as
  a fallback.

## Security and privacy requirements

- **SR-28.1 — Response bound.** A response of exactly `max_bytes` shall pass;
  a larger response shall fail with `reason=too_large`.
- **SR-28.2 — No redirects.** A peer POST shall not follow 3xx redirects.
- **SR-28.3 — Mandatory authentication.** Caller headers shall not replace the
  derived `X-Ryn-Auth` network credential.
- **SR-28.4 — Transport policy preservation.** TLS, SNI/connect-host/Host,
  explicit proxy, and camouflage settings shall apply to POST where supported.
- **SR-28.5 — Body confidentiality.** Request/response bodies and body-derived
  markers shall not appear in errors or logs.
- **SR-28.6 — Protocol integrity.** Encryption, signatures, idempotency,
  settlement, and cancellation payload schemas shall not change.

## Reliability and compatibility requirements

- **NFR-28.1.** Blocking direct inference I/O shall remain outside the asyncio
  event loop.
- **NFR-28.2.** Direct, strict P2P, and encrypted Relay modes shall remain
  behaviorally distinct.
- **NFR-28.3.** Transport and peer failures shall use stable metadata-only
  errors.
- **NFR-28.4.** No persisted-data migration shall be required.

## Verification matrix

| Requirement | Implementation | Verification |
|---|---|---|
| FR-28.1, SR-28.1 | `rynmesh/transport.py` | Transport exact-limit and limit+1 tests |
| FR-28.2, SR-28.2–4 | `rynmesh/transport.py`, `rynmesh/transport_plugins.py` | Stdlib/fronted/CDN-WS/plugin tests |
| FR-28.3, FR-28.5 | `rynmesh/peer_http.py` | Peer JSON UTF-8, invalid JSON/object, unsupported tests |
| FR-28.4, SR-28.6 | `rynmesh/llm_package/routes.py` | Focused LLM regression and host E2E |
| SR-28.5, NFR-28.3 | Peer client error mapping | Unique private-marker tests |
| NFR-28.1–2 | Existing async call sites and mode selection | LLM focused suite, direct and Relay E2E |
| NFR-28.4 | No storage/schema changes | Diff review and existing persistence tests |

## Definition of done

The requirement is accepted only when every requirement above has evidence in
`docs/acceptance/issue-28/ACCEPTANCE_REPORT.md`, the branch is clean, and the
repository's required CI jobs pass on the reviewed commit.
