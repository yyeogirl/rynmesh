# Real local LLM validation record

Date: 2026-08-24 (Asia/Hong_Kong). All prompts were fixed synthetic validation
text. Paths, filenames, keys, and private model details were not recorded.

## Existing OpenAI-compatible connection

- Health/model discovery: passed on loopback; the live capability probe reported
  OpenAI-compatible chat completions and streaming support.
- Backend: owner-managed llama.cpp, real GGUF, 4B-class local model.
- Completion: `RYNMESH SELF TEST OK`.
- Output SHA-256: `f095e157e275d1e11b9db2458041a32a3ff0ce96ab89b25bfa45166b6b7f8bb9`.
- Usage: 23 input tokens, 7 output tokens; 21,217 ms.

## Managed one-click installation

- Hardware: Windows/AMD64, 8 logical CPUs, 32,653 MiB RAM, NVIDIA Quadro P2200
  5,120 MiB, Docker available. The safe managed runtime used CPU.
- Model: official Qwen2.5 0.5B Instruct Q4_K_M GGUF, Apache-2.0.
- Source LFS SHA-256:
  `74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db`.
- Runtime image digest:
  `sha256:100de626bdc5b7df898c12561eefaf557019d2746d5fc8d3f4d7fd24e15ad384`.
- Real self-test: passed, 40 input tokens, 8 output tokens, 171 ms.
- Stop/start/update/restart: passed. Update preserved the model.
- Environment uninstall preserved the model; the separate explicitly confirmed
  model deletion removed it.

## Read-only GGUF import

- An owner-supplied GGUF was validated and fingerprinted in place.
- It was mounted read-only into llama.cpp and completed a real 64-token request.
- Default uninstall preserved it. An explicit Rynmesh deletion request was
  refused because the file was user-owned; existence was verified afterward.

## Two-node real inference

- Topology: isolated Provider and Consumer Ryn containers with separate
  identities, ports, configuration, and volumes; owner-managed llama.cpp was an
  additional Provider backend.
- Discovery count: 1 sanitized service.
- Task state: succeeded through Consumer Ryn -> Provider Ryn; no Consumer call
  to the model port.
- Usage: 26 input tokens, 10 output tokens, 5,920 ms (Chrome WebApp acceptance).
- Development Task Balance amount: 0.001, held amount returned to zero, Provider
  earning recorded idempotently.
- Desensitized preview: `RYNMESH_TWO_NODE_E2E_OK`.
- Task ID: `task_d57a9a97fdba45f0866bac176071fb0e`.
- Browser evidence: `01-provider-published.png`, `02-consumer-discovered.png`,
  and `03-consumer-result-settled.png` in the Codex task visualization folder.
- Provider history: `created -> accepted -> running -> succeeded`; Consumer
  history: `created -> succeeded`. Logs from Registry, Relay, Provider, and
  Consumer contained no validation prompt/output text.

## Public-internet Windows Consumer

### Historical encrypted-relay run (not P2P-direct acceptance)

- A separate remote Windows machine ran the packaged Consumer from its local
  Desktop and opened only its loopback Services page.
- Both machines initiated TLS-protected FRP STCP connections to the public
  rendezvous server. The remote visitor bound Registry and Relay to loopback;
  neither Ryn service nor the model had a public listening port.
- The remote WebApp discovered `host-private-local-model` through Rynmesh and
  submitted a forced-relay encrypted order. Registry metadata showed only one
  `.relay` work order with `encrypted_task_ref` and one body-free `.settlement`
  work order.
- Task ID: `task_ab056eae01a94d018f5181b5e8de61de`.
- Provider history: `created -> accepted -> running -> succeeded`.
- Usage: 26 input tokens, 36 output tokens, 7,009 ms; development-only Task
  Balance amount 0.001. Exactly one Provider earning event was present and held
  balance returned to zero.
- The Relay's stored files and Registry/Relay/Provider logs contained neither
  the fixed prompt nor the returned text. Provider temporary ciphertext files
  were absent after completion.
- After acceptance, the owner-managed llama.cpp listener was changed from
  `0.0.0.0:8080` to `127.0.0.1:8080`. Docker Desktop's host gateway still
  allowed the Provider adapter to reach it; the remote Consumer never received
  or called that runtime URL.

This run proves ciphertext relay behavior and privacy boundaries, but it does
not satisfy the later owner requirement for NAT-hole-punched node-to-node
payload transport and must not be presented as such.

### Strict public P2P attempt — pending

- Both packaged Windows Consumer and host Provider forced
  `RYNMESH_LLM_TRANSPORT=p2p`, `RYNMESH_P2P_REQUIRE_PUBLIC=1`, and removed the
  task-relay URL.
- Signaling contained only server-reflexive STUN candidates. Host/private
  candidates were rejected by policy and the Provider answer explicitly set
  `relay_allowed=false`.
- The two machines' STUN mappings reported the same public egress with
  different UDP ports. No nominated UDP pair formed before timeout, consistent
  with the current NAT/hairpin behavior.
- The strict order failed closed. Relay persisted-file count was unchanged and
  no new real inference reached the Provider.
- A different public egress is required for a conclusive strict-P2P acceptance
  run. This item remains pending; it is not a successful validation record.

On 2026-08-25 the newly packaged Consumer was started on the remote Windows
machine and opened `127.0.0.1:8792`. It discovered both available equal-alias
services. The first selection exposed that the UI labels did not identify the
node/package and reached the Docker Provider. A second attempt explicitly
selected the host-native package; Registry evidence confirmed the host-native
peer accepted work order `wo_06f00358b3b74b1dab043be63aef0fb4` for Consumer
task `task_e78b05d691f841179c5a5f7f734e9ef9`.

That exact attempt still reported the same STUN public mapping
`98.158.108.218` on both sides (different private addresses and UDP ports), so
no candidate pair was nominated. The Provider produced accepted then failed
signaling states, never created a real inference order, and the Consumer hold
returned to zero. This evidence both confirms exact Provider routing and
confirms that the current shared-VPN topology cannot satisfy different-egress
acceptance. The latest source now detects this before the full ICE timeout and
reports `p2p_distinct_public_egress_required`.

The resulting package `remote-consumer-20260825-142504.zip` was copied to and
started on the same remote Windows machine. It automatically populated
`rynmesh-llm-e2e`, displayed the two Provider/node/package choices distinctly,
and submitted strict task `task_8478211f1dc144e4aea515592d9c1294` to the
host-native service. The task failed in about ten seconds with the user-facing
instruction to disconnect the shared VPN or move one node to another network;
the DEV balance remained `100.000` and held balance returned to `0.000`. This
validates the new fail-fast UX, but remains a negative topology test rather than
a successful public P2P run.

### 2026-08-25 review checkpoint

- Focused Python tests: 40 passed.
- Webapp tests: 24 passed; TypeScript/Vite build and typecheck passed.
- Windows full Python suite: 488 passed, 13 platform-specific failures, 3
  skipped.
- Deterministic direct and forced-relay Docker runs passed from clean source;
  the relay persisted ciphertext and contained no fixed plaintext. A host real
  model run returned a valid completion, settled once and left held balance at
  zero.
- Asynchronous real submission returned a task ID in 16 ms and later succeeded.
  A separate live cancellation reached both nodes in 16 ms, ended both states
  as cancelled and released the hold.
- Remote execution is proven, but both nodes currently use the same company-VPN
  public exit. A different approved public egress and successful nominated
  public-pair evidence remain pending.

Reproduce with the commands in `docs/LOCAL_LLM_RUNBOOK.md`. The deterministic
test profile is never presented as real inference evidence.
