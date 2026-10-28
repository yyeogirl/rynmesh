"""Owner feed controls and an authenticated encrypted peer endpoint."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from fastapi import HTTPException, Request

from ..background_workers import BackgroundWorkerSpec, BackoffPolicy
from ..crypto import canonical_json
from .cleanup import FeedCleanup
from .service import PATH, FriendFeed
from .store import FeedError, FeedStore


@dataclass
class FeedState:
    service: FriendFeed
    local_control: object


async def body(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 8192:
            raise HTTPException(413, detail='feed_request_too_large')
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, detail='feed_request_invalid') from None


def install_friend_feed(app, *, home, messaging_key, friends, content, local_control, workers):
    store = FeedStore(home, messaging_key=messaging_key)
    app.state.friend_feed = FeedState(FriendFeed(store=store, friends=friends, content=content), local_control)

    def current():
        return app.state.friend_feed.service

    def tick():
        try:
            return current().run_once()
        except FeedError:
            return False  # Per-subscription failures are persisted; continue rotating.

    workers.register(BackgroundWorkerSpec(name='friend-feed.refresh', initial_delay_s=3,
        run_once=tick, policy=BackoffPolicy.fixed(3)), replace=True)
    if any(getattr(route, 'name', '') == 'friend_feed_publications' for route in app.routes):
        return current()

    async def call(request, method, *args, _cleanup=False, **kwargs):
        app.state.friend_feed.local_control(request)
        try:
            target = FeedCleanup(current().store) if _cleanup else current()
            return await asyncio.to_thread(getattr(target, method), *args, **kwargs)
        except FeedError as exc:
            raise HTTPException(409, detail=str(exc)) from None
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            raise HTTPException(503, detail='feed_operation_unavailable') from None

    @app.get('/api/local/friend-feed/publications', name='friend_feed_publications')
    async def publications(request: Request):
        return {'publications': await call(request, 'publications')}

    async def cleanup_body(request):
        app.state.friend_feed.local_control(request)
        value = await body(request)
        if set(value) != {'review_token'}:
            raise HTTPException(400, detail='feed_request_invalid')
        return value['review_token']

    @app.get('/api/local/privacy/friend-feed/preview')
    async def cleanup_preview(request: Request):
        return await call(request, 'preview', _cleanup=True)

    @app.get('/api/local/privacy/friend-feed/job')
    async def cleanup_status(request: Request):
        return {'job': await call(request, 'status', _cleanup=True)}

    @app.post('/api/local/privacy/friend-feed/job')
    async def cleanup_begin(request: Request):
        return await call(request, 'begin', review_token=await cleanup_body(request), _cleanup=True)

    @app.post('/api/local/privacy/friend-feed/job/{identifier}/resume')
    async def cleanup_resume(identifier: str, request: Request):
        return await call(request, 'resume', identifier, _cleanup=True)

    @app.get('/api/local/privacy/friend-feed/job/{identifier}/backups')
    async def cleanup_backups(identifier: str, request: Request):
        return await call(request, 'review_backups', identifier, _cleanup=True)

    @app.post('/api/local/privacy/friend-feed/job/{identifier}/backups')
    async def cleanup_approve(identifier: str, request: Request):
        return await call(request, 'approve_backups', identifier, review_token=await cleanup_body(request), _cleanup=True)

    @app.post('/api/local/friend-feed/publications/{identifier}/{action}')
    async def publication_change(identifier: str, action: str, request: Request):
        app.state.friend_feed.local_control(request)
        value = await body(request)
        common = {'expected_revision': value.get('expected_revision'), 'operation_id': value.get('operation_id')}
        if action == 'draft':
            return await call(request, 'save_draft', identifier, reference=value.get('reference'), audience=value.get('audience'), **common)
        if action == 'publish':
            return await call(request, 'publish', identifier, confirm_all_friends=value.get('confirm_all_friends', False), **common)
        if action == 'stop':
            return await call(request, 'stop', identifier, **common)
        raise HTTPException(404, detail='feed_action_unavailable')

    @app.get('/api/local/friend-feed')
    async def feed(request: Request):
        return {'subscriptions': await call(request, 'subscriptions'), 'timeline': await call(request, 'timeline')}

    @app.put('/api/local/friend-feed/subscriptions/{relationship_id}')
    async def subscribe(relationship_id: str, request: Request):
        app.state.friend_feed.local_control(request)
        value = await body(request)
        return await call(request, 'subscribe', relationship_id, enabled=value.get('enabled'), expected_revision=value.get('expected_revision'))

    @app.post('/api/local/friend-feed/subscriptions/{relationship_id}/refresh')
    async def refresh(relationship_id: str, request: Request):
        app.state.friend_feed.local_control(request)
        value = await body(request)
        return await call(request, 'refresh', relationship_id, cursor=value.get('cursor', ''))

    @app.post('/api/local/friend-feed/subscriptions/{relationship_id}/{identifier}/{action}')
    async def content_action(relationship_id: str, identifier: str, action: str, request: Request):
        app.state.friend_feed.local_control(request)
        value = await body(request)
        if action not in {'read', 'fetch'}:
            raise HTTPException(404, detail='feed_action_unavailable')
        return await call(request, 'mark_read' if action == 'read' else 'fetch', relationship_id, identifier,
                          expected_revision=value.get('expected_revision'))

    @app.post(PATH)
    async def peer_feed(request: Request):
        wire = await body(request)
        try:
            relationship = await asyncio.to_thread(current().friends().verify_request,
                path=PATH, body=canonical_json(wire), headers=request.headers)
            return await asyncio.to_thread(current().respond, wire, relationship)
        except Exception:
            raise HTTPException(403, detail='feed_request_rejected') from None

    return current()
