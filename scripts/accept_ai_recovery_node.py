"""Serve an isolated real-model installation fixture; interact through its UI.

No model, runtime, progress or network response is mocked. Reuse the same home
with --resume after a deliberate node stop. This is not a desktop-shell test.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

from accept_local_search import configure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--legacy-fixture", action="store_true", help="Serve the explicit synthetic browser-history preparation UI.")
    args = parser.parse_args()
    home = args.home.resolve()
    if home.name != "rynmesh-ai-recovery-acceptance":
        raise SystemExit("Use the dedicated AI recovery acceptance directory.")
    marker = home / ".ai-recovery-fixture.json"
    if args.resume:
        record = json.loads(marker.read_text(encoding="utf-8"))
        if record.get("kind") != "ryn.ai-recovery-acceptance.v1":
            raise SystemExit("The directory is not this acceptance fixture.")
    elif home.exists():
        raise SystemExit("A fresh fixture must use a new directory.")
    home.mkdir(parents=True, exist_ok=True)
    configure(home, 18924)
    # Inherited development overrides must not bypass the actual installation.
    os.environ.pop("RYNMESH_LLAMA_SERVER", None)
    os.environ.pop("RYNMESH_LLAMA_DIR", None)
    os.environ["RYNMESH_LLM_HOME"] = str(home / "llm")
    from rynmesh.atomic_io import atomic_write_json
    from rynmesh.llm_package import model_download
    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    original_open = model_download._urlopen

    def observe_open(request, timeout=300):
        # Observe numeric range metadata without changing requests/responses or
        # retaining a redirect URL, authorization header or response body.
        def record(status, headers):
            raw_range = headers.get("Content-Range", "")
            raw_length = headers.get("Content-Length", "")
            event = {"at": datetime.now(UTC).isoformat(), "requested_range": request.get_header("Range"),
                     "status": status, "content_range": raw_range if re.fullmatch(r"bytes [0-9*/ -]+", raw_range) else None,
                     "content_length": int(raw_length) if raw_length.isdigit() else None}
            log = home / ".download-http.json"
            try:
                values = json.loads(log.read_text(encoding="utf-8")) if log.exists() else []
                atomic_write_json(log, [*values[-255:], event])
            except (OSError, ValueError):
                print("Download metadata observer could not record this response", flush=True)

        try:
            response = original_open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            record(error.code, error.headers)
            raise
        record(response.status, response.headers)
        return response

    model_download._urlopen = observe_open
    app = create_app(RynmeshStore(home=home, network_dir=home / "network", node_name="AI recovery acceptance"))
    if args.legacy_fixture:
        from starlette.routing import Mount
        from starlette.staticfiles import StaticFiles
        directory = home / 'legacy-fixture'
        assert (directory / 'index.html').is_file() and (directory / 'legacy-store.js').is_file()
        app.router.routes.insert(0, Mount('/acceptance-legacy', app=StaticFiles(directory=directory, html=True)))
    marker.write_text(json.dumps({
        "kind": "ryn.ai-recovery-acceptance.v1", "pid": os.getpid(),
        "port": 18924, "started_at": datetime.now(UTC).isoformat(),
    }, indent=2) + "\n", encoding="utf-8")
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=18924, access_log=False)


if __name__ == "__main__":
    main()
