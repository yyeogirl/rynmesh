"""Owner-only, bounded reading cleanup routes with current-state dependencies."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from fastapi import HTTPException, Request

from ..device_sync.records import SyncError
from ..local_search.index import SearchError
from .consumption import ConsumptionError
from .reading_cleanup import ReadingCleanup

SAFE_ERRORS = frozenset({
    'reading_cleanup_version_unsupported', 'reading_cleanup_unreadable', 'reading_cleanup_pending',
    'reading_cleanup_limit', 'reading_cleanup_backup_changed', 'reading_cleanup_backup_limit',
    'reading_cleanup_not_found', 'reading_cleanup_already_started', 'reading_cleanup_backup_review_unavailable',
    'reading_privacy_review_changed', 'reading_privacy_version_unsupported', 'reading_privacy_receipt_invalid',
    'reading_privacy_limit', 'consumption_backup_failed', 'sync_version_unsupported',
    'sync_device_identity_changed', 'sync_capacity_exhausted', 'search_index_busy',
    'search_index_version_unsupported', 'search_source_unavailable',
})


@dataclass
class CleanupState:
    factory: object
    local_control: object


def install_reading_cleanup(app, *, store, home, workers, local_control, factory=None):
    def current():
        return ReadingCleanup(getattr(store, 'home', None) or home,
            source=app.state.consumption_store, replica=app.state.device_sync.transfer.replica,
            pairing_lock=app.state.device_sync.service.store.lock, search=app.state.local_search.index)
    app.state.reading_cleanup = CleanupState(factory or current, local_control)
    if any(getattr(route, 'name', '') == 'reading_cleanup_preview' for route in app.routes):
        return app.state.reading_cleanup

    async def call(request, method, *args, **kwargs):
        state = app.state.reading_cleanup
        state.local_control(request)
        try:
            return await asyncio.to_thread(lambda: getattr(state.factory(), method)(*args, **kwargs))
        except (ConsumptionError, SyncError, SearchError) as exc:
            code = str(exc)
            raise HTTPException(409 if code in SAFE_ERRORS else 503,
                                detail=code if code in SAFE_ERRORS else 'reading_cleanup_unavailable') from None
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise HTTPException(503, detail='reading_cleanup_unavailable') from None

    async def body(request):
        app.state.reading_cleanup.local_control(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8192:
                raise HTTPException(413, detail='reading_cleanup_request_too_large')
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {'review_token'}:
                raise ValueError
            return value['review_token']
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, detail='reading_cleanup_request_invalid') from None

    @app.get('/api/local/privacy/reading/preview', name='reading_cleanup_preview')
    async def preview(request: Request):
        return await call(request, 'preview')

    @app.get('/api/local/privacy/reading/job')
    async def status(request: Request):
        return {'job': await call(request, 'status')}

    @app.post('/api/local/privacy/reading/job')
    async def begin(request: Request):
        return await call(request, 'begin', review_token=await body(request))

    @app.post('/api/local/privacy/reading/job/{identifier}/resume')
    async def resume(identifier: str, request: Request):
        return await call(request, 'resume', identifier)

    @app.post('/api/local/privacy/reading/job/{identifier}/cancel')
    async def cancel(identifier: str, request: Request):
        return await call(request, 'cancel_uncommitted', identifier)

    @app.get('/api/local/privacy/reading/job/{identifier}/backups')
    async def backups(identifier: str, request: Request):
        return await call(request, 'review_backups', identifier)

    @app.post('/api/local/privacy/reading/job/{identifier}/backups')
    async def approve(identifier: str, request: Request):
        return await call(request, 'approve_backups', identifier, review_token=await body(request))

    return app.state.reading_cleanup
