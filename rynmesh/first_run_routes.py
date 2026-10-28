"""Owner-only first-reading progress, wired as an independent route package."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException, Query, Request

from .atomic_io import atomic_write_json, migration_backup
from .background_workers import BackgroundWorkerRegistry
from .services.digest import DigestError
from .services.first_success import FIRST_SUCCESS_VERSION, FirstSuccessStore


class FirstRunService:
    def __init__(self, home: Path, discovery: Callable, consumption: Callable,
                 profile: Callable, audit: Callable):
        self.store = FirstSuccessStore(home / "first_run" / "state.json")
        self.discovery = discovery
        self.consumption = consumption
        self.profile = profile
        self.audit = audit
        self.enabled = os.environ.get("RYNMESH_FIRST_SUCCESS_V1_ENABLED", "1") != "0"
        self.migration_failed = False
        legacy = home / "first-success.json"
        if legacy.exists() and not self.store.path.exists():
            try:
                old = FirstSuccessStore(legacy).get()
                migration_backup(legacy)
                atomic_write_json(self.store.path, old, max_bytes=64 * 1024)
            except (OSError, ValueError):
                self.migration_failed = True

    def record(self, milestone: str) -> None:
        if not self.enabled or self.migration_failed:
            return
        try:
            self.store.record(milestone)
        except (OSError, TypeError, ValueError):
            try:
                self.audit().append("verify", "First-reading progress could not be saved",
                                    details={"code": "first_success_store_unavailable"})
            except Exception:
                pass  # Diagnostics must not fail the successfully saved user action.

    def export(self) -> dict[str, Any]:
        if self.migration_failed:
            return {"version": FIRST_SUCCESS_VERSION, "safe_error": "first_success_migration_failed"}
        data = self.store.get()
        # Unknown fields survive storage, but aren't copied into a public status/export.
        return {key: data[key] for key in (
            "version", "dismissed", "dismissed_at_unix", "replay_started_at_unix"
        )} | {"milestones": {key: data["milestones"].get(key, 0.0) for key in (
            "node_ready", "content_ready", "first_item_opened", "first_signal_recorded", "completed"
        )}}

    def status(self) -> dict[str, Any]:
        if self.migration_failed:
            raise ValueError("first_success_migration_failed")
        discovery = self.discovery().discovery_status()
        records = self.consumption().list()
        existing = self.store.get()
        since = existing["replay_started_at_unix"]
        opened = any(float(row.get("last_opened_unix", 0) or 0) > since for row in records)
        signal = since <= 0 and (
            any(row.get("bookmarked") for row in records)
            or bool(self.profile().public().get("feedback_count"))
        )
        item_count = int(discovery.get("item_count", 0) or 0)
        self.store.sync(node_ready=True, content_ready=item_count > 0,
                        first_item_opened=opened, first_signal_recorded=signal)
        data = self.export()
        milestones = data["milestones"]
        completed = bool(milestones["completed"])
        failed = int(discovery.get("failed_sources", 0) or 0)
        count = int(discovery.get("source_count", 0) or 0)
        if completed:
            phase = "completed"
        elif item_count:
            phase = "awaiting_signal" if milestones["first_item_opened"] else "ready"
        elif discovery.get("phase") == "error" or (count > 0 and failed >= count):
            phase = "needs_action"
        else:
            phase = "checking_sources"
        return {
            "version": FIRST_SUCCESS_VERSION, "phase": phase,
            "completed": completed, "dismissed": data["dismissed"],
            "node_ready": True, "content_ready": item_count > 0,
            "item_count": item_count, "source_count": count, "failed_sources": failed,
            "healthy_sources": int(discovery.get("healthy_sources", 0) or 0),
            "degraded": bool(discovery.get("degraded")),
            "using_cache": bool(discovery.get("cached_sources")),
            "first_item_opened": bool(milestones["first_item_opened"]),
            "first_signal_recorded": bool(milestones["first_signal_recorded"]),
            "milestones": milestones,
            "safe_error": "discovery_unavailable" if phase == "needs_action" else None,
            "recoverable_actions": ["retry_discovery"] if phase == "needs_action" else [],
        }


def install_first_run(app: Any, *, store: Any, home: str | Path,
                      workers: BackgroundWorkerRegistry, local_control: Callable[[Request], None],
                      discovery: Callable, consumption: Callable, profile: Callable,
                      audit: Callable) -> FirstRunService:
    """No polling worker needed: the package observes the supervised discovery service."""
    state = FirstRunService(Path(getattr(store, "home", None) or home),
                            discovery, consumption, profile, audit)
    app.state.first_run = state

    def response(request: Request, action: str = "") -> dict[str, Any]:
        local_control(request)
        current = app.state.first_run
        if not current.enabled:
            raise HTTPException(404, detail="first_success_disabled")
        try:
            if action:
                getattr(current.store, action)()
            return current.status()
        except (OSError, TypeError, ValueError):
            raise HTTPException(503, detail="first_success_store_unavailable") from None

    # Reinstallation replaces state without leaving duplicate routes or closures.
    if not any(getattr(route, "name", "") == "first_run_status" for route in app.routes):
        @app.get("/api/local/first-success", name="first_run_status")
        def status(request: Request) -> dict[str, Any]:
            return response(request)

        @app.post("/api/local/first-success/dismiss")
        def dismiss(request: Request) -> dict[str, Any]:
            return response(request, "dismiss")

        @app.post("/api/local/first-success/reset")
        def reset(request: Request) -> dict[str, Any]:
            return response(request, "reset")

        @app.get("/api/local/sources/health")
        def source_health(request: Request) -> list[dict[str, Any]]:
            local_control(request)
            return app.state.first_run.discovery().source_health()

        @app.post("/api/local/sources/{source_id}/retry")
        def retry_source(source_id: str, request: Request) -> dict[str, Any]:
            local_control(request)
            service = app.state.first_run.discovery()
            try:
                refresh = service.refresh(source_id=source_id)
                digest = service.build(now_unix=time.time())
                return {"refresh": refresh, "digest": digest, "status": service.discovery_status()}
            except DigestError as exc:
                code = str(exc)
                status = 404 if code == "source_not_found" else 409 if code == "discovery_busy" else 400
                raise HTTPException(status, detail=code if code in {
                    "source_not_found", "discovery_busy"
                } else "source_retry_failed") from None
            except OSError:
                raise HTTPException(503, detail="source_retry_failed") from None

        @app.get("/api/local/recommendations/signals")
        def signals(request: Request, offset: int = Query(0, ge=0),
                    limit: int = Query(50, ge=1, le=100)) -> dict[str, Any]:
            local_control(request)
            try:
                return app.state.first_run.profile().history(offset=offset, limit=limit)
            except (OSError, ValueError):
                raise HTTPException(503, detail="recommendation_history_unavailable") from None

        @app.post("/api/local/recommendations/feedback/{event_id}/undo")
        def undo_feedback(event_id: str, request: Request) -> dict[str, Any]:
            local_control(request)
            current = app.state.first_run
            try:
                profile = current.profile().undo(event_id)
                # No model call: recompute solely from remaining local signals.
                digest = current.discovery().build(now_unix=time.time())
                return {"profile": profile, "digest": digest}
            except ValueError as exc:
                if str(exc) == "recommendation_feedback_not_found":
                    raise HTTPException(404, detail=str(exc)) from None
                raise HTTPException(503, detail="recommendation_history_unavailable") from None
            except OSError:
                raise HTTPException(503, detail="recommendation_history_unavailable") from None

    return state
