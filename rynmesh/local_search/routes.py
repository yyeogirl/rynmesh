"""Owner-only search; keywords travel in POST bodies, never access-log URLs."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import HTTPException, Request

from ..background_workers import BackgroundWorkerSpec, BackoffPolicy
from .index import LocalSearchIndex, SearchError


@dataclass
class SearchState:
    index: LocalSearchIndex
    local_control: Callable
    workers: object


def install_local_search(app, *, store, home, workers, messaging_key, local_control, source):
    root = Path(getattr(store, "home", None) or home) / "local-search"
    app.state.local_search = SearchState(LocalSearchIndex(root, messaging_key=messaging_key, source=source), local_control, workers)
    workers.register(BackgroundWorkerSpec(name="local-search.index", initial_delay_s=1,
        run_once=lambda: app.state.local_search.index.rebuild(), policy=BackoffPolicy.fixed(1)), replace=True)
    if any(getattr(route, "name", "") == "local_search_status" for route in app.routes):
        return app.state.local_search.index

    @app.middleware("http")
    async def prevent_search_caching(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/local/search/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    async def call(method, *args, **kwargs):
        try:
            return await asyncio.to_thread(getattr(app.state.local_search.index, method), *args, **kwargs)
        except SearchError as exc:
            raise HTTPException(409, detail=str(exc)) from None
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(503, detail="search_source_unavailable") from None

    @app.get("/api/local/search/status", name="local_search_status")
    def status(request: Request):
        app.state.local_search.local_control(request)
        return {**app.state.local_search.index.status(), "worker": app.state.local_search.workers.status().get("local-search.index")}

    @app.post("/api/local/search/query")
    async def query(request: Request):
        app.state.local_search.local_control(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8192:
                raise HTTPException(413, detail="search_request_too_large")
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or "query" not in body or set(body) - {"query", "kind", "source", "friend_id", "after", "before", "sort", "limit", "cursor"}:
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, detail="search_request_invalid") from None
        return await call("query", **body)

    @app.post("/api/local/search/rebuild")
    async def rebuild(request: Request):
        app.state.local_search.local_control(request)
        await call("rebuild", force=True)
        return app.state.local_search.index.status()

    @app.get("/api/local/search/open")
    async def open_result(request: Request, identifier: str):
        app.state.local_search.local_control(request)
        if len(identifier) > 512:
            raise HTTPException(400, detail="search_request_invalid")
        return await call("resolve", identifier)

    return app.state.local_search.index
