#!/usr/bin/env bash
set -euo pipefail

BASE="${RYNMESH_PROVIDER_BASE:-/opt/rynmesh}"
VENV="$BASE/venv"
CONFIG="$BASE/config"
LOGS="$BASE/logs"
DATA="$BASE/data"

mkdir -p "$LOGS" "$DATA/registry" "$DATA/provider" "$DATA/videos"
export RYNMESH_NETWORK_KEY="$(<"$CONFIG/network_key")"
export RYNMESH_VIDEO_API_TOKEN="$(<"$CONFIG/video_api_token")"

start_one() {
  local name="$1"
  shift
  local pid_file="$BASE/$name.pid"
  if [[ -s "$pid_file" ]] && kill -0 "$(<"$pid_file")" 2>/dev/null; then
    echo "$name already running pid=$(<"$pid_file")"
    return
  fi
  nohup "$@" >"$LOGS/$name.log" 2>&1 &
  echo "$!" >"$pid_file"
  echo "$name started pid=$!"
}

export RYNMESH_TRANSFORMERS_MODEL_PATH="${RYNMESH_TRANSFORMERS_MODEL_PATH:-/model/ModelScope/Qwen/Qwen3-14B}"
export RYNMESH_TRANSFORMERS_MODEL_ID="${RYNMESH_TRANSFORMERS_MODEL_ID:-Qwen3-14B-Q4}"
export RYNMESH_TRANSFORMERS_HOST=127.0.0.1
export RYNMESH_TRANSFORMERS_PORT=8080
start_one llm-runtime "$VENV/bin/rynmesh-transformers-llm"

export RYNMESH_REGISTRY_HOST=0.0.0.0
export RYNMESH_REGISTRY_PORT=3000
export RYNMESH_REGISTRY_DIR="$DATA/registry"
export RYNMESH_HOME="$DATA/gateway"
export RYNMESH_PROVIDER_HOST=0.0.0.0
export RYNMESH_PROVIDER_PORT=3000
export RYNMESH_VIDEO_MODEL_PATH="${RYNMESH_VIDEO_MODEL_PATH:-/model/ModelScope/Wan-AI/Wan2.1-T2V-1.3B-Diffusers}"
export RYNMESH_VIDEO_OUTPUT_DIR="$DATA/videos"
start_one provider-gateway "$VENV/bin/rynmesh-provider-gateway"

export RYNMESH_REGISTRY_URL=http://127.0.0.1:3000
export RYNMESH_NETWORK_ID="${RYNMESH_NETWORK_ID:-rynmesh-gpu-e2e}"
export RYNMESH_NODE_NAME="${RYNMESH_NODE_NAME:-v100-provider}"
export RYNMESH_HOME="$DATA/provider"
export RYNMESH_PEER_HOST=127.0.0.1
export RYNMESH_PEER_PORT=8791
export RYNMESH_PEER_ENDPOINT="${RYNMESH_PEER_ENDPOINT:-}"
if [[ -z "$RYNMESH_PEER_ENDPOINT" ]]; then
  echo "RYNMESH_PEER_ENDPOINT must be the public gateway URL ending in /peer" >&2
  exit 2
fi
export RYNMESH_LLM_SERVICE_MANIFEST="$CONFIG/llm/packages/qwen3-14b/manifest.json"
export RYNMESH_AUTO_REGISTER=1
export RYNMESH_LLM_TRANSPORT=p2p
export RYNMESH_LLM_FORCE_RELAY=0
export RYNMESH_P2P_STUN="${RYNMESH_P2P_STUN:-stun.chat.bilibili.com:3478}"
export RYNMESH_P2P_BIND_PORT="${RYNMESH_P2P_BIND_PORT:-3000}"
export RYNMESH_P2P_REQUIRE_PUBLIC=1
export RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC=1
unset RYNMESH_LLM_RELAY_URL
start_one provider-node "$VENV/bin/rynmesh-peer"
