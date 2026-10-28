"""Isolate feed network failures through a loopback HTTPS CONNECT proxy.

Set HTTPS_PROXY only in the acceptance node process. This never changes the
machine network or application stores. mode.txt contains offline (default) or
online. Online tunnels preserve the origin's TLS; no content is fabricated.
"""
from __future__ import annotations

import argparse
import json
import select
import socket
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--control', type=Path, required=True)
    args = parser.parse_args()
    control = args.control.resolve()
    if not control.name.startswith('rynmesh-reading-network-acceptance-'):
        raise SystemExit('Use a dedicated network acceptance control directory.')
    control.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()

    def online():
        path = control / 'mode.txt'
        return path.exists() and path.read_text(encoding='utf-8').strip() == 'online'

    def record(host, result):
        with lock, (control / 'connections.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'at': datetime.now(UTC).isoformat(),
                'host': host, 'result': result}) + '\n')

    class Proxy(BaseHTTPRequestHandler):
        def do_CONNECT(self):
            host, _, port = self.path.rpartition(':')
            if not host or port != '443':
                self.send_error(403)
                return
            if not online():
                record(host, 'blocked')
                self.send_error(503)
                return
            try:
                upstream = socket.create_connection((host, 443), timeout=10)
            except OSError:
                record(host, 'connect_failed')
                self.send_error(502)
                return
            record(host, 'tunnel')
            with upstream:
                self.send_response(200, 'Connection established')
                self.end_headers()
                sockets = [self.connection, upstream]
                try:
                    while online():
                        ready, _, _ = select.select(sockets, [], [], 1)
                        for source in ready:
                            data = source.recv(65536)
                            if not data:
                                return
                            target = upstream if source is self.connection else self.connection
                            target.sendall(data)
                except OSError:
                    return

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 18932), Proxy) as server:
        print('Acceptance HTTPS proxy ready on port 18932', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
