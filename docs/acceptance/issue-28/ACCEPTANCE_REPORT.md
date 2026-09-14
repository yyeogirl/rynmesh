# Formal acceptance report: Transport-backed Private AI writes (#28)

Decision: ACCEPTED
Reviewed commit: `933c312c6ef5e3e37799840078c9a1fd84a40e5c`
Branch: `codex/issue-28-transport-post`
Acceptance date: 2026-09-02

## Scope under acceptance

This report accepts only issue #28: bounded POST support across the Transport
seam and migration of the three Private AI peer HTTP writes. Streaming,
settlement-ledger unification, background-worker refactoring, and public-WAN
P2P certification are excluded.

## Evidence collected

### Static and focused automated verification

| Check | Result | Evidence |
|---|---|---|
| Ruff on changed Python and focused tests | PASS | `All checks passed` |
| Transport unit suite | PASS | 25 passed |
| Transport + LLM focused regression | PASS | 66 passed |
| Git whitespace validation | PASS | `git diff --check` clean |
| Branch isolation | PASS | One issue-specific commit based on public main |

The focused tests cover exact body and content type, network authentication,
exact/max+1 response boundaries, redirect rejection, fronted Host behavior,
CDN-WebSocket framing, REALITY streaming bounds, meek inner envelopes, ECH
fallback, UTF-8/invalid JSON handling, plugin fail-closed behavior, and private
marker exclusion.

### Isolated multi-process acceptance

Docker Desktop could not start on the Windows host because its own stale
`dockerInference` runtime socket crashed the engine. No application code caused
that failure. An equivalent isolated five-process topology was run instead:
Registry, encrypted Relay, Provider node, Consumer node, and an existing local
OpenAI-compatible model server, each with separate data directories.

| Flow | Result | Safe evidence |
|---|---|---|
| Direct task creation | PASS | `peer_http_direct`, `relay_used=false`, non-empty output |
| Direct settlement | PASS | Provider contained exactly one matching earning event |
| Direct cancellation | PASS | Consumer reached `cancelled`; no output was returned |
| Encrypted Relay regression | PASS | `encrypted_relay`, `relay_used=true`, succeeded |
| Cleanup | PASS | All temporary node/registry processes stopped; test ports released |

No prompt or model output is reproduced in this report.

## Complete-suite status

The Windows full suite reached 531 passing tests and 3 skips. Seven remaining
failures are pre-existing platform assumptions: POSIX executable/0600 modes,
an unavailable WSL bash, and `select()` on a Windows subprocess pipe. They do
not execute changed #28 code. These are not treated as Linux CI evidence.

The Windows-only exclusions above are superseded by the successful Ubuntu
backend and Docker LLM E2E jobs recorded below. They remain useful portability
follow-up items but are not acceptance exceptions for issue #28.

## Acceptance checklist

- [x] Product specification is present and linked.
- [x] Traceable functional, security, and reliability requirements are present.
- [x] Development plan and rollback constraints are documented.
- [x] Transport exposes bounded POST bytes.
- [x] Every bundled Transport has a POST implementation.
- [x] `HttpPeerClient.post_json` has stable, bounded JSON behavior.
- [x] Task, settlement, and cancellation writes use the active Transport.
- [x] Authentication, profile, proxy, and redirect policies are preserved.
- [x] Private bodies are absent from public errors and acceptance evidence.
- [x] Direct and encrypted Relay multi-process flows pass.
- [x] Focused tests and lint pass.
- [x] Required remote CI jobs pass on the reviewed commit.
- [x] Final reviewer decision and acceptance date are recorded.

## Remote CI evidence

Pull request: https://github.com/yeogirlyun/rynmesh/pull/32
Reviewed CI commit: `2e074d70cf9cba5a93655241e47b97f7bda08448`
Workflow run: https://github.com/yeogirlyun/rynmesh/actions/runs/33643713617

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

Accepted. Issue #28 meets its functional, security, privacy, compatibility,
documentation, focused-test, full-suite, E2E, packaging, and desktop compile
requirements. It is ready for maintainer review and merge through pull request
#32. This decision does not accept any explicitly excluded follow-up scope.
