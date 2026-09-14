"""Isolated browser acceptance with an explicitly synthetic HTTP model runtime.

The normal UI configures the service and submits every task. Fault modes are
selected by a dedicated local file, never by altering product API responses.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from accept_local_search import configure

HOME = Path('D:/code/rynmesh-ask-errors-acceptance-20260914')


def observe(label, *, timeout_evidence=False):
    import httpx
    with httpx.Client(base_url='http://127.0.0.1:18950/api/local/', trust_env=False, timeout=15) as client:
        def get(path):
            response = client.get(path)
            response.raise_for_status()
            return response.json()
        assert Path(get('privacy/status')['storage_root']).resolve() == HOME.resolve()
        rows = get('ask/conversations')['conversations']
        conversations = []
        for row in rows:
            conversation = get('ask/conversations/' + row['id'])
            messages = []
            for message in conversation['messages']:
                entry = {k: message.get(k) for k in ('id', 'role', 'status', 'taskId')}
                entry['content_sha256'] = hashlib.sha256(message.get('content', '').encode()).hexdigest()
                if message.get('taskId') and message['role'] == 'assistant':
                    entry['run'] = get('ask/runs/' + message['taskId'])
                messages.append(entry)
            conversations.append({'id': row['id'], 'messages': messages})
        orders = [{k: row.get(k) for k in ('task_id', 'state', 'error_code', 'amount')}
                  for row in get('llm/orders')['orders']]
        extra = {}
        if timeout_evidence:
            balance = get('task-balance')
            extra = {'at': datetime.now(UTC).isoformat(),
                'balance': {k: v for k, v in balance.items() if isinstance(v, (int, float))},
                'balance_events': [{k: event.get(k) for k in ('task_id', 'kind', 'amount')} for event in balance.get('events', [])],
                'provider_orders': [{'task_id': row['task_id'], 'state': row['state'],
                    'error_code': next((event['error_code'] for event in reversed(row.get('history') or []) if event.get('error_code')), None)}
                    for row in get('llm/provider-orders')['orders']]}
    checkpoint = {'label': label, **extra, 'conversations': conversations, 'orders': orders,
                  'runtime_calls': [json.loads(line) for line in (HOME / 'runtime-calls.jsonl').read_text().splitlines()]}
    filename = 'timeout-browser-checkpoints-20260914.json' if timeout_evidence else 'error-browser-checkpoints-20260914.json'
    path = Path(__file__).resolve().parents[1] / 'docs/acceptance/ask-ryn-development' / filename
    data = json.loads(path.read_text()) if path.exists() else {'scope': 'Synthetic HTTP faults; real browser, node stores and task protocol; loopback only', 'checkpoints': []}
    data['checkpoints'].append(checkpoint)
    path.write_text(json.dumps(data, indent=2) + '\n')
    print(json.dumps({'label': label, 'orders': orders, 'runtime_calls': len(checkpoint['runtime_calls'])}))


def runtime():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            mode = (HOME / 'mode.txt').read_text().strip()
            if mode == 'model_not_ready':
                return self.respond(503, {'detail': mode})
            self.respond(200, {'data': [{'id': 'synthetic-error-model'}]})

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            mode = (HOME / 'mode.txt').read_text().strip()
            with (HOME / 'runtime-calls.jsonl').open('a', encoding='utf-8') as output:
                output.write(json.dumps({'mode': mode}) + '\n')
            if mode in {'timeout', 'cancel_delay'}:
                from rynmesh.atomic_io import atomic_write_json
                started = time.monotonic()
                delay = 125 if mode == 'timeout' else 45
                receipt = HOME / ('timeout-runtime.json' if mode == 'timeout' else 'cancel-runtime.json')
                value = {'state': 'waiting', 'started_at': datetime.now(UTC).isoformat(), 'delay_seconds': delay}
                atomic_write_json(receipt, value)
                time.sleep(delay)
                delivered = self.respond(200, {'choices': [{'message': {'content': 'Synthetic delayed response.'}}]})
                atomic_write_json(receipt, {**value, 'state': 'response_attempted',
                    'elapsed_seconds': round(time.monotonic() - started, 3), 'socket_write_succeeded': delivered})
                return
            if mode in {'runtime_busy', 'runtime_unavailable', 'model_not_found'}:
                return self.respond(404 if mode == 'model_not_found' else 503, {'detail': mode})
            self.respond(200, {'choices': [{'message': {'content': 'Synthetic recovery response.'}}],
                               'usage': {'prompt_tokens': 10, 'completion_tokens': 5}})

        def respond(self, status, value):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
                return True
            except OSError:
                return False

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 18951), Handler)
    print(json.dumps({'kind': 'synthetic-runtime', 'pid': os.getpid(), 'port': 18951}), flush=True)
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('surface', choices=['node', 'runtime', 'observe'])
    parser.add_argument('--label', default='checkpoint')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--timeout-evidence', action='store_true')
    args = parser.parse_args()
    marker = HOME / '.ask-errors-fixture.json'
    if args.surface in {'runtime', 'observe'} or args.resume:
        assert json.loads(marker.read_text())['kind'] == 'ryn.ask-errors.v1'
    else:
        HOME.mkdir(exist_ok=False)
        marker.write_text(json.dumps({'kind': 'ryn.ask-errors.v1'}))
        (HOME / 'mode.txt').write_text('ready')
    if args.surface == 'runtime':
        return runtime()
    if args.surface == 'observe':
        return observe(args.label, timeout_evidence=args.timeout_evidence)
    configure(HOME, 18950)
    os.environ['RYNMESH_LLM_HOME'] = str(HOME / 'llm')
    import uvicorn

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore
    app = create_app(RynmeshStore(home=HOME, network_dir=HOME / 'network', node_name='Synthetic error acceptance'))
    marker.write_text(json.dumps({'kind': 'ryn.ask-errors.v1', 'node_pid': os.getpid()}))
    uvicorn.run(app, host='127.0.0.1', port=18950, access_log=False)


if __name__ == '__main__':
    main()
