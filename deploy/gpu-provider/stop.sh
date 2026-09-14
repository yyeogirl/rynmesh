#!/usr/bin/env bash
set -euo pipefail

BASE="${RYNMESH_PROVIDER_BASE:-/opt/rynmesh}"
for name in provider-node provider-gateway llm-runtime; do
  pid_file="$BASE/$name.pid"
  if [[ -s "$pid_file" ]]; then
    pid="$(<"$pid_file")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid"
      echo "$name stopped pid=$pid"
    fi
    rm -f "$pid_file"
  fi
done
