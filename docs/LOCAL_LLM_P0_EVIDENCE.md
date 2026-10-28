# Local LLM P0 acceptance evidence

Validated on 2026-08-24 in branch `feature/local-llm-dual-node`. “Pass” below
means an implementation plus automated or recorded runtime evidence exists.
The deterministic adapter is used only for repeatable protocol automation;
non-mock evidence is recorded separately in `REAL_LLM_VALIDATION.md`.

> **Checkpoint status — 2026-08-25:** this branch is reviewable work in
> progress, not a final P0 sign-off. The isolated-Compose, forced encrypted
> relay, local real-model, asynchronous submission and live cancellation
> scenarios below pass. The rebuilt Windows Consumer ran remotely and the exact
> host-native Provider accepted its strict order, but both STUN mappings used
> the shared company-VPN public exit. The final different-egress NAT-hole-punch
> run and its nominated-candidate evidence remain outstanding. The older
> encrypted-relay run is retained as security evidence only and is **not**
> counted as P2P-direct success.

| # | Result | Evidence |
|---:|:---:|---|
| 1 | Pass | `rynmesh-llm setup` is the single wizard entry; managed/import users write no Python, CUDA, Docker, or server command. |
| 2 | Pass | `hardware.detect_hardware` reports OS/CPU/RAM/disk/NVIDIA memory/driver/Docker. Actual report captured Windows, 8 CPUs, 32,653 MiB RAM, Quadro P2200 5,120 MiB and Docker; missing capabilities produce readable warnings. |
| 3 | Pass | `hardware.recommend` uses 75% available RAM, 85% free VRAM, actual disk bounds, conservative context/concurrency, and returns no unsafe default. |
| 4 | Pass | Managed Qwen2.5 0.5B Q4_K_M download verified official LFS SHA-256, started llama.cpp, and completed a real self-test in 171 ms. |
| 5 | Pass | GGUF magic/readability/size/hardware/fingerprint validation plus actual owner model mounted read-only; no copy/upload/rewrite, and post-test existence verified. |
| 6 | Pass | OpenAI-compatible `/v1/models` health, `/v1/chat/completions` inference, and streaming probe passed in unit integration and against a real local 4B GGUF runtime (`streaming=true`). |
| 7 | Pass | Actual `status`, `stop`, `start`, `restart` (through update), `update`, self-test, and safe uninstall passed. |
| 8 | Pass | Default environment uninstall reported `MODEL_AFTER_ENV_UNINSTALL=True`; separate confirmed delete reported `False`; imported-model delete was refused and the user file remained. |
| 9 | Pass | `ProviderService.publish` advertises capability, context, benchmark, price/bounds, online health, capacity/concurrency/queue, alias/license/risk/content/privacy policy; configured Providers publish on startup and refresh every 30 seconds, with a WebApp manual retry. |
| 10 | Pass | `test_manifest_public_view_has_no_paths_urls_or_key_names` proves the public record excludes URL, API-key reference, runtime command, absolute path, and filename. |
| 11 | Pass | Publish refuses unhealthy services; every order rechecks adapter health; full slots return explicit `capacity_exhausted`. |
| 12 | Pass | Imported/private models publish only owner alias plus full local SHA-256 fingerprint; documentation explicitly limits what that fingerprint proves. |
| 13 | Pass | Compose Provider and Consumer use different identities, ports, config and Docker volumes and establish Ryn discovery/connectivity. |
| 14 | Pass | Consumer discovered one Provider service through the registry in deterministic, relay, and real runs. |
| 15 | Pass | Consumer local Ryn API created a stable task, froze balance, signed/encrypted the prompt, and submitted it. |
| 16 | Pass | Provider invoked an owner-managed real 4B GGUF llama.cpp backend; Chrome WebApp acceptance succeeded (26 input, 10 output tokens, 5,920 ms, output `RYNMESH_TWO_NODE_E2E_OK`). |
| 17 | Pass | Consumer calls only Provider Ryn port. In Compose it is not attached to `provider-runtime`; the model service has no Consumer/host port in isolated profiles. |
| 18 | Pass | Provider evidence records `created -> accepted -> running -> succeeded`; failed, timed-out, cancelled and rejected are terminal states. |
| 19 | Pass | Bounded semaphore plus `reject_when_full`; automated capacity test proves no inference call is dropped or silently accepted. |
| 20 | Pass | Terminal encrypted response cache executes duplicate task once; hold/settlement/earning IDs are idempotent; released holds can be safely re-held for reconnect recovery. |
| 21 | Pass | Runtime scan found zero validation-prompt plaintext matches in Registry, Relay, Provider and Consumer persisted data. Registry sees sanitized capacity, encrypted blob hashes and body-free settlements only. |
| 22 | Pass | Forced-relay Compose run succeeded through a separately operated relay. Relay persisted ciphertext; plaintext scan count was zero and signature/ciphertext tamper test fails authentication. |
| 23 | Pass | Body logging defaults false; Provider stores ciphertext/metadata only. `RYNMESH_LLM_DEBUG_BODIES=1` is the sole opt-in and emits a warning. |
| 24 | Pass | Request/response temp ciphertext is deleted in `finally`; container scan found zero `*.ciphertext` temp files. Unit test also proves persisted JSON contains neither prompt nor output. |
| 25 | Pass | Design/runbook state that Provider/runtime sees plaintext, Python memory cannot guarantee erasure, and Rynmesh is not confidential computing or “absolute privacy.” |
| 26 | Pass | `credits.py` is unchanged reputation Credits; `task_balance.py` uses explicit `rynmesh-dev-task-balance-v1` / `DEV_TASK_BALANCE`. README/runbook keep names and claims separate. |
| 27 | Pass | Consumer freezes before send, settles successful actual amount, refunds excess, and releases on failure/cancel. Provider earns only after signed acknowledgement. |
| 28 | Pass | Automated repeated settle/earn returns the same record; duplicate task reuses encrypted result and does not rerun inference or debit again. |
| 29 | Pass | Billing has task/service/node IDs, input/output tokens, duration, currency and amount; forbidden body/secret fields are rejected and tests scan events for prompt absence. |
| 30 | Pass | Ledger state and every user-facing document say development-only/simulated; no real payment backend is claimed. |
| 31 | Pass | `python scripts/llm_e2e.py run` starts Registry, separate Relay, Provider, Consumer and test runtime; real variants are one command after model/runtime selection. |
| 32 | Pass | Automated direct and forced-relay E2E both completed publish, discover, order, encrypted inference, result and settlement. |
| 33 | Pass | `REAL_LLM_VALIDATION.md` records reproducible, desensitized non-mock connection, managed, import and real two-node inference hashes/usage. |
| 34 | Pass | `down` stops; `clean` removes only the named E2E containers/networks/volumes and never deletes a host model. Managed/import cleanup behavior was separately verified. |
| 35 | Pass | Current focused LLM/services/transport suite: `40 passed`; public-P2P audit suite: `2 passed`; Webapp: `24 passed`. Current Windows full suite: `488 passed / 13 failed / 3 skipped`; remaining failures are platform-only categories outside this task. |
| 36 | Pass | `ruff` passes on every changed Python file; TypeScript/Vite production build passes; Vitest 24/24 passes. Full-repo ruff retains seven pre-existing `sim/` findings. Linux full pytest from the earlier Windows checkout checkpoint: 484 pass, 2 pre-existing CRLF shell-syntax failures, 3 skip. |
| 37 | Pass | README plus design, runbook, real-validation and this evidence file cover all three modes, topology/demo, privacy/payment boundaries and troubleshooting. |
| 38 | Pass | Work began from clean committed `f631682` in a separate worktree/branch. Diff is limited to the package, node route integration, E2E, docs, entry point, ignore and focused tests; no original-worktree files were read/overwritten. |
| 39 | Pass | Provider setup is available through API/Webapp for managed llama.cpp, read-only GGUF import, OpenAI-compatible and Ollama profiles. Setup self-tests but remains unpublished until an explicit publish action; pause and persisted settings are covered by tests. |
| 40 | Pass | Consumer submission returns a task ID without blocking, polls signed state/progress, supports live cancellation, keeps body-free task history, encrypts retained results for 0/1h/24h/7d, never persists prompts, clears terminal history, and reconciles interrupted balance settlement idempotently. |
| 41 | Pass | Services initializes discovery from `NodeSettings.network_id`; equal aliases display node and package identity and are selected with a compound peer/package key. A regression test submits the exact second Provider/package. |
| 42 | Pass | Strict acceptance can require distinct STUN public mappings. Shared-exit attempts fail closed with `p2p_distinct_public_egress_required`, actionable UI guidance, released hold and no relay fallback. |
| 43 | Pass | `scripts/audit_public_p2p.py` verifies Registry signatures and signer identities, body-free signaling fields, srflx-only candidates, distinct public mappings, accepted/completed states, exact requester/Provider continuity, bidirectional byte evidence and `relay_used=false`; shared-exit evidence is rejected by test. |

