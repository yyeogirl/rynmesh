"""Loopback-only inference API with separate, revocable project credentials."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from rynmesh.node_auth import is_browser_cross_site, is_forwarded, is_loopback_addr

from .api_formats import StreamFormat, convert_response, normalize, sse
from .chat import completion
from .streaming import events


class InferenceKeys:
    def __init__(self, home: Path):
        self.path = home / "llm" / "inference-keys.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.active: dict[str, int] = {}
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS keys (id TEXT PRIMARY KEY, digest TEXT UNIQUE, name TEXT, created REAL, revoked INTEGER DEFAULT 0, token_limit INTEGER, used INTEGER DEFAULT 0)")
            db.execute("CREATE TABLE IF NOT EXISTS model_aliases (name TEXT PRIMARY KEY, target TEXT NOT NULL)")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create(self, name: str, token_limit: int) -> dict[str, Any]:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or type(token_limit) is not int or not 1 <= token_limit <= 10**9:
            raise ValueError("name (1–80 characters) and output_token_limit (1–1000000000) are required")
        key = "ryn_" + secrets.token_urlsafe(32)
        key_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO keys(id,digest,name,created,token_limit) VALUES(?,?,?,?,?)", (key_id, hashlib.sha256(key.encode()).hexdigest(), name.strip(), time.time(), token_limit))
        return {"id": key_id, "key": key, "name": name.strip(), "output_token_limit": token_limit}

    def list(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,name,created,revoked,token_limit AS output_token_limit,used AS used_output_tokens FROM keys ORDER BY created DESC")]

    def revoke(self, key_id: str):
        with self.connect() as db:
            db.execute("UPDATE keys SET revoked=1 WHERE id=?", (key_id,))

    def aliases(self) -> dict[str, str]:
        with self.connect() as db:
            return dict(db.execute("SELECT name,target FROM model_aliases ORDER BY name"))

    def set_alias(self, name: str, target: str):
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}", name):
            raise ValueError("alias must be 1–64 letters, digits, dots, underscores or hyphens")
        with self.connect() as db:
            db.execute("INSERT INTO model_aliases(name,target) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET target=excluded.target", (name, target))

    def authenticate(self, token: str) -> str:
        with self.connect() as db:
            row = db.execute("SELECT id FROM keys WHERE digest=? AND revoked=0", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or not token:
            raise HTTPException(401, "invalid_api_key")
        return row["id"]

    def reserve(self, key_id: str, tokens: int):
        with self.lock:
            if self.active.get(key_id, 0) >= 2:
                raise HTTPException(429, "project_concurrency_limit")
            with self.connect() as db:
                changed = db.execute("UPDATE keys SET used=used+? WHERE id=? AND revoked=0 AND used+?<=token_limit", (tokens, key_id, tokens)).rowcount
                if not changed:
                    raise HTTPException(429, "project_output_token_limit")
            self.active[key_id] = self.active.get(key_id, 0) + 1

    def finish(self, key_id: str, reserved: int, used: int):
        with self.lock:
            with self.connect() as db:
                db.execute("UPDATE keys SET used=MAX(0,used-?+?) WHERE id=?", (reserved, used, key_id))
            self.active[key_id] = max(0, self.active.get(key_id, 1) - 1)


def install_inference_api(app: Any, *, home: Path, store: Any, active_manager: Any,
                          discover: Any, execute_order: Any, cancel_order: Any):
    keys = InferenceKeys(home)
    network = os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main")

    def authorize(request: Request) -> str:
        host = urlparse("http://" + request.headers.get("host", "")).hostname
        if not is_loopback_addr(request.client.host if request.client else "") or is_forwarded(request.headers) or is_browser_cross_site(request.headers) or host not in {"127.0.0.1", "::1", "localhost", "testserver"}:
            raise HTTPException(403, "inference_api_is_loopback_only")
        bearer = request.headers.get("authorization", "")
        token = bearer[7:] if bearer.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
        return keys.authenticate(token)

    def model_catalog():
        values = []
        manager = active_manager()
        if manager and manager.adapter.health().get("ok"):
            values.append({"id": "local/" + manager.manifest.package_id, "object": "model", "created": 0, "owned_by": "local", "rynmesh": {"source": "local", "model_alias": manager.manifest.public_model_alias, "max_output_tokens": manager.manifest.max_output_tokens, "context_window": manager.manifest.context_window}})
        try:
            discovered = discover(network)
        except Exception:
            discovered = []  # local inference remains usable when registry is unavailable
        for item in discovered:
            if not item.get("online") or item.get("peer_id") == store.peer_id or item.get("chat_protocol") != "rynmesh.chat.v1":
                continue
            service = item["service"]
            # A full stable digest avoids ambiguity when models share aliases.
            digest = hashlib.sha256(item["peer_id"].encode()).hexdigest()
            values.append({"id": "peer/" + digest + "/" + service["package_id"], "object": "model", "created": 0, "owned_by": "rynmesh", "rynmesh": {"source": "peer", "provider_peer_id": item["peer_id"], "service_id": service["package_id"], "model_alias": service["model_alias"], "max_output_tokens": service["max_output_tokens"], "context_window": service["context_window"]}})
        return values

    def public_models(catalog, aliases):
        targets = {item["id"]: item for item in catalog}
        short = [{**targets[target], "id": name} for name, target in aliases.items() if target in targets]
        return short + [item for item in catalog if item["id"] not in aliases.values()]

    @app.get("/api/local/llm/api-access")
    def access():
        port = int(os.environ.get("RYNMESH_PEER_PORT", "8791"))
        catalog = model_catalog()
        aliases = keys.aliases()
        return {"base_url": f"http://127.0.0.1:{port}/v1", "keys": keys.list(), "models": public_models(catalog, aliases), "targets": catalog, "aliases": aliases, "protocols": ["chat_completions", "responses_stateless", "anthropic_messages"], "loopback_only": True}

    @app.put("/api/local/llm/model-aliases/{name}")
    async def set_model_alias(name: str, request: Request):
        try:
            value = await request.json()
            target = value.get("target")
            catalog = await asyncio.to_thread(model_catalog)
            if not isinstance(target, str) or not any(item["id"] == target for item in catalog):
                raise HTTPException(400, "choose an available model target")
            keys.set_alias(name, target)
            return {"name": name, "target": target}
        except (ValueError, TypeError, AttributeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/local/llm/api-keys")
    async def create_key(request: Request):
        try:
            value = await request.json()
            return keys.create(value.get("name", ""), value.get("output_token_limit", 100000))
        except (ValueError, TypeError, AttributeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/local/llm/api-keys/{key_id}")
    def revoke_key(key_id: str):
        keys.revoke(key_id)
        return {"revoked": True}

    def error(exc: Exception, protocol: str) -> JSONResponse:
        status = exc.status_code if isinstance(exc, HTTPException) else 400 if isinstance(exc, (ValueError, TypeError, KeyError, AttributeError, IndexError)) else 502
        message = str(exc.detail) if isinstance(exc, HTTPException) else str(exc) if status == 400 else "model_inference_failed"
        kind = "authentication_error" if status == 401 else "permission_error" if status == 403 else "rate_limit_error" if status == 429 else "invalid_request_error" if status < 500 else "api_error"
        body = {"error": {"type": kind, "message": message}}
        if protocol == "messages":
            body["type"] = "error"
        else:
            body["error"].update(code=message, param=None)
        return JSONResponse(body, status_code=status)

    @app.get("/v1/models")
    async def models(request: Request):
        try:
            authorize(request)
            return {"object": "list", "data": public_models(await asyncio.to_thread(model_catalog), keys.aliases())}
        except Exception as exc:
            return error(exc, "chat")

    async def handle(request: Request, protocol: str):
        try:
            key_id = authorize(request)
            raw = bytearray()
            async for part in request.stream():
                raw.extend(part)
                if len(raw) > 1024 * 1024:
                    raise HTTPException(413, "request exceeds 1 MiB")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("request must be a JSON object")
            chat = normalize(body, protocol)
            catalog = await asyncio.to_thread(model_catalog)
            requested = body.get("model")
            if not isinstance(requested, str):
                raise ValueError("model must be a string")
            aliases = keys.aliases()
            target_id = aliases.get(requested, requested)
            model = next((m for m in catalog if m["id"] == target_id), None)
            if model is None:
                if requested in aliases:
                    raise HTTPException(503, "model_alias_target_unavailable")
                raise HTTPException(404, "model_not_found; choose an id from /v1/models")
            model = {**model, "id": requested}
            target = model["rynmesh"]
            # Client defaults often describe a larger model (e.g. 128k output).
            # A requested maximum is a ceiling, so fit it to the selected model
            # and estimated remaining context before reserving quota or routing.
            estimated_input = max(1, (len(json.dumps(chat, ensure_ascii=False)) + 3) // 4)
            available = target["context_window"] - estimated_input
            if available < 1:
                raise HTTPException(400, "estimated context window exceeded")
            chat["max_tokens"] = min(chat["max_tokens"], target["max_output_tokens"], available)
            task_id = "api_" + uuid.uuid4().hex
            manager = active_manager() if target["source"] == "local" else None
            if manager and not manager._slots.acquire(blocking=False):
                raise HTTPException(429, "local_model_busy")
            try:
                keys.reserve(key_id, chat["max_tokens"])
            except Exception:
                if manager:
                    manager._slots.release()
                raise
        except Exception as exc:
            return error(exc, protocol)

        started = False

        async def run(emit):
            nonlocal started
            started = True
            used = chat["max_tokens"]  # interrupted/unknown usage conservatively keeps the reservation
            try:
                if manager:
                    with manager._lock:
                        manager._running += 1
                    result = await asyncio.to_thread(manager.adapter.chat, chat, task_id=task_id,
                                                     timeout_s=manager.manifest.timeout_seconds, on_event=emit)
                else:
                    result = await execute_order({"network_id": network, "provider_peer_id": target["provider_peer_id"],
                                                  "service_id": target["service_id"], "chat": chat,
                                                  "transport": "p2p",
                                                  "max_tokens": chat["max_tokens"], "task_id": task_id}, emit)
                    if result.get("state") != "succeeded":
                        raise HTTPException(502, result.get("error_code", "inference_failed"))
                used = result["output_tokens"]
                return completion(result, model["id"], task_id)
            finally:
                keys.finish(key_id, chat["max_tokens"], used)
                if manager:
                    with manager._lock:
                        manager._running -= 1
                    manager._slots.release()

        def cancel():
            if manager:
                manager.adapter.cancel(task_id)
            else:
                try:
                    cancel_order(task_id)
                except HTTPException:
                    pass

        if not chat.get("stream"):
            # Watch a disconnected client while keeping the worker alive for cleanup.
            task = asyncio.create_task(run(None))
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=0.2)
                    if await request.is_disconnected():
                        await asyncio.to_thread(cancel)
                        break
                return convert_response(await asyncio.shield(task), protocol)
            except asyncio.CancelledError:
                cleanup = asyncio.create_task(asyncio.to_thread(cancel))
                cleanup.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
                raise
            except Exception as exc:
                return error(exc, protocol)

        async def generate():
            formatter = StreamFormat(protocol, model["id"], task_id)
            try:
                for value in formatter.start():
                    yield value
                async for kind, value in events(run, cancel):
                    if kind == "event" and protocol == "chat" and not value.get("choices") and not (body.get("stream_options") or {}).get("include_usage"):
                        continue
                    for data in formatter.delta(value) if kind == "event" else formatter.finish(value):
                        yield data
            except Exception as exc:
                payload = json.loads(error(exc, protocol).body)
                yield sse(payload if protocol != "responses" else {"type": "error", "code": "inference_failed", "message": payload["error"]["message"]}, "error" if protocol != "chat" else "")

        def release_unused():
            if not started:
                keys.finish(key_id, chat["max_tokens"], 0)
                if manager:
                    manager._slots.release()

        from starlette.background import BackgroundTask

        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}, background=BackgroundTask(release_unused))

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        return await handle(request, "chat")

    @app.post("/v1/responses")
    async def responses(request: Request):
        return await handle(request, "responses")

    @app.post("/v1/messages")
    async def messages(request: Request):
        return await handle(request, "messages")
