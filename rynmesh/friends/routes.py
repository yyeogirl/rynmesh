"""Owner-controlled pairing and authenticated private sharing routes."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException, Request, Response

from ..background_workers import BackgroundWorkerSpec, BackoffPolicy, WorkerRunResult
from ..services.library_imports import LibraryImportStore
from .content import FriendContent
from .crypto import FriendCryptoError, validate_endpoint
from .mailbox import wire_mailbox
from .service import MAX_ATTACHMENT_BYTES, FriendError, FriendService

# A 5 MiB attachment is base64 inside encrypted JSON, then base64 on the wire.
# Include bounded text/envelope overhead without advertising an unreachable limit.
MAX_WIRE_BYTES = ((((MAX_ATTACHMENT_BYTES + 2) // 3) * 4 + 131072 + 2) // 3) * 4 + 131072


@dataclass
class FriendsState:
    service: FriendService
    local_control: Callable
    content: FriendContent
    attempts: dict[str, list[float]] = field(default_factory=dict)
    poll_index: int = 0


SAFE_ERRORS = {
    'friend_card_erased', 'friend_card_cleanup_review_changed', 'friend_card_cleanup_file_unavailable',
    'friend_card_cleanup_version_unsupported', 'friend_card_cleanup_limit',
    "invalid_invite", "invite_expired", "invite_used", "invite_cancelled", "invite_not_found",
    "invalid_join", "could_not_join_friend", "friend_acceptance_invalid", "unsafe_endpoint",
    "friend_revoked", "friend_not_found", "active_friend_required", "friend_queue_full",
    "message_too_large", "attachment_too_large", "message_id_invalid", "message_id_conflict",
    "friend_card_not_found", "friend_card_content_unavailable", "friend_card_hash_mismatch",
    "friend_card_fetch_failed", "friend_capacity_exhausted", "friend_store_version_unsupported",
    "friend_card_read_first", "friend_card_content_too_large", "friend_card_id_conflict", "friend_card_capacity_exhausted",
    "friend_copy_unavailable", "library_import_cancelled_by_cleanup", "library_import_version_unsupported",
    "friend_card_content_changed",
    "library_cleanup_pending", "library_cleanup_review_changed", "library_cleanup_files_changed",
    "library_cleanup_not_found", "library_cleanup_limit", "library_cleanup_backup_failed",
}


async def _body(request: Request, limit: int = MAX_WIRE_BYTES) -> dict[str, Any]:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise HTTPException(413, detail="friend_request_too_large")
    try:
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict):
            raise ValueError
        return body
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, detail="friend_request_invalid") from None


def install_friends(app: Any, *, store: Any, home: str | Path, workers: Any,
                    local_control: Callable, messaging_key: Any,
                    post_json: Callable | None = None) -> FriendService:
    from ..peer_http import HttpPeerClient

    def post(endpoint, path, payload, headers, *, max_response_bytes=2 * 1024 * 1024):
        validate_endpoint(endpoint, allow_loopback=current().allow_loopback)
        return HttpPeerClient(endpoint, timeout_s=5).post_json(
            path, payload, headers=headers, max_bytes=max_response_bytes
        )

    endpoint = os.environ.get("RYNMESH_FRIEND_ENDPOINT", "") or str(store.node_info().get("peer_endpoint", ""))
    service = FriendService(
        home=Path(getattr(store, "home", None) or home), peer_id=store.peer_id,
        node_name=store.node_name, endpoint=endpoint, identity_private=store.private_key_bytes,
        messaging_private=messaging_key, post_json=post_json or post,
        allow_loopback=os.environ.get("RYNMESH_FRIEND_ALLOW_LOOPBACK", "0") == "1",
    )
    content = FriendContent(store=store, imports=LibraryImportStore(Path(getattr(store, "home", None) or home) / "library-imports"),
                            cache=lambda: app.state.reader_cache,
                            consumption=lambda: app.state.consumption_store,
                            offline=lambda: getattr(getattr(app.state, 'offline_reading', None), 'service', None))
    app.state.friends = FriendsState(service, local_control, content)
    from ..services.library_cleanup_routes import install_library_cleanup_routes
    install_library_cleanup_routes(app)
    service.resolve_content = lambda library_id: app.state.friends.content.resolve(library_id)
    service.import_content = lambda resource: app.state.friends.content.import_card(resource)
    service.import_generation = lambda: app.state.friends.content.imports.generation()
    service.verify_import = lambda library_id: app.state.friends.content.imports.body(library_id.removeprefix("import:"))

    def current():
        return app.state.friends.service

    def control(request):
        app.state.friends.local_control(request)

    async def call(method, *args, **kwargs):
        try:
            return await asyncio.to_thread(method, *args, **kwargs)
        except (FriendError, FriendCryptoError, ValueError, OSError) as exc:
            code = str(exc)
            raise HTTPException(409 if code in SAFE_ERRORS else 503,
                                detail=code if code in SAFE_ERRORS else "friend_operation_unavailable") from None

    if getattr(app.state, "mailbox", None):
        wire_mailbox(mailbox=app.state.mailbox, service=current)

    def tick():
        friends = [friend for friend in current().list_friends() if friend["status"] == "active" or friend.get("revocation_delivery") == "pending"]
        if not friends:
            return WorkerRunResult(activity=False)
        index = app.state.friends.poll_index % len(friends)
        app.state.friends.poll_index += 1
        if friends[index]["status"] == "revoked":
            result = current().retry_revocation(friends[index]["relationship_id"], automatic=True)
            return WorkerRunResult(activity=bool(result["delivered"]))
        result = current().retry(friends[index]["peer_id"], automatic=True, limit=1)
        return WorkerRunResult(activity=bool(result["attempted"]))

    workers.register(BackgroundWorkerSpec(
        name="friends.delivery", run_once=tick, initial_delay_s=3,
        policy=BackoffPolicy(busy_delay_s=3, idle_initial_s=5, idle_multiplier=1.5,
                             idle_max_s=30, error_multiplier=2, error_max_s=60),
    ), replace=True)

    if any(getattr(route, "name", "") == "friend_list" for route in app.routes):
        return service

    @app.get("/api/local/friends", name="friend_list")
    async def list_friends(request: Request):
        control(request)
        return {"friends": await call(current().list_friends)}

    @app.get("/api/local/friends/capabilities")
    def capabilities(request: Request):
        control(request)
        return {"attachment_max_bytes": MAX_ATTACHMENT_BYTES, "mailbox_max_envelope_bytes": 65536,
                "default_invite_minutes": 15, "message_ttl_seconds": 3600}

    @app.post("/api/local/friends/invites")
    async def invite(request: Request):
        control(request)
        body = await _body(request, 4096)
        return await call(current().create_invite, ttl_minutes=body.get("ttl_minutes", 15))

    @app.get("/api/local/friends/invites")
    async def invites(request: Request):
        control(request)
        return {"invites": await call(current().list_invites)}

    @app.post("/api/local/friends/invites/inspect")
    async def inspect(request: Request):
        control(request)
        body = await _body(request, 24576)
        return await call(current().inspect_invite, str(body.get("invite_uri", "")))

    @app.delete("/api/local/friends/invites/{invite_id}")
    async def cancel(invite_id: str, request: Request):
        control(request)
        return await call(current().cancel_invite, invite_id)

    @app.post("/api/local/friends/join")
    async def join(request: Request):
        control(request)
        body = await _body(request, 24576)
        return await call(current().join, str(body.get("invite_uri", "")))

    @app.delete("/api/local/friends/{relationship_id}")
    async def revoke(relationship_id: str, request: Request):
        control(request)
        return await call(current().revoke, relationship_id)

    @app.post("/api/local/friends/{relationship_id}/retry-revocation")
    async def retry_revocation(relationship_id: str, request: Request):
        control(request)
        return await call(current().retry_revocation, relationship_id)

    @app.get("/api/local/friends/{peer_id:path}/messages")
    async def messages(peer_id: str, request: Request):
        control(request)
        return {"messages": await call(current().history, peer_id)}

    @app.post("/api/local/friends/{peer_id:path}/messages")
    async def send(peer_id: str, request: Request):
        control(request)
        body = await _body(request)
        attachment = body.get("attachment")
        if attachment is not None:
            try:
                attachment = {"filename": str(attachment.get("filename", "file")),
                              "mime": str(attachment.get("mime", "application/octet-stream")),
                              "bytes": base64.b64decode(attachment.get("data_base64", ""), validate=True)}
            except (AttributeError, ValueError):
                raise HTTPException(400, detail="friend_attachment_invalid") from None
        return await call(current().send_message, peer_id, text=str(body.get("text", "")),
                          attachment=attachment, message_id=str(body.get("message_id", "")))

    @app.post("/api/local/friends/{peer_id:path}/retry-messages")
    async def retry_messages(peer_id: str, request: Request):
        control(request)
        return await call(current().retry, peer_id, limit=1)

    @app.get("/api/local/friends/cards")
    async def cards(request: Request):
        control(request)
        return {"cards": await call(visible_cards)}

    def visible_cards():
        rows = current().content_cards()
        imports = app.state.friends.content.imports
        for row in rows:
            identifier = row.get('fetched_library_id', '')
            if row.get('fetch_state') == 'fetched' and identifier.startswith('import:'):
                try:
                    imports.get(identifier.removeprefix('import:'))
                except (ValueError, OSError):
                    row.update(fetch_state='unavailable', sha256_verified=False)
        return rows

    @app.post("/api/local/friends/share")
    async def share(request: Request):
        control(request)
        body = await _body(request, 4096)
        peer_id, card_id = str(body.get("peer_id", "")), str(body.get("card_id", ""))
        item_id = str(body.get("item_id", ""))
        if not card_id:
            raise HTTPException(400, detail="friend_card_invalid")
        await call(current()._relationship, peer_id)
        prior = await call(current().store.card, card_id)
        if prior:
            if (prior.get("to") != peer_id or prior.get("dir") != "out" or prior.get("source_item_id") != item_id
                    or body.get('offline_job_id') is not None and prior.get('source_offline_job_id') != body.get('offline_job_id')
                    or body.get('prefer_source') is True and prior.get('source_offline_job_id')):
                raise HTTPException(409, detail="friend_card_id_conflict")
            return await call(current().retry_card, card_id)
        card = await call(app.state.friends.content.prepare, {"item_id": item_id, 'offline_job_id': body.get('offline_job_id'),
                                                            'prefer_source': body.get('prefer_source', False)})
        card["source_item_id"] = item_id
        return await call(current().send_content_card, peer_id, card, card_id=card_id)

    @app.get('/api/local/friends/card-cleanup')
    async def card_cleanup_review(request: Request, response: Response):
        control(request)
        response.headers['Cache-Control'] = 'no-store'
        from .card_cleanup import CardCleanup
        return await call(CardCleanup(current().store).preview)

    @app.post('/api/local/friends/card-cleanup')
    async def card_cleanup_begin(request: Request, response: Response):
        control(request)
        response.headers['Cache-Control'] = 'no-store'
        body = await _body(request, 1024)
        if set(body) != {'review_token'}:
            raise HTTPException(400, detail='friend_request_invalid')
        from .card_cleanup import CardCleanup
        return await call(CardCleanup(current().store).begin, body['review_token'])

    @app.post("/api/local/friends/cards/{card_id}/fetch")
    async def fetch_card(card_id: str, request: Request):
        control(request)
        body = await _body(request, 4096)
        if "repair" in body and not isinstance(body["repair"], bool):
            raise HTTPException(400, detail="friend_request_invalid")
        return await call(current().fetch_content_card, card_id, repair=body.get("repair", False))

    @app.post("/api/local/friends/cards/{card_id}/retry")
    async def retry_card(card_id: str, request: Request):
        control(request)
        return await call(current().retry_card, card_id)

    @app.get("/api/local/friends/documents/{import_id}/body")
    async def document_body(import_id: str, request: Request):
        control(request)
        return await call(app.state.friends.content.imports.body, import_id)

    @app.get("/api/local/friends/documents")
    async def documents(request: Request):
        control(request)
        rows = await call(app.state.friends.content.imports.list)
        return {"documents": [{key: row[key] for key in ("import_id", "filename", "state", "size_bytes", "created_at_unix") if key in row} for row in rows]}

    @app.delete("/api/local/friends/documents/{import_id}")
    async def remove_document(import_id: str, request: Request):
        control(request)
        body = await _body(request, 8192)
        if set(body) != {'review_token'}:
            raise HTTPException(409, detail='library_cleanup_review_required')
        return await call(remove_copies, import_id, body['review_token'])

    @app.post("/api/local/friends/documents/clear")
    async def clear_documents(request: Request):
        control(request)
        body = await _body(request, 8192)
        if set(body) != {'review_token'}:
            raise HTTPException(409, detail='library_cleanup_review_required')
        return await call(remove_copies, None, body['review_token'])

    def remove_copies(import_id, token):
        from ..services.library_cleanup import LibraryCleanup
        return LibraryCleanup(app.state.friends.content.imports).begin(scope=import_id, review_token=token)

    @app.post("/api/peer/friends/content-card/fetch")
    async def peer_card_fetch(request: Request):
        from ..crypto import canonical_json
        body = await _body(request, 4096)
        try:
            relationship = await asyncio.to_thread(current().verify_request, path="/api/peer/friends/content-card/fetch", body=canonical_json(body), headers=request.headers)
            return await asyncio.to_thread(current().serve_content_card, body, relationship)
        except (FriendError, ValueError, OSError):
            raise HTTPException(403, detail="friend_request_rejected") from None

    @app.get("/api/local/friends/{peer_id:path}/attachments/{message_id}")
    async def attachment(peer_id: str, message_id: str, request: Request):
        control(request)
        rows = await call(current().history, peer_id)
        row = next((row for row in rows if row.get("msg_id") == message_id), None)
        if not row or not isinstance(row.get("attachment"), dict):
            raise HTTPException(404, detail="friend_attachment_not_found")
        raw = await call(current().messages.load_attachment, row["attachment"].get("blob_id", message_id))
        return Response(raw, media_type="application/octet-stream", headers={
            "Content-Disposition": "attachment", "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        })

    @app.post("/api/peer/friends/accept")
    async def accept(request: Request):
        host = request.client.host if request.client else "unknown"
        now = time.monotonic()
        attempts = app.state.friends.attempts
        for key in list(attempts):
            attempts[key] = [stamp for stamp in attempts[key] if now - stamp < 60]
            if not attempts[key]:
                attempts.pop(key, None)
        recent = attempts.setdefault(host, [])
        if len(attempts) > 1024 or len(recent) >= 20:
            raise HTTPException(429, detail="friend_pairing_busy")
        recent.append(now)
        body = await _body(request, 32768)
        try:
            return await asyncio.to_thread(current().accept, body)
        except FriendError as exc:
            from ..crypto import sign_payload

            code = str(exc)
            if code not in SAFE_ERRORS:
                raise HTTPException(503, detail="friend_operation_unavailable") from None
            invite_data = body.get("invite")
            invite_payload = invite_data.get("payload") if isinstance(invite_data, dict) else {}
            if not isinstance(invite_payload, dict):
                invite_payload = {}
            refusal = {"error": code, "invite_id": str(invite_payload.get("invite_id", "")),
                       "receiver": str(body.get("peer_id", ""))}
            return {**refusal, "proof": sign_payload(refusal, private_key_bytes=current().identity_private).to_dict()}

    @app.post("/api/peer/friends/{operation}")
    async def operation(operation: str, request: Request):
        if operation not in {"message", "revoke", "content-card"}:
            raise HTTPException(404)
        body = await _body(request)
        path = f"/api/peer/friends/{operation}"
        from ..crypto import canonical_json

        try:
            if operation == "revoke":
                await asyncio.to_thread(current().receive_revoke, body)
            else:
                await asyncio.to_thread(current().verify_request, path=path, body=canonical_json(body), headers=request.headers)
                await asyncio.to_thread(current().receive_message if operation == "message" else current().receive_content_card, body)
            return current().delivery_receipt(path, body)
        except Exception:
            raise HTTPException(403, detail="friend_request_rejected") from None

    return service
