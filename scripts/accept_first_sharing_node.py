"""Run an unseeded node for browser invitation, sharing and revocation acceptance.

Only a clearly synthetic HTTP article/feed is provided as test input. No
relationship, invitation, message, card, reading history or receipt is seeded.
Peer request observations contain paths/statuses only, never secrets or bodies.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from accept_local_search import configure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--registry-url', required=True)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    home = args.home.resolve()
    if not home.name.startswith('rynmesh-first-sharing-acceptance-'):
        raise SystemExit('Use a dedicated first-sharing acceptance directory.')
    marker = home / '.first-sharing-fixture.json'
    if args.resume:
        previous = json.loads(marker.read_text(encoding='utf-8'))
        assert previous['kind'] == 'ryn.first-sharing-acceptance.v1'
        assert previous['port'] == args.port and previous['name'] == args.name
    elif home.exists():
        raise SystemExit('Fresh acceptance requires a new directory.')
    home.mkdir(parents=True, exist_ok=True)
    for key in list(os.environ):
        if key.startswith('RYNMESH_'):
            del os.environ[key]
    configure(home, args.port)
    endpoint = f'http://127.0.0.1:{args.port}'
    os.environ.update(RYNMESH_DEFAULT_DISCOVERY='0', RYNMESH_REGISTRY_URL=args.registry_url,
        RYNMESH_PEER_ENDPOINT=endpoint, RYNMESH_PEER_PORT=str(args.port),
        RYNMESH_LLM_HOME=str(home / 'llm'), RYNMESH_NETWORK_ID='first-sharing-acceptance')
    import uvicorn

    from rynmesh.atomic_io import atomic_write_json
    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    app = create_app(RynmeshStore(home=home, network_dir=home / 'network', node_name=args.name))
    observation_lock = threading.Lock()
    transport = app.state.friends.service.post_json

    def observed_transport(endpoint, path, payload, headers, **kwargs):
        with observation_lock, (home / 'outbound-observations.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'at': datetime.now(UTC).isoformat(), 'path': path}) + '\n')
        return transport(endpoint, path, payload, headers, **kwargs)

    app.state.friends.service.post_json = observed_transport

    @app.middleware('http')
    async def observe_peer(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith('/api/peer/friends/'):
            with observation_lock, (home / 'peer-observations.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'at': datetime.now(UTC).isoformat(),
                    'method': request.method, 'path': request.url.path,
                    'status': response.status_code}) + '\n')
        return response

    # A separate HTTP listener can answer while a synchronous feed fetch runs.
    source_endpoint = f'http://127.0.0.1:{args.port + 3}'
    article_body = ('<html><head><title>First sharing article</title></head><body><article>'
            '<p>A short synthetic article for the first sharing acceptance. Two friends can read this complete paragraph and save their own copies.</p>'
            '<p>共享正文验收：这段合成内容由页面正常打开和分享，不预先创建好友关系或卡片。</p></article></body></html>')

    feed_body = (f'<rss version="2.0"><channel><title>First sharing fixture</title><item>'
            f'<title>First sharing article</title><link>{source_endpoint}/acceptance/article</link>'
            '<guid>first-sharing-article</guid><description>A synthetic article for browser sharing acceptance.</description>'
            '</item></channel></rss>')

    class SourceHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ('/acceptance/feed', '/acceptance/article'):
                self.send_error(404)
                return
            is_feed = self.path.endswith('/feed')
            body = (feed_body if is_feed else article_body).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', ('application/rss+xml' if is_feed else 'text/html') + '; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    source_server = ThreadingHTTPServer(('127.0.0.1', args.port + 3), SourceHandler)
    threading.Thread(target=source_server.serve_forever, daemon=True).start()
    atomic_write_json(marker, {'kind': 'ryn.first-sharing-acceptance.v1', 'pid': os.getpid(),
        'name': args.name, 'port': args.port, 'started_at': datetime.now(UTC).isoformat()})
    try:
        uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False, log_level='warning')
    finally:
        source_server.shutdown()
        source_server.server_close()


if __name__ == '__main__':
    main()