## Verification commands

```bash
python -m pytest -q tests/test_llm_package.py tests/test_services.py \
  tests/test_peer_messaging_http.py tests/test_credit_policy.py tests/test_consumption.py
python -m ruff check rynmesh tests qa scripts
cd webapp && npm run build && npm test -- --run
python scripts/llm_e2e.py run
python scripts/llm_e2e.py relay-run
python scripts/llm_e2e.py host-real-run
python scripts/llm_e2e.py clean
```

## Known baseline/platform failures

The 13 Windows full-suite failures are unchanged baseline categories outside
this task: locale-default GBK decoding of UTF-8 examples/docs, POSIX executable
and `0600` mode assertions on NTFS, missing WSL bash, Windows `select()` on a
subprocess pipe. A
Linux container run removes those Windows-only categories but retains two
pre-existing CRLF shell syntax failures because it mounts the Windows checkout.
No LLM package, task, settlement, relay, Compose, ruff, TypeScript, or Vitest
check fails.

## Current review checkpoint

- Branch: `feature/local-llm-dual-node`.
- Strict public transport is fail-closed: public ICE candidates only; no host
  candidate and no task-relay fallback.
- The new physical remote Consumer ran, discovered two equal-alias Providers,
  and the corrected attempt was accepted by the exact host-native Provider.
  Strict UDP nomination timed out because both machines' STUN candidates used
  the same company-VPN public exit. No payload was relayed.
- A new fail-closed package detects that shared-exit condition early. The
  remaining gate is to place one node on a genuinely different public exit,
  rerun the package remotely and record the nominated public ICE pair.
- Package `remote-consumer-20260825-142504.zip` was copied and started on the
  remote machine. It automatically loaded `rynmesh-llm-e2e`, distinctly listed
  both Provider/node/package options, and failed strict task
  `task_8478211f1dc144e4aea515592d9c1294` in about ten seconds with actionable
  shared-public-exit guidance. DEV balance stayed `100.000`; held balance
  returned to `0.000`.
- The current UX immediately returns a task ID, shows queued/connecting/running
  progress, supports cancellation, preserves terminal history, exposes only
  sanitized errors, and refreshes released Task Balance.
- This checkpoint must not be described as completed public P2P acceptance.

## P1 intentionally deferred

Ollama-specific performance tuning (basic Ollama support is present), streaming
token delivery, hard mid-request backend interruption, AMD/Apple-Silicon
acceleration, more formats/engines, dynamic pricing, signed release packages,
production payment, disputes and chargebacks.
