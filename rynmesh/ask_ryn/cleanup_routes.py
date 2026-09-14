"""Owner-only reviewed cleanup, separate from ordinary conversation deletion."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from fastapi import HTTPException, Request

from ..device_sync.records import SyncError
from ..llm_package.task_protocol import TaskProtocolError
from ..local_search.index import SearchError
from .cleanup import ConversationCleanup
from .store import ConversationError

SAFE_ERRORS = frozenset({
    'ask_privacy_review_changed', 'ask_privacy_tasks_active', 'ask_cleanup_pending', 'ask_cleanup_limit',
    'ask_cleanup_not_found', 'ask_cleanup_backup_changed', 'ask_cleanup_backup_limit',
    'ask_cleanup_already_started', 'ask_cleanup_backup_review_unavailable', 'ask_cleanup_orders_active',
    'ask_cleanup_orders_unavailable', 'ask_cleanup_version_unsupported', 'ask_cleanup_unreadable',
    'ask_cleanup_backup_failed',
    'ask_history_limit', 'ask_history_version_unsupported', 'search_index_busy',
    'search_index_version_unsupported', 'search_source_unavailable',
})


@dataclass
class CleanupState:
    factory: object
    local_control: object


def install_conversation_cleanup(app, *, store, home, workers, local_control, factory=None):
    def current():
        def erase_orders(ids):
            commands = app.state.ask_ryn.orders
            if commands is None or commands.erase_results is None:
                raise ConversationError('ask_cleanup_orders_unavailable')
            return commands.erase_results(ids)
        return ConversationCleanup(getattr(store, 'home', None) or home,
            source=app.state.ask_ryn.conversations, replica=app.state.device_sync.transfer.replica,
            pairing_lock=app.state.device_sync.service.store.lock, search=app.state.local_search.index,
            erase_order_results=erase_orders)
    app.state.conversation_cleanup = CleanupState(factory or current, local_control)
    if any(getattr(route, 'name', '') == 'conversation_cleanup_preview' for route in app.routes):
        return app.state.conversation_cleanup

    async def call(request, method, *args, **kwargs):
        state = app.state.conversation_cleanup
        state.local_control(request)
        try:
            return await asyncio.to_thread(lambda: getattr(state.factory(), method)(*args, **kwargs))
        except (ConversationError, SyncError, SearchError, TaskProtocolError) as exc:
            code = str(exc)
            raise HTTPException(409 if code in SAFE_ERRORS else 503,
                                detail=code if code in SAFE_ERRORS else 'ask_cleanup_unavailable') from None
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise HTTPException(503, detail='ask_cleanup_unavailable') from None

    async def body(request):
        app.state.conversation_cleanup.local_control(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8192:
                raise HTTPException(413, detail='ask_cleanup_request_too_large')
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {'review_token'}:
                raise ValueError
            return value['review_token']
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, detail='ask_cleanup_request_invalid') from None

    @app.get('/api/local/privacy/conversations/preview', name='conversation_cleanup_preview')
    async def preview(request: Request):
        return await call(request, 'preview')

    @app.get('/api/local/privacy/conversations/jobs')
    async def jobs(request: Request):
        return {'jobs': await call(request, 'list_status')}

    @app.post('/api/local/privacy/conversations/jobs')
    async def begin(request: Request):
        return await call(request, 'begin', review_token=await body(request))

    @app.get('/api/local/privacy/conversations/jobs/{identifier}')
    async def status(identifier: str, request: Request):
        return await call(request, 'status', identifier)

    @app.post('/api/local/privacy/conversations/jobs/{identifier}/resume')
    async def resume(identifier: str, request: Request):
        return await call(request, 'resume', identifier)

    @app.post('/api/local/privacy/conversations/jobs/{identifier}/cancel')
    async def cancel(identifier: str, request: Request):
        return await call(request, 'cancel_uncommitted', identifier)

    @app.get('/api/local/privacy/conversations/jobs/{identifier}/backups')
    async def backups(identifier: str, request: Request):
        return await call(request, 'review_backups', identifier)

    @app.post('/api/local/privacy/conversations/jobs/{identifier}/backups')
    async def approve_backups(identifier: str, request: Request):
        return await call(request, 'approve_backups', identifier, review_token=await body(request))

    return app.state.conversation_cleanup
