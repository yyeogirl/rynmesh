# Local LLM service package and two-node task MVP

Status: implementation design for the P0 acceptance checklist.

## Boundaries

The Ryn node remains the only control and network gateway. A local model runtime
is never published directly. The directory/registry receives signed service
metadata and task-free settlement coordination only; it must never receive,
forward, generate, or store prompts, context, model outputs, or another task
payload. Task bytes go directly from Consumer Ryn node to Provider Ryn node.
An optional relay is a separate data-plane role and may hold only end-to-end
ciphertext.

The Provider necessarily decrypts a request to run inference. Process isolation,
short-lived in-memory values, disabled body logging, and cleanup reduce exposure;
they are not confidential computing and do not promise absolute privacy.

Rynmesh Credits remain non-transferable contribution/reputation signals. The
development-only **Task Balance** is a simulated spend/earn account. It is not
money, a deposit, or a production payment system. On a node it is persisted as
signed events in the credit ledger's `dev:task_balance` category — one
auditable escrow history for every service — which reputation scoring never
reads; the JSON file next to it is a rebuildable snapshot.

## Components and files

- `rynmesh/llm_package/manifest.py`: versioned package/config schema and a
  redacted public service view.
- `rynmesh/llm_package/adapters.py`: common `health`, `models`, `capabilities`,
  `infer`, `cancel`, `metrics`, and `shutdown` interface; OpenAI-compatible and
  Ollama adapters.
- `rynmesh/llm_package/hardware.py`: OS, CPU, memory, disk, NVIDIA, driver, and
  container probes plus conservative recommendations.
- `rynmesh/llm_package/lifecycle.py`: managed runtime/model download, checksum,
  process lifecycle, GGUF read-only import, self-test, update, and split removal.
- `rynmesh/llm_package/task_balance.py`: idempotent development Task Balance
  holds, releases, settlements, earnings, and body-free billing records.
- `rynmesh/llm_package/task_protocol.py`: signed, X25519/ChaCha20-Poly1305 task
  envelopes and durable metadata-only state.
- `rynmesh/llm_package/routes.py`: node-local publish/discover/order APIs and the
  encrypted peer data path.
- `rynmesh/llm_package/cli.py`: one entry point for setup/import/connect,
  lifecycle, publishing, orders, and inspection.
- `deploy/llm-e2e/`: isolated Registry, Provider, Consumer, and optional real
  OpenAI-compatible runtime topology.

## Three package modes

1. `managed`: the wizard detects hardware, selects a conservative GGUF/runtime
   profile, downloads to node-owned directories, verifies hashes, starts the
   runtime, and performs a real completion self-test.
2. `import_gguf`: validates a user GGUF in place, records only a local path in
   the private config, launches it read-only, and publishes only an alias and
   content fingerprint. The fingerprint detects a local model swap; it does not
   prove intellectual-property ownership, origin, quality, or absolute
   authenticity. Removal never deletes the imported file.
3. `openai_compatible` (and `ollama`): validates a loopback endpoint by default,
   discovers models/capabilities, and sends a real test request. API keys are
   named by environment variable and are never copied into normal config/logs.

## Runtime backends

Managed and GGUF-import modes select a runtime backend (`lifecycle.select_runtime`,
`runtime: "auto" | "native" | "docker"`): `native_llama_cpp`
(`rynmesh/llm_package/runtime_native.py`) is the default and resolves a bundled or
downloaded `llama-server` binary with no container engine required; `docker_llama_cpp`
(`rynmesh/llm_package/runtime_docker.py`) is an opt-in backend for server operators who
prefer container isolation. Managed installs additionally pick one of three pinned
catalog profiles (`light`, `balanced`, `quality`, `rynmesh/llm_package/catalog.py`),
each with a conservative estimated-memory/disk footprint used by
`GET /api/local/llm/hardware` to recommend a profile for the detected hardware. See
`docs/ISSUE_34_NATIVE_RUNTIME_WORK_PLAN.md` for the native-runtime delivery plan.

## Order flow

1. Provider publishes a signed `JobCapacityRecord` containing only the public
   service manifest, health, capacity, queue policy, benchmark, price, licence,
   privacy, and risk labels. A configured Provider refreshes this short-lived
   discovery record every 30 seconds; the WebApp also exposes a manual retry.
2. Consumer discovers that record through Rynmesh and resolves the Provider Ryn
   endpoint. It never uses the model runtime URL.
3. Consumer creates a stable task ID and idempotency key, freezes a worst-case
   amount in Task Balance, signs and encrypts the prompt to the Provider's node
   messaging key, then POSTs ciphertext to the Provider peer API through the
   active `Transport`. Task creation, settlement acknowledgement, and
   cancellation all follow this route; none bypasses transport selection with
   a direct `urllib` call.
4. Provider verifies the node signature, enforces recipient/service/health/
   capacity/expiry/idempotency, records metadata-only lifecycle transitions,
   decrypts in memory, calls the adapter, encrypts the response to Consumer,
   and clears controllable temporary state.
5. Consumer decrypts, verifies metering and amount, settles its hold exactly
   once, then sends an idempotent body-free settlement acknowledgement so the
   Provider records the development earning exactly once.
6. Failure, timeout, or cancellation releases the hold. Duplicate requests and
   callbacks return the stored ciphertext/record without re-running inference
   or charging twice.

States are `created`, `accepted`, `running`, then `succeeded`, `failed`,
`timed_out`, or `cancelled`. Capacity exhaustion is an explicit rejection and
never silently drops a request.

## Logging and cleanup

Normal logs and JSON records contain IDs, aliases, states, timing, token counts,
amounts, and error codes only. Prompt/response logging requires the explicit
`RYNMESH_LLM_DEBUG_BODIES=1` opt-in and emits a startup warning. No task body is
written to temporary files. Terminal cleanup removes request keys, partial
files, and controllable adapter caches. Python cannot guarantee erasure of every
in-memory copy; the documentation and UI must not claim otherwise.

## Background worker lifecycle

The LLM package registers two supervised workers with the node's
`BackgroundWorkerRegistry` while installing its routes:

- `llm.relay-poll` processes signaling, settlement, cancellation, and optional
  encrypted Relay work. It polls every second while active and backs off to 10
  seconds while idle or 30 seconds after repeated failures.
- `llm.publish-refresh` refreshes an enabled Provider's short-lived discovery
  record every 30 seconds, with bounded failure backoff.

The node lifespan starts and stops the registry; the LLM package does not own
detached asyncio tasks. Synchronous worker calls run in a thread, failures are
isolated per worker, and shutdown cancels and awaits all registered workers.
Registry status contains scheduling metadata and exception classes only. It
never stores worker arguments, results, prompts, outputs, keys, URLs, or private
paths. Existing local service status fields remain `publication_error` and
`relay_poll_error`.

## Change plan

The implementation is additive: preserve existing content, recommendation,
messaging, work-order, relay, and Credits behavior; add the LLM package, private
peer task routes, Task Balance, CLI, Docker test topology, focused tests, and
documentation. Existing work orders continue to support non-sensitive jobs but
must not be used for LLM prompt/output bodies.
