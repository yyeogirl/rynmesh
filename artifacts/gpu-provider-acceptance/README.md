# Rynmesh V100 Provider acceptance evidence

Formal documentation:

- [`../../docs/V100_GPU_PROVIDER_REQUIREMENTS.md`](../../docs/V100_GPU_PROVIDER_REQUIREMENTS.md)
- [`../../docs/V100_GPU_PROVIDER_DEVELOPMENT.md`](../../docs/V100_GPU_PROVIDER_DEVELOPMENT.md)
- [`../../docs/V100_GPU_PROVIDER_TEST_REPORT.md`](../../docs/V100_GPU_PROVIDER_TEST_REPORT.md)
- [`../../docs/V100_GPU_PROVIDER_ACCEPTANCE.md`](../../docs/V100_GPU_PROVIDER_ACCEPTANCE.md)

Status on 2026-09-07 (Asia/Hong_Kong): **all acceptance checks pass**. This
includes deployment, public node access, Qwen3-14B, the LLM package, Wan2.1
video generation, and strict direct ICE/UDP communication between the Windows
Consumer and V100 Provider.

The cloud firewall now allows both TCP/3000 and UDP/3000. Strict P2P task
`task_90dd301a0b26488a9b52913e53bb20f1` returned a real Qwen response over a
nominated public candidate pair with `transport=ice_udp_direct` and
`relay_used=false` in 948 ms.

Cold-restart retest on 2026-09-08 also passes. The three server processes are
now managed by Supervisor with automatic start/restart. After migration, a
forced Provider process termination produced a new running PID, strict public
P2P succeeded again, and two new Wan2.1 MP4 jobs were generated and downloaded.
See `cold-restart-retest-2026-09-08.json`.

Evidence:

- `ui-video-proof.png`: evidence-rich video job inspector with the real generated frame, job parameters, CUDA execution timeline, Windows download, and SHA-256 match.
- `ui-overview-proof.png`: end-to-end Consumer/Provider topology, public services, GPU and model runtimes.
- `ui-llm-proof.png`: Qwen conversation UI plus task receipt, tokens, transport and runtime attestation.
- `ui-p2p-proof.png`: both public candidates, nominated pair, raw transport evidence and real Qwen response.
- `ui-firewall-proof.png`: the cloud-console rule alongside runtime proof obtained after the rule change.
- `screenshot-overview.png`: server node, public endpoint, V100 runtime.
- `screenshot-llm.png`: real Qwen3-14B response from the Windows Consumer.
- `screenshot-video.png`: real Wan2.1 generation and output integrity.
- `screenshot-p2p.png`: both public ICE candidates, nominated pairs, and real model result.
- `screenshot-firewall.png`: enabled inbound UDP/3000 cloud-firewall rule.
- `final-public-video.mp4`: generated 480×272, 33-frame, 4.125-second MP4.
- `p2p-final-result.json`: sanitized machine-readable P2P acceptance evidence.
- Other `*.json`: machine-readable health, job, mesh, and task evidence.

Final automated verification: 77 tests passed and Ruff reported no findings.
