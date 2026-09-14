"""Start the host-native strict-P2P Provider used by public E2E validation."""

from __future__ import annotations

import os
from pathlib import Path


def _load_private_environment(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    _load_private_environment(root / "deploy" / "llm-e2e" / "config" / "public-network.env")
    provider_home = Path(os.environ.get("LOCALAPPDATA", root)) / "RynmeshP2PProvider"
    provider_home.mkdir(parents=True, exist_ok=True)
    token_source = root / "deploy" / "llm-e2e" / "config" / "demo-control-token"
    token_target = provider_home / "control_token"
    if not token_target.exists():
        token_target.write_bytes(token_source.read_bytes())

    os.environ.update({
        "RYNMESH_REGISTRY_URL": "http://127.0.0.1:18890",
        "RYNMESH_NETWORK_ID": "rynmesh-llm-e2e",
        "RYNMESH_NODE_NAME": "p2p-host-real-provider",
        "RYNMESH_PEER_HOST": "127.0.0.1",
        "RYNMESH_PEER_PORT": "18894",
        "RYNMESH_PEER_ENDPOINT": "http://127.0.0.1:18894",
        "RYNMESH_HOME": str(provider_home),
        "RYNMESH_LLM_SERVICE_MANIFEST": str(
            root / "deploy" / "llm-e2e" / "config" / "host-native-real-manifest.json"
        ),
        "RYNMESH_AUTO_REGISTER": "1",
        "RYNMESH_LLM_TRANSPORT": "p2p",
        "RYNMESH_P2P_STUN": "stun.l.google.com:19302",
        "RYNMESH_P2P_REQUIRE_PUBLIC": "0",
        "RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC": "0",
        "RYNMESH_LLM_RELAY_URL": "",
    })
    from rynmesh.peer_http import main as peer_main

    return peer_main()


if __name__ == "__main__":
    raise SystemExit(main())
