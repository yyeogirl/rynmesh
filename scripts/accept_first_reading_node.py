"""Run a fresh node with desktop public sources and no seeded content or model.

This serves the production web build over real TCP. It does not prove packaged
desktop behavior. Public registration is disabled to isolate acceptance data.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from accept_local_search import configure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    home = args.home.resolve()
    if not home.name.startswith("rynmesh-first-reading-acceptance-"):
        raise SystemExit("Use a dedicated first-reading acceptance directory.")
    marker = home / ".first-reading-fixture.json"
    if args.resume:
        record = json.loads(marker.read_text(encoding="utf-8"))
        if record.get("kind") != "ryn.first-reading-acceptance.v1":
            raise SystemExit("This directory is not a first-reading fixture.")
    elif home.exists():
        raise SystemExit("Fresh acceptance requires a new directory.")
    home.mkdir(parents=True, exist_ok=True)
    configure(home, 18930)
    os.environ.update(RYNMESH_DESKTOP_MODE="1", RYNMESH_DEFAULT_DISCOVERY="1",
        RYNMESH_PEER_HOST="127.0.0.1", RYNMESH_PEER_PORT="18930",
        RYNMESH_PEER_ENDPOINT="http://127.0.0.1:18930", RYNMESH_LLM_HOME=str(home / "llm"))
    from rynmesh.atomic_io import atomic_write_json
    from rynmesh.peer_http import main as serve
    atomic_write_json(marker, {"kind": "ryn.first-reading-acceptance.v1", "pid": os.getpid(),
        "started_at": datetime.now(UTC).isoformat(), "port": 18930})
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
