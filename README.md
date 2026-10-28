# Rynmesh

[![CI](https://github.com/yeogirlyun/rynmesh/actions/workflows/ci.yml/badge.svg)](https://github.com/yeogirlyun/rynmesh/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![GitHub release](https://img.shields.io/github/v/release/yeogirlyun/rynmesh)](https://github.com/yeogirlyun/rynmesh/releases)

Rynmesh is an open-source, local-first personal AI assistant and verifiable content mesh. A Ryn node can discover and rank public content, learn from local feedback, publish signed content, discover peers, and exchange content with provenance and safety receipts.

Visit [rynmesh.ai](https://www.rynmesh.ai) for the product overview, verified
downloads, current milestone status, and the live
[contribution center](https://www.rynmesh.ai/contribute/).

The node, web interface, recommendation agent, peer protocol, registry, and command-line tools are all included under the MIT license. No account, API key, proprietary service, or preference setup is required for the default experience.

## What works today

- Proactive recommendations from a built-in public catalog, including video, articles, research, podcasts, audiobooks, and visual content
- Local recommendation profiles that learn from More, Less, Hide, Open, topics, platforms, and written direction
- A self-hosted web interface served by the node daemon
- Signed content manifests, provenance chains, and safety receipts
- Direct peer HTTP transport with registry-assisted discovery and NAT-safe relay jobs
- Direct ICE/UDP file transfer with automatic ordinary-peer transit fallback; TURN is not used
- Content publishing for video, images, audio, documents, slides, datasets, and other files
- MCP tools for Codex-, Claude-, and other MCP-compatible AI operators
- Non-transferable Rynmesh Credits for distribution reputation

Rynmesh is alpha software. APIs and storage formats may change before 1.0.

## Current boundaries

- Desktop installers are currently available for macOS on Apple Silicon and
  Intel. Windows and Linux packaging is planned.
- macOS community builds are not yet Apple-notarized.
- Public-source recommendations work without peers, accounts, preferences, or
  a model. AI-generated briefings and Search & Ask require a reachable local
  Ollama model or explicit opt-in to a configured cloud provider.
- Friend invitations, friend-attributed recommendations, multi-user egress,
  and budgeted agent-to-agent services are planned milestones.
- The safety scanner is an alpha implementation; operating an unrestricted
  network of untrusted peers requires the additional hardening described in
  [Product milestones](docs/PRODUCT_MILESTONES.md).

For an implementation inventory and clearly separated contribution areas, see
[Product milestones](docs/PRODUCT_MILESTONES.md). Work that is ready to be
claimed is tracked in [GitHub Issues](https://github.com/yeogirlyun/rynmesh/issues),
not inferred from aspirational design documents. The current accepted backlog
is grouped in the [P1 hardening milestone](https://github.com/yeogirlyun/rynmesh/milestone/1).

## Install from GitHub

### macOS desktop

Download the Apple Silicon (`aarch64`) or Intel (`x86_64`) DMG from the
[latest release](https://github.com/yeogirlyun/rynmesh/releases/latest), open it, and drag **Ryn**
to Applications. The app contains its own node daemon and web interface; Python, Node.js, Ollama,
an account, and source configuration are not required. Because community builds are not Apple
notarized yet, the first launch may require Control-clicking Ryn and choosing **Open**.

### Python package

Python 3.10 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
curl -fsSL https://github.com/yeogirlyun/rynmesh/releases/latest/download/install.sh | sh
```

Or install the current wheel into an existing Python environment:

```bash
python -m pip install https://github.com/yeogirlyun/rynmesh/releases/download/v0.6.2/rynmesh-0.6.2-py3-none-any.whl
rynmesh-peer
```

Open [http://127.0.0.1:8791](http://127.0.0.1:8791). Ryn starts its recommendation agent automatically. Adding a YouTube channel, subreddit, or RSS feed is optional.

Versioned wheels, checksums, installer scripts, and source archives are available from [GitHub Releases](https://github.com/yeogirlyun/rynmesh/releases). Release wheels include the built web interface; editable source installs use the Vite development server described below.

## Develop from source

```bash
git clone https://github.com/yeogirlyun/rynmesh.git
cd rynmesh
./scripts/dev_setup.sh
```

For hot-reloading development, run the node and web app in separate terminals:

```bash
./.venv/bin/rynmesh-peer
```

```bash
cd webapp
npm run dev
```

The local API runs on port `8791`; Vite serves the development UI on port `5173`.

## Run a registry

Peer discovery can use a registry, but a registry is not required for the local personal-assistant experience.

```bash
export RYNMESH_REGISTRY_HOST="0.0.0.0"
export RYNMESH_REGISTRY_PORT="8790"
export RYNMESH_REGISTRY_DIR="$HOME/.rynmesh/registry"
rynmesh-registry
```

The registry stores signed peer records, work-order mailbox messages, and optional relay blobs. Nodes verify signatures and content hashes locally.

## Three-node P2P peer transit

`rynmesh-transit` sends directly over ICE/UDP when possible and can use another
ordinary Rynmesh peer as a single encrypted transit hop when the direct route is
unavailable or degraded. Both legs are host/server-reflexive P2P connections;
the registry carries signed signaling only, and TURN candidates fail closed.

```bash
# Peer 3
rynmesh-transit worker --role target --network-id my-network
# Peer 2
rynmesh-transit worker --role transit --network-id my-network
# Peer 1: direct first, peer 2 on hard failure
rynmesh-transit send-file-adaptive artifact.bin \
  --target-peer "<peer-3-id>" --relay-peer "<peer-2-id>" \
  --network-id my-network
```

See [the design and acceptance contract](docs/P2P_PEER_TRANSIT.md) and
[operator runbook](docs/P2P_PEER_TRANSIT_RUNBOOK.md).

## MCP server

```bash
rynmesh-mcp
```

See [Architecture](docs/ARCHITECTURE.md), [Product milestones](docs/PRODUCT_MILESTONES.md), and [Contributing](CONTRIBUTING.md) for deeper project context.

## Optional local LLM provider packages

The desktop node and recommendation assistant do not require Docker or a local
model. Operators who choose to provide private inference can connect an existing
loopback OpenAI-compatible or Ollama service, or let Rynmesh manage one: managed
mode downloads a verified GGUF model and runs it with the bundled llama.cpp
runtime built into the desktop app, or a managed download of the pinned release
on a `pip`-installed node. Docker is an opt-in runtime for server operators who
prefer container isolation, selected with `--runtime docker`:

```bash
rynmesh-llm detect
rynmesh-llm setup --mode openai-compatible --base-url http://127.0.0.1:8080
# Managed local model, bundled/downloaded native runtime (default):
rynmesh-llm setup --mode managed --profile balanced --yes
# Managed local model, opt-in Docker runtime (server nodes):
rynmesh-llm setup --mode managed --runtime docker --yes
```

Provider/Consumer task bodies travel as signed end-to-end ciphertext directly
between Ryn nodes, with a dedicated ciphertext-only relay fallback. The registry
receives discovery and body-free coordination only. Rynmesh Credits remain
non-transferable reputation; development Task Balance is a separate simulated
ledger and is not real money or a production payment system.

Run the isolated two-node automated demonstration with:

```bash
python scripts/llm_e2e.py run
python scripts/llm_e2e.py down
```

See [Local LLM runbook](docs/LOCAL_LLM_RUNBOOK.md),
[design and boundaries](docs/LOCAL_LLM_SERVICE_MVP.md), and
[P0 evidence](docs/LOCAL_LLM_P0_EVIDENCE.md). Developers and AI agents should
start with the [current development handoff](docs/LOCAL_LLM_DEVELOPMENT_STATUS.md)
before continuing this feature. The user-facing catalog, typed service routes,
local conversation storage, and extension rules are documented in the
[Services UI architecture](docs/SERVICES_UI_ARCHITECTURE.md).

## Verify a checkout

```bash
python -m pytest -q
python -m ruff check rynmesh tests qa
cd webapp && npm ci && npm run build
```

CI also verifies that a packaged node serves its own UI without a development server.

## Security and privacy

Node identity, preferences, feedback, and fetched content are stored locally under `RYNMESH_HOME` (by default beneath `~/.rynmesh`). Network access is used for sources or peers the node contacts; model integrations are optional.

Please report vulnerabilities privately as described in [SECURITY.md](SECURITY.md). Do not include secrets or personal data in public issues.

## Community

- Start with the [First Contributor Starter Guide](docs/FIRST_CONTRIBUTOR_GUIDE.md).
- Read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a pull request.
- Choose accepted work from the
  [contribution center](https://www.rynmesh.ai/contribute/) and comment
  `/claim` on the linked GitHub issue before implementation.
- Follow the [Code of Conduct](CODE_OF_CONDUCT.md).
- Use GitHub Issues for reproducible bugs and focused feature requests.
- Use GitHub Discussions for questions and broader design ideas.

## License

Rynmesh is licensed under the [MIT License](LICENSE).
