"""Owner-only Ask Ryn history API; no inference or remote publication here."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException, Request

from ..background_workers import BackgroundWorkerSpec, BackoffPolicy
from ..device_sync.records import SyncError
from ..llm_package.consumer_commands import ConsumerCommands
from .context import AskContextService
from .runs import AskRunService
from .store import ConversationError, ConversationStore


@dataclass
class AskRynState:
    conversations: ConversationStore
    local_control: Callable
    context: AskContextService
    orders: ConsumerCommands | None = None
    runs: AskRunService | None = None


def install_ask_ryn(app: Any, *, store: Any, home: str | Path, workers: Any,
                    local_control: Callable, messaging_key: Any) -> AskRynState:
    if not getattr(app.state, "ask_ryn_cache_guard", False):
        @app.middleware("http")
        async def prevent_ask_caching(request: Request, call_next):
            response = await call_next(request)
            if request.url.path.startswith("/api/local/ask/"):
                response.headers["Cache-Control"] = "no-store"
            return response

        app.state.ask_ryn_cache_guard = True

    def catalog(network: str):
        commands = app.state.ask_ryn.orders
        if commands is not None and hasattr(commands, "discover"):
            return commands.discover(network)
        records = store.list_job_capacities(network_id=network, capability="rynmesh.llm.private.v1", max_age_hours=1).get("capacities", [])
        return [{**row["metadata"]["llm_service"], "peer_id": row.get("peer_id")} for row in records if isinstance((row.get("metadata") or {}).get("llm_service"), dict)]
    app.state.ask_ryn = AskRynState(ConversationStore(Path(getattr(store, "home", None) or home) / "ask-ryn", messaging_key), local_control,
                                  AskContextService(lambda: app.state.friends.content, catalog))
    app.state.ask_ryn.runs = AskRunService(app.state.ask_ryn.conversations, lambda: app.state.ask_ryn.context, lambda: app.state.ask_ryn.orders)
    workers.register(BackgroundWorkerSpec(name="ask-ryn-runs", run_once=lambda: app.state.ask_ryn.runs.run_once(), policy=BackoffPolicy.fixed(1)), replace=True)

    async def call(method: str, *args, service: str = "conversations", **kwargs):
        try:
            return await asyncio.to_thread(getattr(getattr(app.state.ask_ryn, service), method), *args, **kwargs)
        except ConversationError as exc:
            code = str(exc)
            status = 404 if code == "ask_conversation_not_found" else 409
            raise HTTPException(status, detail=code) from None
        except SyncError as exc:
            code = str(exc)
            if code in {'sync_revision_conflict', 'sync_choice_unavailable', 'sync_restore_identity_conflict', 'sync_restore_new_identity_required', 'sync_restore_copy_not_deleted', 'sync_recovery_not_deleted', 'sync_recovery_busy'}:
                raise HTTPException(409, detail=code) from None
            raise HTTPException(503, detail='ask_history_unavailable') from None
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(503, detail="ask_history_unavailable") from None

    async def body(request: Request) -> dict:
        app.state.ask_ryn.local_control(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 2 * 1024 * 1024:
                raise HTTPException(413, detail="ask_history_limit")
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (TypeError, ValueError):
            raise HTTPException(400, detail="ask_invalid_request") from None

    @app.get("/api/local/ask/conversations")
    async def conversations(request: Request, service_key: str | None = None):
        app.state.ask_ryn.local_control(request)
        return {"conversations": await call("list", service_key)}

    @app.get("/api/local/ask/conversations/{conversation_id}")
    async def conversation(conversation_id: str, request: Request):
        app.state.ask_ryn.local_control(request)
        return await call("get", conversation_id)

    @app.put("/api/local/ask/conversations/{conversation_id}")
    async def save(conversation_id: str, request: Request):
        value = await body(request)
        row = value.get("conversation")
        if not isinstance(row, dict) or row.get("id") != conversation_id:
            raise HTTPException(400, detail="ask_invalid_conversation")
        return await call("save", row, expected_revision=value.get("expected_revision"))

    @app.delete("/api/local/ask/conversations/{conversation_id}")
    async def remove(conversation_id: str, request: Request):
        value = await body(request)
        return await call("remove", conversation_id, expected_revision=value.get("expected_revision"))

    @app.post("/api/local/ask/migrate")
    async def migrate(request: Request):
        value = await body(request)
        if value.get("source") != "ryn-private-ai-chat-v1":
            raise HTTPException(400, detail="ask_migration_source_unsupported")
        return await call("migrate", value.get("conversation"))

    @app.get("/api/local/ask/export")
    async def export(request: Request):
        app.state.ask_ryn.local_control(request)
        return await call('export_owner')

    @app.get('/api/local/ask/sync/conflicts')
    async def sync_conflicts(request: Request):
        app.state.ask_ryn.local_control(request)
        return {'conflicts': await call('sync_conflicts')}

    @app.post('/api/local/ask/sync/restore')
    async def sync_restore(request: Request):
        value = await body(request)
        return await call('sync_restore', value.get('conversation_id'), choice_id=value.get('choice_id'),
                          new_id=value.get('new_id'), expected_revision=value.get('expected_revision'), replaces=value.get('replaces'))

    @app.post('/api/local/ask/sync/discard')
    async def sync_discard(request: Request):
        value = await body(request)
        return await call('sync_discard_recovery', value.get('conversation_id'), review_token=value.get('review_token'))

    @app.get("/api/local/ask/draft")
    async def draft(request: Request):
        app.state.ask_ryn.local_control(request)
        return await call("draft")

    @app.put("/api/local/ask/draft")
    async def save_draft(request: Request):
        value = await body(request)
        return await call("save_draft", value.get("text"), expected_revision=value.get("expected_revision"))

    @app.post("/api/local/ask/contexts")
    async def prepare_context(request: Request):
        value = await body(request)
        return await call("prepare", value.get("item_id"), offline_job_id=value.get('offline_job_id'),
                          prefer_source=value.get('prefer_source', False), service="context")

    @app.get("/api/local/ask/contexts/{library_id}")
    async def read_context(library_id: str, request: Request):
        app.state.ask_ryn.local_control(request)
        return await call("describe", library_id, include_text=True, service="context")

    @app.post("/api/local/ask/preview")
    async def preview(request: Request):
        value = await body(request)
        conversation = await call("get", value.get("conversation_id"))
        if conversation["revision"] != value.get("expected_revision"):
            raise HTTPException(409, detail="ask_revision_conflict")
        return await call("preview", conversation, value.get("question"), service="context")

    @app.post("/api/local/ask/runs")
    async def begin_run(request: Request):
        return await call("begin", await body(request), service="runs")

    @app.get("/api/local/ask/runs/{task_id}")
    async def read_run(task_id: str, request: Request):
        app.state.ask_ryn.local_control(request)
        return await call("get", task_id, service="runs")

    @app.post("/api/local/ask/runs/{task_id}/cancel")
    async def cancel_run(task_id: str, request: Request):
        app.state.ask_ryn.local_control(request)
        return await call("cancel", task_id, service="runs")

    return app.state.ask_ryn
