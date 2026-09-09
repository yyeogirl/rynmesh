# GPU Provider deployment runbook

Documentation set:

- Requirements: [`V100_GPU_PROVIDER_REQUIREMENTS.md`](V100_GPU_PROVIDER_REQUIREMENTS.md)
- Development: [`V100_GPU_PROVIDER_DEVELOPMENT.md`](V100_GPU_PROVIDER_DEVELOPMENT.md)
- Test report: [`V100_GPU_PROVIDER_TEST_REPORT.md`](V100_GPU_PROVIDER_TEST_REPORT.md)
- Acceptance: [`V100_GPU_PROVIDER_ACCEPTANCE.md`](V100_GPU_PROVIDER_ACCEPTANCE.md)

This profile turns a Linux NVIDIA host into a Rynmesh Provider while keeping
the model runtimes private:

- Public TCP `3000`: keyed Rynmesh registry/rendezvous, signed/encrypted peer
  tasks under `/peer`, plus token-protected video jobs under `/video`.
- Loopback TCP `8791`: Provider Ryn node.
- Loopback TCP `8080`: OpenAI-compatible Qwen runtime.
- Public UDP `3000`: ICE/STUN hole-punched encrypted LLM task bodies. The public
  registry carries signed signaling only and payload relay is disabled.

The deployment defaults target a V100 32GB host. Qwen3-14B is loaded in NF4
4-bit through Transformers/bitsandbytes and Wan2.1-T2V-1.3B uses Diffusers
FP16 with CPU offload. HunyuanVideo-1.5 is not selected on V100: its single
DiT checkpoint is approximately 33.3GB before VAE, encoders, activations, and
runtime overhead, and its optimized BF16/FP8 paths target newer GPUs.

Required private files, never committed:

```text
/opt/rynmesh/config/network_key
/opt/rynmesh/config/video_api_token
/opt/rynmesh/config/llm/packages/qwen3-14b/manifest.json
```

Start and stop the three processes:

```bash
bash /opt/rynmesh/app/deploy/gpu-provider/start.sh
bash /opt/rynmesh/app/deploy/gpu-provider/stop.sh
```

For restart-safe operation in the current container, install the provided
Supervisor configuration instead of relying on PID files:

```bash
install -m 0755 deploy/gpu-provider/supervisor-wrapper.sh \
  /opt/rynmesh/app/deploy/gpu-provider/supervisor-wrapper.sh
install -m 0644 deploy/gpu-provider/rynmesh-gpu-provider.conf \
  /etc/supervisor/conf.d/rynmesh-gpu-provider.conf
supervisorctl reread
supervisorctl update
```

Supervisor starts the gateway, Qwen runtime, and Provider node after every
container restart and automatically restarts a failed process. Secrets remain
in `/opt/rynmesh/config` and are read by the wrapper at process start.

Set the advertised endpoint before starting, for example:

```bash
export RYNMESH_PEER_ENDPOINT=http://PUBLIC_IP:3000/peer
```

The cloud firewall/security group must allow both TCP `3000` and UDP `3000`.
The Provider binds ICE to UDP `3000`; the Consumer remains on an ephemeral UDP
port and only needs normal outbound UDP/stateful-return access.

Public liveness and authenticated video health:

```bash
curl http://PUBLIC_IP:3000/health
curl -H "Authorization: Bearer $RYNMESH_VIDEO_API_TOKEN" \
  http://PUBLIC_IP:3000/video/health
```

The Consumer CLI submits, polls, and downloads a generated MP4:

```bash
rynmesh-video --base-url http://PUBLIC_IP:3000/video generate \
  --prompt "A blue glass cube slowly rotates in a dark studio" \
  --output acceptance.mp4
```
