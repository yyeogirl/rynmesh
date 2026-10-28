# Requirements: service background-worker registry (#27)

Status: implemented and formally accepted
Last reviewed: 2026-09-02

## Functional requirements

- **FR-27.1 — Explicit registration.** A service package shall register a
  named `BackgroundWorkerSpec` during application construction.
- **FR-27.2 — Unique ownership.** Worker names shall be unique; duplicate and
  post-start registration shall fail clearly.
- **FR-27.3 — Supervised lifecycle.** The registry shall create exactly one
  task per registered worker and shall cancel and await all tasks on shutdown.
- **FR-27.4 — Sync and async execution.** Synchronous callables shall execute
  through `asyncio.to_thread`; async callables/results shall be awaited.
- **FR-27.5 — Activity scheduling.** Worker results shall distinguish activity
  from idle operation and select the configured busy or idle delay.
- **FR-27.6 — Failure supervision.** An ordinary exception shall be captured,
  exposed safely, backed off, and isolated from other workers.
- **FR-27.7 — Cancellation semantics.** `asyncio.CancelledError` shall terminate
  a worker without being recorded as a service failure.
- **FR-27.8 — LLM migration.** Relay polling and Provider publication refresh
  shall register as `llm.relay-poll` and `llm.publish-refresh`; their old node
  loops and app-state callable attributes shall be removed.

## Reliability, privacy, and compatibility requirements

- **NFR-27.1 — Valid scheduling.** Delays and multipliers shall reject invalid,
  negative, zero where disallowed, NaN, and infinite values; backoff is capped.
- **NFR-27.2 — Monotonic scheduling.** Scheduling metadata shall use a monotonic
  clock; wall time is display-only.
- **NFR-27.3 — Metadata-only observability.** Registry status and error text
  shall not contain worker arguments/results, prompts, outputs, keys, URLs,
  task envelopes, or private paths.
- **NFR-27.4 — Status compatibility.** `/api/local/llm/service/status` shall
  retain `background.publication_error` and `background.relay_poll_error`.
- **NFR-27.5 — Behavior preservation.** Relay busy/idle/error cadence and the
  thirty-second Provider refresh shall remain equivalent to previous behavior.
- **NFR-27.6 — Protocol preservation.** No LLM, Registry, Relay, settlement, or
  stored-data schema shall change.

## Verification matrix

| Requirement | Implementation | Verification |
|---|---|---|
| FR-27.1–3 | `rynmesh/background_workers.py`, `rynmesh/peer_http.py` | Registration, duplicate, sealing, lifecycle tests |
| FR-27.4 | Registry invocation boundary | Thread identity and async-await tests |
| FR-27.5–7, NFR-27.1–2 | Registry runner | Deterministic injected-sleep/backoff/cancellation tests |
| FR-27.8, NFR-27.4–6 | `rynmesh/llm_package/routes.py` | App construction, status, LLM regression and E2E |
| NFR-27.3 | Sanitized registry state | Unique private-marker and bounded-error tests |
| NFR-27.5 | LLM worker policies | Spec assertions and multi-process publication/Relay evidence |

## Definition of done

The requirement is accepted only when every requirement above has evidence in
`docs/acceptance/issue-27/ACCEPTANCE_REPORT.md`, the branch is clean, and the
repository's required CI jobs pass on the reviewed commit.
