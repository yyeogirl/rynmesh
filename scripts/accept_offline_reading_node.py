"""Isolated node and optional synthetic web origin for offline browser checks.

Download with --source, then stop and restart with --reuse without --source.
The original article and images are then unreachable; the saved copy must open.
This is loopback browser evidence, not packaged desktop or cross-NAT evidence.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from accept_local_search import configure


def picture(width=8, height=8):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress((b'\x00' + b'\x40\x80\xc0' * width) * height)) + chunk(b'IEND', b''))


class Origin(BaseHTTPRequestHandler):
    image_size = (8, 8)
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/article':
            paragraphs = ''.join(f'<p>Section {i}. Offlinegardencheckpoint 离线花园阅读。 ' +
                'This synthetic paragraph checks reading progress after a restart. ' * 12 + '</p>' for i in range(12))
            data = ('<html><title>Offline garden journal</title><body><article>' + paragraphs +
                    '<img src="/picture.png" alt="Saved blue square"><img src="/missing.png" alt="Unavailable illustration">'
                    '</article></body></html>').encode()
            status, mime = 200, 'text/html; charset=utf-8'
        elif self.path == '/picture.png':
            data, status, mime = picture(*self.image_size), 200, 'image/png'
        else:
            data, status, mime = b'', 404, 'text/plain'
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18870)
    parser.add_argument('--source-port', type=int, default=18871)
    parser.add_argument('--source', action='store_true')
    parser.add_argument('--reuse', action='store_true')
    parser.add_argument('--tall-image', action='store_true', help='Use a large synthetic image to expose layout-dependent position errors')
    parser.add_argument('--image-read-delay', type=float, default=0, help='Delay only local saved-image responses for browser layout checks (0–5 seconds)')
    args = parser.parse_args()
    if not 0 <= args.image_read_delay <= 5:
        raise SystemExit('Image delay must be between 0 and 5 seconds.')
    home = args.home.resolve()
    if not home.name.startswith('rynmesh-offline-acceptance-') or home.exists() != args.reuse:
        raise SystemExit('Use a new rynmesh-offline-acceptance-* directory, or explicitly --reuse it after stopping the node.')
    configure(home, args.port)
    os.environ['RYNMESH_OFFLINE_ALLOW_LOOPBACK'] = '1'
    import uvicorn

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore
    app = create_app(RynmeshStore(home=home, network_dir=home / 'network', node_name='Offline reading acceptance'))
    if args.image_read_delay:
        @app.middleware('http')
        async def delay_saved_image(request, call_next):
            if request.url.path.startswith('/api/local/offline-reading/copies/') and '/images/' in request.url.path:
                await asyncio.sleep(args.image_read_delay)
            return await call_next(request)
    if not args.reuse:
        app.state.consumption_store.record({'item_id': 'offline-garden', 'title': 'Offline garden journal',
            'source_title': 'Synthetic garden notebook', 'content_kind': 'document',
            'link': f'http://127.0.0.1:{args.source_port}/article'}, 'bookmark')
    app.state.first_run.store.dismiss()
    Origin.image_size = (800, 1600) if args.tall_image else (8, 8)
    source = ThreadingHTTPServer(('127.0.0.1', args.source_port), Origin) if args.source else None
    if source:
        threading.Thread(target=source.serve_forever, daemon=True).start()
    try:
        uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)
    finally:
        if source:
            source.shutdown()
            source.server_close()


if __name__ == '__main__':
    main()
