# Windows public-E2E Consumer

This bootstrap packages the current checkout as a no-console Windows Consumer.
It reads `rynmesh-consumer.json` next to the executable, stores identity/order
state under `%LOCALAPPDATA%\RynmeshPublicConsumer`, forces strict ICE/UDP P2P
with distinct public egress validation and no task-relay fallback, and opens
the local Services page. The private network key stays in the
separate ignored JSON configuration and is never embedded in the executable.

The Consumer never receives the Provider model URL. Only the Registry signaling
surface is reachable through the public reverse proxy. It carries discovery,
ephemeral ICE candidates, signed states, and body-free settlement metadata.
Prompt and response ciphertext travel over the nominated node-to-node UDP pair.

When `frpc.exe` and `frpc.toml` are present beside the executable, the bootstrap
starts the signaling visitor tunnel without a console window before starting the
node. The visitor binds Registry to loopback only; no task relay is configured.
