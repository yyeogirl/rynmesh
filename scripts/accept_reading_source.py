"""Serve a controllable synthetic source for real browser recovery acceptance.

Only the independent source fails; the Ryn node and its network stack are real.
In the dedicated fixture home, source-mode.txt may contain ok, unavailable or
invalid. No application data/configuration is edited by this source server.
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    args = parser.parse_args()
    home = args.home.resolve()
    assert home.name.startswith('rynmesh-first-reading-acceptance-')
    marker = json.loads((home / '.first-reading-fixture.json').read_text(encoding='utf-8'))
    assert marker['kind'] == 'ryn.first-reading-acceptance.v1'
    mode_file = home / 'source-mode.txt'

    class Source(BaseHTTPRequestHandler):
        def do_GET(self):
            mode = mode_file.read_text(encoding='utf-8').strip() if mode_file.exists() else 'ok'
            if mode == 'unavailable':
                self.send_error(503)
                return
            if mode == 'invalid':
                data = b'not a readable feed'
            elif self.path == '/feed':
                data = b'<rss version="2.0"><channel><title>Acceptance recovery source</title><link>http://127.0.0.1:18931</link><item><title>Recovery article</title><link>http://127.0.0.1:18931/article</link><guid>recovery-article</guid><description>Source recovery acceptance article.</description></item></channel></rss>'
            elif self.path == '/article':
                data = b'<html><head><title>Recovery article</title></head><body><article>' + b'<p>A saved article remains readable when another source fails. This is synthetic acceptance content.</p>' * 30 + b'</article></body></html>'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/rss+xml' if self.path == '/feed' else 'text/html')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 18931), Source) as server:
        print('Synthetic recovery source ready on port 18931', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
