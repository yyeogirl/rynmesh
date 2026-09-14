# Peer transit and GPU Provider integration review

Review date: 2026-09-09. Contributor source: `c6d15f9609d1bad4cdf8cd8e6417a1a6178bad61`.

This integration retains yyeogirl's encrypted peer transit implementation,
acceptance tooling, and GPU Provider runtime, then merges current main and fixes
the findings from the source-commit review. It does not claim a new public-NAT,
V100 hardware, or long-duration soak acceptance run.

## Fixes and regression evidence

| Trigger | Previous behavior | Integrated behavior | Regression test |
| --- | --- | --- | --- |
| Inference overlaps a health/model request | Synchronous GPU work blocked the HTTP event loop | Inference runs in the bounded application thread pool; runtime admission rejects overlapping inference before CUDA allocation, including after HTTP cancellation | `test_generation_keeps_health_responsive_and_rejects_overlap` |
| Two ICE sessions use the fixed UDP port | The second session silently lost its candidates | Cross-thread port reservation rejects overlap with `p2p_capacity_exhausted`; successful or failed closure releases the reservation | `test_fixed_port_overlap_is_rejected_and_port_reusable_after_close`, `test_fixed_port_failed_bind_releases_reservation` |
| Model configuration, CUDA, or ML dependencies are missing | `/v1/models` advertised a model that the Provider treated as healthy | Startup loads the model; unavailable runtimes return 503 from `/v1/models` and report degraded health without private error details | `test_missing_weights_cuda_and_dependencies_fail_readiness`, startup/readiness tests in `test_llm_runtime_server.py` |
| A duplicate resume arrives during an active transfer | The rejected request's cleanup could delete the active partial file | Claim precedes checkpoint inspection; abort only modifies a partial file actually opened by that sink | `test_rejected_overlapping_resume_cannot_delete_active_partial_file` |

Main-branch integration preserves mailbox support and the shared atomic writer
while retaining the contributor's open-order index and serialized media job I/O.

## Verification

- Focused runtime/LLM tests passed before broad validation.
- Full backend run: 970 passed after repairing a missing `ruff` executable in
  the isolated environment. The same backend suite and lint also passed in PR CI.
- Frontend: 53 tests across 10 files passed; TypeScript and production build passed.
- Repository Python lint and whitespace checks passed.
- Three-node acceptance: 8 MiB payload; three concurrent sessions completed;
  both the signed-evidence audit and complete-report audit passed. Coverage
  includes degraded UDP, direct-path failure fallback, recovery after disconnect,
  signaling blackout, and absence of plaintext in transit/control records.
- The initial CI worker smoke completed its transfers but the audit rejected
  missing stdout/stderr artifacts. CI now captures both real process streams
  under the audited root, preserving the strict completeness check.
- Docker is not running locally. Docker service E2E and packaged/desktop gates
  are delegated to the PR's configured CI. This is a one-time integration check.

## Product boundaries

The GPU fixed-port profile supports one active ICE session per process/port.
The general peer transit worker needs two simultaneous sockets and uses the
ordinary ephemeral-port profile. Supporting multiple sessions on one fixed UDP
socket remains a separate feature. No live GPU deployment or recurring
monitoring was started as part of this integration.
