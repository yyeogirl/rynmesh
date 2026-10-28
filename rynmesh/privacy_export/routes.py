"""Owner-only selected export. The response owns and closes the temporary ZIP."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from fastapi import HTTPException, Request
from starlette.responses import StreamingResponse

from .archive import EXCLUDED, SCOPES, ExportBuilder, ExportError
from .sources import ProductDataSources


@dataclass
class ExportState:
    builder: ExportBuilder
    local_control: object


class ArchiveResponse(StreamingResponse):
    def __init__(self, artifact):
        self.artifact = artifact
        super().__init__(artifact.chunks(), media_type='application/zip', headers={
            'Content-Disposition': 'attachment; filename="ryn-product-data.zip"',
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "sandbox; default-src 'none'",
        })

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.artifact.close()


def install_privacy_export(app, *, local_control, sources=None):
    prior = getattr(app.state, 'privacy_export', None)
    builder = prior.builder if prior else ExportBuilder(sources or ProductDataSources(app))
    builder.sources = sources or ProductDataSources(app)
    app.state.privacy_export = ExportState(builder, local_control)
    if any(getattr(route, 'name', '') == 'product_export_scopes' for route in app.routes):
        return app.state.privacy_export

    @app.get('/api/local/privacy/export/scopes', name='product_export_scopes')
    async def scopes(request: Request):
        app.state.privacy_export.local_control(request)
        return {'scopes': [{'id': key, 'label': value} for key, value in SCOPES.items()], 'excluded': EXCLUDED}

    @app.post('/api/local/privacy/export/archive')
    async def archive(request: Request):
        state = app.state.privacy_export
        state.local_control(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8192:
                raise HTTPException(413, detail={'code': 'privacy_export_request_too_large'})
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or set(body) != {'scopes'}:
                raise ValueError
            selected = state.builder.validate(body['scopes'])
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, detail={'code': 'privacy_export_scopes_invalid'}) from None
        task = asyncio.create_task(asyncio.to_thread(state.builder.build, selected))
        try:
            artifact = await asyncio.shield(task)
        except asyncio.CancelledError:
            # A disconnected request cannot abandon a still-running builder or
            # leak its archive and busy lock after the worker completes.
            def close_finished(future):
                try:
                    future.result().close()
                except BaseException:
                    pass
            task.add_done_callback(close_finished)
            raise
        except ExportError as exc:
            status = 409 if exc.code in {'privacy_export_busy', 'privacy_export_source_changed'} else (
                413 if exc.code == 'privacy_export_capacity_exhausted' else 503)
            raise HTTPException(status, detail={'code': exc.code, 'scope': exc.scope}) from None
        except Exception:
            raise HTTPException(503, detail={'code': 'privacy_export_unavailable'}) from None
        return ArchiveResponse(artifact)

    return app.state.privacy_export
