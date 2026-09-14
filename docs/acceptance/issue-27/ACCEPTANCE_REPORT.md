# Formal acceptance report: service background-worker registry (#27)

Decision: ACCEPTED
Reviewed commit: `18b17bcb48e1a14f20e45649425174809ff2d780`
Branch: `codex/issue-27-background-worker-registry`
Acceptance date: 2026-09-02

## Scope under acceptance

This report accepts only issue #27: a supervised in-process worker registry and
migration of Private AI Relay polling and Provider publication. Updater,
Digest, Daily Recap, Signal50, user-submitted inference threads, distributed
queues, and persistent scheduling are excluded.

## Evidence collected

### Static and focused automated verification

| Check | Result | Evidence |
|---|---|---|
| Ruff on changed Python and focused tests | PASS | `All checks passed` |
| New registry + LLM focused regression | PASS | 56 passed |
| Registry-only suite | PASS | 14 passed |
| Git whitespace validation | PASS | `git diff --check` clean |
| Legacy scheduler search | PASS | No production `llm_*_once` or old loop symbols remain |

The focused suite covers validation, deterministic listing, duplicate and late
registration, sync thread offload, async awaiting, busy/idle/error backoff,
failure isolation, recovery/error clearing, cancellation, shutdown awaiting,
monotonic metadata, status privacy, LLM registration, and lifespan start/stop.

### Isolated multi-process acceptance

Docker Desktop could not start on the Windows host because its own stale
`dockerInference` runtime socket crashed the engine. An equivalent isolated
five-process topology was therefore run: Registry, encrypted Relay, Provider,
Consumer, and an existing local OpenAI-compatible model server, with separate
node and registry data directories.

| Flow | Result | Safe evidence |
|---|---|---|
| Worker registration | PASS | Exactly `llm.publish-refresh` and `llm.relay-poll` |
| Automatic publication refresh | PASS | Capacity record timestamp advanced on the 30-second cadence |
| Strict P2P inference | PASS | `ice_udp_direct`, `relay_used=false`, succeeded |
| Encrypted Relay processing | PASS | `encrypted_relay`, `relay_used=true`, succeeded |
| Relay settlement | PASS | Exactly one matching Provider earning event after polling |
| Existing status contract | PASS | Both background error fields present and empty after success |
| Shutdown | PASS | Registry unit/lifespan tests await tasks; host processes stopped |

No prompt, model output, URL, credential, or private path is reproduced in this
report.

## Complete-suite status

The Windows full suite reached 533 passing tests and 3 skips. Seven failures
are pre-existing POSIX/Windows assumptions: executable bits, `0600` modes, an
unavailable WSL bash, and `select()` on a Windows subprocess pipe. One unrelated
Signal50 atomic-replace stress test was flaky under Windows and passed when
rerun alone. None exercises the changed registry or LLM scheduler code.

The Windows-only exclusions above are superseded by the successful Ubuntu
backend and Docker LLM E2E jobs recorded below. They remain useful portability
follow-up items but are not acceptance exceptions for issue #27.

## Acceptance checklist

- [x] Product specification is present and linked.
- [x] Traceable functional, reliability, privacy, and compatibility requirements exist.
- [x] Development plan and rollback constraints are documented.
- [x] Worker specification and registry are implemented and validated.
- [x] Sync work is offloaded and async work is awaited.
- [x] Failures are isolated with bounded backoff and recoverable error state.
- [x] Shutdown cancels and awaits registered workers.
- [x] Both LLM workers use the registry; old scheduler wiring is removed.
- [x] Existing LLM background status shape remains compatible.
- [x] Status/errors exclude service-private bodies and configuration details.
- [x] Strict P2P and encrypted Relay multi-process flows pass.
- [x] Focused tests and lint pass.
- [x] Required remote CI jobs pass on the reviewed commit.
- [x] Final reviewer decision and acceptance date are recorded.

## Remote CI evidence

Pull request: https://github.com/yeogirlyun/rynmesh/pull/33
Reviewed CI commit: `d78a56cd837984cb34e32b05f2a98f041b9a2239`
Workflow run: https://github.com/yeogirlyun/rynmesh/actions/runs/33643742045

| CI job | Result |
|---|---|
| contribution-workflow | PASS |
| backend | PASS |
| webapp | PASS |
| llm-e2e | PASS |
| packaged-node | PASS |
| desktop-compile (x86_64) | PASS |
| desktop-compile (aarch64) | PASS |

## Final decision

Accepted. Issue #27 meets its functional, reliability, privacy, compatibility,
documentation, focused-test, full-suite, E2E, packaging, and desktop compile
requirements. It is ready for maintainer review and merge through pull request
#33. This decision does not accept any explicitly excluded follow-up scope.
