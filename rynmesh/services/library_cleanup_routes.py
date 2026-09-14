"""Owner-only reviewed private-document file maintenance; no worker owned."""
import asyncio

from fastapi import HTTPException, Request

from .library_cleanup import LibraryCleanup
from .library_imports import LibraryImportError


def install_library_cleanup_routes(app):
    if any(getattr(route, 'name', '') == 'library_cleanup_preview' for route in app.routes):
        return

    async def call(request, method, *args, **kwargs):
        app.state.friends.local_control(request)
        try:
            return await asyncio.to_thread(getattr(LibraryCleanup(app.state.friends.content.imports), method), *args, **kwargs)
        except LibraryImportError as exc:
            raise HTTPException(409, detail=exc.code) from None
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(503, detail='library_cleanup_unavailable') from None

    async def body(request, keys):
        from ..friends.routes import _body
        app.state.friends.local_control(request)
        value = await _body(request, 8192)
        if set(value) != set(keys):
            raise HTTPException(400, detail='library_cleanup_request_invalid')
        return value

    @app.post('/api/local/privacy/documents/preview', name='library_cleanup_preview')
    async def preview(request: Request):
        value = await body(request, ['scope'])
        return await call(request, 'preview', value['scope'])

    @app.get('/api/local/privacy/documents/job')
    async def status(request: Request):
        return {'job': await call(request, 'status')}

    @app.post('/api/local/privacy/documents/job')
    async def begin(request: Request):
        return await call(request, 'begin', **await body(request, ['scope', 'review_token']))

    @app.post('/api/local/privacy/documents/job/{identifier}/resume')
    async def resume(identifier: str, request: Request):
        return await call(request, 'resume', identifier)

    @app.get('/api/local/privacy/documents/job/{identifier}/files')
    async def review_files(identifier: str, request: Request):
        return await call(request, 'review_files', identifier)

    @app.post('/api/local/privacy/documents/job/{identifier}/files')
    async def approve_files(identifier: str, request: Request):
        return await call(request, 'approve_files', identifier, **await body(request, ['review_token']))
