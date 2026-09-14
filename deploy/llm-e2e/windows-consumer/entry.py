"""No-console Windows bootstrap for the public two-node acceptance Consumer."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _base_dir() -> Path:
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def _load_config(base: Path) -> None:
    config_path = base / "rynmesh-consumer.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("rynmesh-consumer.json must contain an object")
    allowed = {
        "RYNMESH_REGISTRY_URL",
        "RYNMESH_LLM_RELAY_URL",
        "RYNMESH_NETWORK_KEY",
        "RYNMESH_NETWORK_ID",
        "RYNMESH_NODE_NAME",
        "RYNMESH_PEER_HOST",
        "RYNMESH_PEER_PORT",
        "RYNMESH_PEER_ENDPOINT",
        "RYNMESH_LLM_FORCE_RELAY",
        "RYNMESH_LLM_TRANSPORT",
        "RYNMESH_P2P_STUN",
        "RYNMESH_AUTO_REGISTER",
        "RYNMESH_MODEL_PROVIDER",
        "RYNMESH_OPEN_BROWSER",
        "RYNMESH_HOME_NAME",
    }
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(f"unsupported configuration keys: {sorted(unknown)}")
    for key, value in config.items():
        os.environ[key] = str(value)


def _open_browser(port: str) -> None:
    time.sleep(2.5)
    webbrowser.open(f"http://127.0.0.1:{port}/services")


def _start_tunnel(base: Path, home: Path) -> subprocess.Popen[bytes] | None:
    """Start the control-plane signaling tunnel, never an LLM payload relay."""
    frpc = base / "frpc.exe"
    config = base / "frpc.toml"
    if not frpc.is_file() or not config.is_file():
        return None
    tunnel_log = (home / "frpc.log").open("ab", buffering=0)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [str(frpc), "-c", str(config)],
        cwd=str(base),
        stdin=subprocess.DEVNULL,
        stdout=tunnel_log,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    (home / "frpc.pid").write_text(str(process.pid), encoding="ascii")
    time.sleep(1.5)
    if process.poll() is not None:
        raise RuntimeError("the public tunnel exited during startup; see frpc.log")
    return process


def main() -> int:
    base = _base_dir()
    _load_config(base)
    # This acceptance binary is deliberately fail-closed: task bodies may only
    # use the nominated ICE/UDP peer path.  The adjacent FRP visitor is solely
    # for registry signaling and can never become a payload fallback.
    os.environ["RYNMESH_LLM_TRANSPORT"] = "p2p"
    os.environ["RYNMESH_LLM_FORCE_RELAY"] = "0"
    # Same-gateway peers must be able to nominate either a directly routable
    # host candidate or the gateway's server-reflexive hairpin candidate.  ICE
    # still forbids TURN/relay candidates; public-address diversity is not a
    # prerequisite for a direct encrypted UDP path.
    os.environ["RYNMESH_P2P_REQUIRE_PUBLIC"] = "0"
    os.environ["RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC"] = "0"
    os.environ.pop("RYNMESH_LLM_RELAY_URL", None)
    local_app_data = Path(os.environ.get("LOCALAPPDATA", base))
    home_name = os.environ.get("RYNMESH_HOME_NAME", "RynmeshPublicConsumer")
    if not home_name.replace("-", "").replace("_", "").isalnum():
        raise ValueError("RYNMESH_HOME_NAME must contain only letters, digits, '-' or '_'")
    home = local_app_data / home_name
    home.mkdir(parents=True, exist_ok=True)
    log = (home / "consumer.log").open("a", encoding="utf-8", buffering=1)
    sys.stdout = log
    sys.stderr = log
    os.environ["RYNMESH_HOME"] = str(home)
    port = os.environ.get("RYNMESH_PEER_PORT", "8791")
    (home / "consumer.pid").write_text(str(os.getpid()), encoding="ascii")
    _start_tunnel(base, home)
    if os.environ.get("RYNMESH_OPEN_BROWSER", "1").strip().lower() not in {"0", "false", "no"}:
        threading.Thread(target=_open_browser, args=(port,), daemon=True).start()

    from rynmesh.peer_http import main as peer_main

    return peer_main()


if __name__ == "__main__":
    raise SystemExit(main())
