#!/usr/bin/env bash
set -euo pipefail

BASE="${RYNMESH_PROVIDER_BASE:-/opt/rynmesh}"
VENV="$BASE/venv"
CONFIG="$BASE/config"
DATA="$BASE/data"
export PYTHONUNBUFFERED=1

case "${1:-}" in
  llm-runtime)
    export RYNMESH_TRANSFORMERS_MODEL_PATH="${RYNMESH_TRANSFORMERS_MODEL_PATH:-/model/ModelScope/Qwen/Qwen3-14B}"
    export RYNMESH_TRANSFORMERS_MODEL_ID="${RYNMESH_TRANSFORMERS_MODEL_ID:-Qwen3-14B-Q4}"
    export RYNMESH_TRANSFORMERS_HOST=127.0.0.1
    export RYNMESH_TRANSFORMERS_PORT=8080
    exec "$VENV/bin/rynmesh-transformers-llm"
    ;;
  gateway)
    export RYNMESH_NETWORK_KEY="$(<"$CONFIG/network_key")"
    export RYNMESH_VIDEO_API_TOKEN="$(<"$CONFIG/video_api_token")"
    export RYNMESH_REGISTRY_HOST=0.0.0.0
    export RYNMESH_REGISTRY_PORT=3000
    export RYNMESH_REGISTRY_DIR="$DATA/registry"
    export RYNMESH_HOME="$DATA/gateway"
    export RYNMESH_PROVIDER_HOST=0.0.0.0
    export RYNMESH_PROVIDER_PORT=3000
    export RYNMESH_VIDEO_MODEL_PATH="${RYNMESH_VIDEO_MODEL_PATH:-/model/ModelScope/Wan-AI/Wan2.1-T2V-1.3B-Diffusers}"
    export RYNMESH_VIDEO_OUTPUT_DIR="$DATA/videos"
    exec "$VENV/bin/rynmesh-provider-gateway"
    ;;
  provider)
    export RYNMESH_NETWORK_KEY="$(<"$CONFIG/network_key")"
    export RYNMESH_REGISTRY_URL=http://127.0.0.1:3000
    export RYNMESH_NETWORK_ID="${RYNMESH_NETWORK_ID:-rynmesh-gpu-e2e}"
    export RYNMESH_NODE_NAME="${RYNMESH_NODE_NAME:-v100-provider}"
    export RYNMESH_HOME="$DATA/provider"
    export RYNMESH_PEER_HOST=127.0.0.1
    export RYNMESH_PEER_PORT=8791
    export RYNMESH_PEER_ENDPOINT="${RYNMESH_PEER_ENDPOINT:-http://117.50.189.73:3000/peer}"
    export RYNMESH_LLM_SERVICE_MANIFEST="$CONFIG/llm/packages/qwen3-14b/manifest.json"
    export RYNMESH_AUTO_REGISTER=1
    export RYNMESH_LLM_TRANSPORT=p2p
    export RYNMESH_LLM_FORCE_RELAY=0
    export RYNMESH_P2P_STUN="${RYNMESH_P2P_STUN:-stun.chat.bilibili.com:3478}"
    export RYNMESH_P2P_BIND_PORT="${RYNMESH_P2P_BIND_PORT:-3000}"
    export RYNMESH_P2P_REQUIRE_PUBLIC=1
    export RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC=1
    unset RYNMESH_LLM_RELAY_URL
    exec "$VENV/bin/rynmesh-peer"
    ;;
  *)
    echo "usage: $0 {llm-runtime|gateway|provider}" >&2
    exit 2
    ;;
esac
