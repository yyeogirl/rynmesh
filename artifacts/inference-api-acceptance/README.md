# Inference API acceptance — 2026-09-17

Acceptance result: **passed** for the documented local inference API and dual-node
strict P2P text/function-tool scope, based on the completed real-model checks below
and the 2026-09-18 regression checks (120 backend tests, 12 Services UI tests, Ruff
and the production frontend build).

The user confirmed on 2026-09-18 that they deliberately shut down the test server
after the successful tests and accepted the functionality on that evidence.
The later live retest observed HTTP 503 `model_alias_target_unavailable` and an
SSH timeout while the server was shut down; it completed no inference checks.
Those observations are retained, but are not a functional acceptance failure.
This acceptance does not assert current service availability, completion of the
annotated screenshot, a new installer release or integration with latest main.
See [final-audit.json](final-audit.json) and
[current acceptance status and remaining work](../../docs/LOCAL_INFERENCE_ACCEPTANCE.md).

Machine-readable evidence: [summary.json](summary.json).
Usage and boundaries: [Local inference API](../../docs/LOCAL_INFERENCE_API.md).

## Real model checks

Provider: `117.50.189.73`, V100 32GB, Qwen3-14B-Q4.
Clients: OpenAI Python SDK 3.14.1 and Anthropic Python SDK 1.6.0.

Each of these three paths passed nine SDK checks (27 total):

- Provider-local inference, accessed through an SSH tunnel. This exercises local
  dispatch on the V100; no GPU model was installed on the Windows workstation.
- Windows consumer API → signed encrypted direct HTTP → remote provider.
- Windows consumer API → signed encrypted strict ICE/UDP P2P → remote provider,
  with `transport=ice_udp_direct`, `relay_used=false`, public candidate nominated.

The checks cover multi-turn recall, real Chat Completions streaming, tool-call
round trip, Responses nonstreaming/streaming, Messages nonstreaming/streaming,
and streamed tool-call round trips for both Responses and Messages.

Observed warm-model text streaming (one sample per route, not a benchmark):

| Path | First content | Completion | Content events |
| --- | ---: | ---: | ---: |
| Provider-local | 1,217 ms | 4,827 ms | 16 |
| Remote HTTP | 2,015 ms | 5,843 ms | 16 |
| Remote P2P | 10,359 ms | 17,094 ms | 16 |

P2P measurements include a fresh ICE handshake for each request.

## Cancellation, privacy and permissions

- Closing the client stream cancelled the consumer task and released its Task
  Balance hold: HTTP 47 ms, P2P 1,547 ms in the recorded samples.
- Both recorded final provider tasks were independently confirmed `cancelled`.
- Project keys were revoked after each test. Secrets and prompt/response bodies
  are excluded from the summary artifact.
- Backend tests verify key hashing/persistence, revocation, budgets, loopback and
  forwarded-request rejection, separation from node management, encrypted deltas,
  task binding, incremental P2P events and Windows task-file concurrency.

## Code and UI checks

- Focused backend suite: **94 passed**.
- Existing Services UI tests: **11 passed**.
- Frontend TypeScript/Vite production build: passed.
- Ruff on changed Python implementation, tests and acceptance scripts: passed.
- Browser verification: Services → Manage → API access displays the address,
  discovered model, project budgets and Python example; layout was visually checked.

## Deployment

The test provider's LLM runtime, provider node and gateway were updated. The
original code is backed up at
`/opt/rynmesh/backups/before-inference-api-20260917.tar.gz`.
Model files, service credentials, firewall settings and video model were preserved.
Services remain managed by Supervisor. This work does not build or publish a new
desktop installer or claim compatibility with every agent application.

## Long output update (2026-09-17)

The running Qwen provider and local `qwen` alias now expose a 32768-token output
limit with a 40960-token total context and a 7200-second provider deadline.
Gateway deadlines and bounded stream transport limits were updated accordingly.
The runtime prefills long prompts in chunks to bound attention memory.

Verified on the V100 through the real deployment:
- A local API request with `max_tokens=32768` returned READY.
- A 40933-token prompt plus one generated token completed without GPU OOM.
- A local-to-provider request generated all 2304 requested tokens in 215.7 seconds,
  crossing the former 2048-token and 120-second runtime caps, with a valid final
  usage record, `finish_reason=length`, and stream terminator.
- A complete 32768-token generation was not run. See `long-output.json` for evidence.
- Focused backend regression suites: 97 tests passed; Ruff passed.

Provider code and config backups: `/opt/rynmesh/backups/long-output-20260917/`.

## Qwen thinking option compatibility (2026-09-17)

Fixed rejection of the agent's top-level `enable_thinking` flag. The same boolean
can be supplied inside `chat_template_kwargs`; conflicting and invalid values
are rejected. The setting survives encrypted provider dispatch and controls the
Qwen chat template. Reasoning uses `reasoning_content` in complete responses and
streaming deltas, while final answers use `content`. Split markers and tool-like
text inside reasoning are covered by parser regression tests.

The updated local-to-server API was tested with the user's `hi` prompt with
thinking disabled, enabled with SSE, and enabled via nested template options.
See `thinking.json` for response summaries; raw reasoning is not stored.
Backend regression suite: 116 passed. Provider code backup:
`/opt/rynmesh/backups/thinking-option-20260917/`.

## ZCode output budget compatibility (2026-09-17)

The Rynmesh custom model entry in ZCode used generic limits of 1000000 context
and 128000 output tokens. Its model limits were corrected to 40960 and 32768,
with the original client config backed up in the same private config directory.
The local API now caps requested output to the model limit and estimated remaining
context before reserving quota; oversized input still fails without truncation.
The estimate matches the peer order path's JSON accounting and ceiling division.

Replayed the failed task's retained SDK request after converting it to wire-level
Chat Completions: all 5 messages, 76 tool schemas, thinking enabled, streaming,
and its original 128000-token requested ceiling. It completed successfully:
33649 prompt tokens, 108 output tokens, nonempty final answer, usage, and DONE
in 80.0 seconds. Tools were supplied to the model but none were executed.
Evidence: `zcode-budget.json` (counts only; no prompt or reasoning retained).
Focused API regression tests: 29 passed; Ruff passed.

## Restore strict P2P design (2026-09-17)

The API integration had used `auto`, which preferred the provider's public HTTP
endpoint. This deviated from the intended P2P model. The local inference API now
always dispatches remote orders with `transport=p2p`. Explicit P2P takes precedence
over HTTP and forced-relay environment overrides. Services UI defaults to P2P.
The persistent local node launcher enables P2P, public NAT traversal and distinct
public egress checks. No inference API fallback to HTTP, TURN or relay is allowed.

Real calls using the unchanged local API address, key and `qwen` alias passed in
both nonstreaming and SSE modes (16.1s and 10.2s). Both recorded `ice_udp_direct`,
`relay_used=false`, a server-reflexive public path, and strict public checks.
See `api-strict-p2p.json`. Registry/STUN remain connection coordination services;
they do not carry the encrypted inference request or generated answer.
Validation: 87 backend tests, 12 Services UI tests, production build and Ruff passed.
