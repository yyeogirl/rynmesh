# Product specification: service background-worker registry (#27)

Status: implemented and formally accepted
Owner: Rynmesh maintainers
Last reviewed: 2026-09-02

## Product decision

Service packages register repeatable background work through one supervised
node facility. Adding a service worker must not require copying an asyncio loop
into the core node lifespan.

## User problem

Private AI previously installed two callable attributes on `app.state`, while
the node discovered them by name and owned two LLM-specific loops. This hidden
contract made every future service change core lifecycle code, encouraged
duplicated backoff/error handling, and made clean shutdown difficult to prove.

## User outcome

- Private AI publication and Relay polling continue automatically.
- A failure in one service worker does not stop another worker or the node.
- Synchronous service work cannot freeze the node's asyncio event loop.
- Worker failures remain visible through existing service-status fields and
  clear after recovery.
- Application shutdown cancels and awaits every registered service worker.
- Future services add a worker in their own package installer and tests.

## In-scope behavior

1. Provide validated worker, result, and backoff specifications.
2. Register workers during route/package installation.
3. Start one supervised task per worker from node lifespan.
4. Run synchronous work in a thread and await asynchronous work normally.
5. Isolate failures and apply bounded busy/idle/error scheduling.
6. Expose metadata-only status internally and preserve the current LLM status
   response shape.
7. Migrate only `llm.relay-poll` and `llm.publish-refresh`.

## Out of scope

- Distributed queues, cron, persistent schedules, and hot plugin unloading.
- User-submitted background inference threads.
- Updater, Digest discovery, Daily Recap, or Signal50 worker migration.
- New public diagnostics endpoints.
- Changes to LLM task, Relay, settlement, or publication protocols.

## Product behavior and recovery

`llm.relay-poll` begins after one second, polls quickly after activity, backs
off to ten seconds while idle, and caps repeated-failure delay at thirty
seconds. `llm.publish-refresh` begins after one second and refreshes every
thirty seconds with bounded failure backoff. A later success clears the
existing `relay_poll_error` or `publication_error` field.

Worker status contains names, run state, timestamps, counters, sanitized error
classes, and monotonic scheduling metadata only. It never contains worker
arguments/results, prompts, outputs, credentials, URLs, or private paths.

## Compatibility and rollout

The LLM status API remains compatible. No persistent storage or public protocol
changes are introduced. The previous `llm_relay_once` and `llm_publish_once`
duck-typed attributes are intentionally removed; internal embedders must use
the registry contract.

Rollback must restore the two former loops and remove the registrations in one
coherent change. Old and new schedulers must never run simultaneously.

## Product success conditions

- Exactly two LLM service worker specifications exist at app construction.
- Automatic Provider publication refreshes without manual intervention.
- Strict P2P and encrypted Relay inference both complete.
- Relay settlement is recorded exactly once.
- A failing worker is isolated and a later success clears its error.
- Closing the application leaves no registered worker running.
- Repository-required CI jobs pass on the reviewed commit.

## Related documents

- Requirements: `docs/requirements/ISSUE_27_BACKGROUND_WORKERS_REQUIREMENTS.md`
- Development plan: `docs/ISSUE_27_BACKGROUND_WORKER_REGISTRY_WORK_PLAN.md`
- Acceptance report: `docs/acceptance/issue-27/ACCEPTANCE_REPORT.md`
- Service platform roadmap: `docs/SERVICE_PLATFORM_NEXT.md`
