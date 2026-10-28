"""Owner-only controls for persisted, explicit friend AI grants."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException, Request

from ..background_workers import BackgroundWorkerSpec, BackoffPolicy
from ..crypto import canonical_json
from .catalog import PATH, FriendAICatalog
from .store import AIAccessError, AIAccessStore


@dataclass
class AIAccessState:
    store: AIAccessStore
    local_control: Callable
    recheck: Callable | None = None
    provider: Callable | None = None
    catalog: FriendAICatalog | None = None


def install_ai_access(app: Any, *, home: str | Path, local_control: Callable,
                      relationship: Callable, friends: Callable | None = None, workers: Any = None) -> AIAccessStore:
    permissions = AIAccessStore(home, relationship=relationship)
    prior = getattr(app.state, "ai_access", None)
    app.state.ai_access = AIAccessState(permissions, local_control, getattr(prior, "recheck", None))
    app.state.ai_access.provider = getattr(prior, "provider", None)
    if friends:
        app.state.ai_access.catalog = FriendAICatalog(friends=friends, grants=lambda: app.state.ai_access.store,
            provider=lambda: app.state.ai_access.provider() if app.state.ai_access.provider else None)
        if workers:
            workers.register(BackgroundWorkerSpec(name="ai-access.discovery", initial_delay_s=3,
                run_once=lambda: app.state.ai_access.catalog.run_once(),
                policy=BackoffPolicy(busy_delay_s=10, idle_initial_s=10, idle_multiplier=1,
                                     idle_max_s=10, error_multiplier=2, error_max_s=60)), replace=True)
    if any(getattr(route, "name", "") == "ai_access_list" for route in app.routes):
        return permissions

    @app.get("/api/local/ai-access/friend-services")
    async def friend_services(request: Request):
        app.state.ai_access.local_control(request)
        catalog = app.state.ai_access.catalog
        return {"friends": await asyncio.to_thread(catalog.snapshots) if catalog else []}

    @app.post("/api/local/ai-access/friend-services")
    async def refresh_friend(peer_id: str, request: Request):
        app.state.ai_access.local_control(request)
        catalog = app.state.ai_access.catalog
        if not catalog:
            raise HTTPException(503, detail="ai_catalog_unavailable")
        try:
            return await asyncio.to_thread(catalog.refresh, peer_id)
        except Exception:
            raise HTTPException(409, detail="ai_friend_service_unreachable") from None

    @app.post(PATH)
    async def peer_services(request: Request):
        catalog = app.state.ai_access.catalog
        if not catalog:
            raise HTTPException(503, detail="ai_catalog_unavailable")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096:
                raise HTTPException(413, detail="ai_catalog_request_too_large")
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError
            relationship = await asyncio.to_thread(catalog.friends().verify_request,
                path=PATH, body=canonical_json(body), headers=request.headers)
            return await asyncio.to_thread(catalog.respond, body, relationship)
        except Exception:
            raise HTTPException(403, detail="ai_catalog_request_rejected") from None

    @app.get("/api/local/ai-access", name="ai_access_list")
    async def list_grants(request: Request):
        app.state.ai_access.local_control(request)
        try:
            return {"grants": await asyncio.to_thread(app.state.ai_access.store.list)}
        except (AIAccessError, OSError, ValueError):
            raise HTTPException(503, detail="ai_permissions_unavailable") from None

    @app.put("/api/local/ai-access/{service_id}/{relationship_id}")
    async def set_grant(service_id: str, relationship_id: str, request: Request):
        app.state.ai_access.local_control(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096:
                raise HTTPException(413, detail="ai_permission_request_too_large")
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError
            grant = await asyncio.to_thread(app.state.ai_access.store.set, service_id, relationship_id,
                                             allowed=body.get("allowed"), expected_revision=body.get("expected_revision"))
        except AIAccessError as exc:
            raise HTTPException(409, detail=str(exc)) from None
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, detail="ai_permission_request_invalid") from None
        except OSError:
            raise HTTPException(503, detail="ai_permissions_unavailable") from None
        # The grant is already durable. Cancellation is best effort and has
        # its own result; an unsuccessful notification must not undo revocation.
        cancellation = "not_requested"
        if not grant["allowed"] and app.state.ai_access.recheck:
            try:
                await asyncio.to_thread(app.state.ai_access.recheck)
                cancellation = "requested"
            except Exception:
                cancellation = "pending"
        return {"grant": grant, "cancellation": cancellation}

    return permissions
