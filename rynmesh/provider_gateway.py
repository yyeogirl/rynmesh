"""Single-port public Provider gateway for registry, peer tasks, and video."""

from __future__ import annotations

import asyncio
import hashlib
import os
import urllib.error
import urllib.request
from collections.abc import Callable

from .registry_http import create_app as create_registry_app
from .video_package.service import create_app as create_video_app


def _forward_peer_task(body: bytes) -> tuple[int, bytes, str]:
    endpoint = os.environ.get(
        "RYNMESH_PROVIDER_PEER_UPSTREAM", "http://127.0.0.1:8791"
    ).rstrip("/")
    network_key = os.environ.get("RYNMESH_NETWORK_KEY", "").strip()
    headers = {"Content-Type": "application/json"}
    if network_key:
        headers["X-Ryn-Auth"] = hashlib.sha256(
            ("rynmesh-net-key:" + network_key).encode("utf-8")
        ).hexdigest()
    request = urllib.request.Request(
        endpoint + "/api/peer/llm/tasks",
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return (
                response.status,
                response.read(),
                response.headers.get("content-type", "application/json"),
            )
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("content-type", "application/json")


def create_peer_proxy_app(
    *,
    forward: Callable[[bytes], tuple[int, bytes, str]] = _forward_peer_task,
):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response

    globals()["Request"] = Request
    app = FastAPI(title="Rynmesh Provider Peer Proxy", docs_url=None, redoc_url=None)

    @app.post("/api/peer/llm/tasks")
    async def peer_llm_task(request: Request):
        body = await request.body()
        if not body or len(body) > 8 * 1024 * 1024:
            return JSONResponse({"detail": "invalid task envelope"}, status_code=400)
        try:
            status, payload, content_type = await asyncio.to_thread(forward, body)
        except (OSError, TimeoutError):
            return JSONResponse({"detail": "provider peer unavailable"}, status_code=502)
        return Response(content=payload, status_code=status, media_type=content_type)

    return app


def create_app():
    app = create_registry_app()
    app.mount("/video", create_video_app())
    app.mount("/peer", create_peer_proxy_app())
    return app


def main() -> int:
    import uvicorn

    uvicorn.run(
        "rynmesh.provider_gateway:create_app",
        factory=True,
        host=os.environ.get("RYNMESH_PROVIDER_HOST", "0.0.0.0"),
        port=int(os.environ.get("RYNMESH_PROVIDER_PORT", "3000")),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
