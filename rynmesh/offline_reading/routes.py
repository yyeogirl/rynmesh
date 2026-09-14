"""Owner controls for explicit downloads; the shared registry owns execution."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass

from fastapi import HTTPException, Request, Response

from ..background_workers import BackgroundWorkerSpec, BackoffPolicy
from .fetch import OfflineError, fetch_resource
from .service import OfflineReading, OfflineSources
from .store import OfflineStore


@dataclass
class OfflineState:
    service: OfflineReading
    local_control: object


async def body(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 8192:
            raise HTTPException(413, detail='offline_request_too_large')
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, detail='offline_request_invalid') from None


def install_offline_reading(app, *, home, messaging_key, consumption, imports, native, local_control, workers, fetch=None):
    # Loopback is an explicit development/acceptance opt-in, never a property of
    # an untrusted page or remote image URL.
    allow_loopback = os.environ.get('RYNMESH_OFFLINE_ALLOW_LOOPBACK') == '1'
    def download(url, **options):
        return fetch_resource(url, allow_loopback=allow_loopback, **options)
    service = OfflineReading(store=OfflineStore(home, messaging_key=messaging_key),
        sources=OfflineSources(consumption=consumption, imports=imports, native=native, fetch=fetch or download))
    app.state.offline_reading = OfflineState(service, local_control)
    # Damaged data should affect this feature rather than node startup.
    try:
        service.recover()
    except (OSError, ValueError):
        pass
    workers.register(BackgroundWorkerSpec(name='offline-reading.download', initial_delay_s=1,
        run_once=lambda: app.state.offline_reading.service.run_once(), policy=BackoffPolicy.fixed(1)), replace=True)
    if any(getattr(route, 'name', '') == 'offline_reading_status' for route in app.routes):
        return service

    async def call(request, method, *args, **kwargs):
        app.state.offline_reading.local_control(request)
        try:
            return await asyncio.to_thread(getattr(app.state.offline_reading.service, method), *args, **kwargs)
        except OfflineError as exc:
            raise HTTPException(409, detail=str(exc)) from None
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            raise HTTPException(503, detail='offline_operation_unavailable') from None

    @app.get('/api/local/offline-reading', name='offline_reading_status')
    async def status(request: Request):
        return await call(request, 'status')

    @app.post('/api/local/offline-reading/{action}')
    async def action(action: str, request: Request):
        app.state.offline_reading.local_control(request)
        value = await body(request)
        if action == 'download':
            return await call(request, 'request', value.get('item_id'), update=value.get('update', False))
        if action in {'retry', 'cancel', 'body', 'resolve'}:
            return await call(request, 'read' if action == 'body' else action, value.get('item_id'))
        if action == 'clear-preview':
            return await call(request, 'clear_preview', value.get('item_id'))
        if action == 'clear':
            return await call(request, 'clear', item_id=value.get('item_id'), review_token=value.get('review_token'))
        if action == 'clear-remaining-preview':
            return await call(request, 'clear_remaining_preview')
        if action == 'clear-remaining':
            return await call(request, 'clear_remaining', review_token=value.get('review_token'))
        raise HTTPException(404, detail='offline_action_unavailable')

    @app.get('/api/local/offline-reading/copies/{key}/{job_id}/images/{index}')
    async def image(key: str, job_id: str, index: int, request: Request):
        app.state.offline_reading.local_control(request)
        records = await call(request, 'status')
        item = next((row for row in records['records'] if row['key'] == key), None)
        if not item:
            raise HTTPException(404, detail='offline_image_unavailable')
        data, mime = await call(request, 'image', item['item_id'], index, job_id=job_id)
        return Response(data, media_type=mime, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    return service
