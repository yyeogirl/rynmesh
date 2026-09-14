# Product specification: Transport-backed Private AI writes (#28)

Status: implemented and formally accepted
Owner: Rynmesh maintainers
Last reviewed: 2026-09-02

## Product decision

Private AI service writes must use the same selectable peer Transport as peer
reads. A user or operator who selects direct HTTPS, a fronted connection,
CDN-WebSocket, REALITY, meek, or ECH must not silently fall back to raw urllib
when creating, settling, or cancelling an LLM task.

## User problem

Before this change, discovery and downloads honored the configured transport,
but the three Private AI POST operations bypassed it. In filtered or proxied
networks, the service could appear discoverable while task creation failed.
Security controls such as bounded reads, redirect rejection, and common error
handling were also duplicated.

## User outcome

- Configuring one peer Transport applies consistently to Private AI reads and
  writes.
- Task creation, settlement acknowledgement, and cancellation retain their
  current UI and API behavior.
- A provider cannot return an unbounded response to consume client memory.
- A selected legacy plugin that cannot POST fails explicitly; traffic is never
  downgraded to another transport without notice.
- Prompts, model outputs, encrypted bodies, and credentials remain absent from
  logs and public errors.

## In-scope behavior

1. Add bounded POST bytes to the Transport contract.
2. Support POST in all bundled transports.
3. Add JSON POST to `HttpPeerClient` with object-only response parsing.
4. Route Private AI task, settlement, and cancellation writes through it.
5. Preserve the existing 2 MiB LLM peer-response limit.
6. Preserve network-key authentication, explicit proxy policy, TLS profile,
   fronting, and redirect rejection.

## Out of scope

- Token streaming and partial response delivery.
- Changes to task encryption, signatures, pricing, settlement schema, or ICE.
- Automatic fallback between transports.
- Support for a generic unrestricted HTTP client.

## Compatibility and rollout

This is additive for the built-in transports. Third-party Transport plugins
must implement `post_bytes` before they can carry service writes. An old plugin
continues to support existing reads, but a write returns the stable
`peer_transport_post_unsupported` error instead of bypassing operator policy.

No migration of stored data is required. Rollback consists of reverting this
change as one unit; authentication, response limits, or redirect protection
must not be weakened independently.

## Product success conditions

- A direct Private AI request completes with `transport=peer_http_direct` and
  `relay_used=false`.
- Provider settlement is recorded exactly once.
- A cancellation reaches the provider write path without producing plaintext
  output.
- The encrypted Relay mode still completes with `relay_used=true`.
- CI passes backend, webapp, packaged-node, deterministic P2P/Relay E2E, and
  desktop compilation jobs.

## Related documents

- Requirements: `docs/requirements/ISSUE_28_TRANSPORT_POST_REQUIREMENTS.md`
- Development plan: `docs/ISSUE_28_TRANSPORT_POST_WORK_PLAN.md`
- Acceptance report: `docs/acceptance/issue-28/ACCEPTANCE_REPORT.md`
- Transport architecture: `docs/RYNMESH_TRANSPORT_CENSORSHIP.md`
