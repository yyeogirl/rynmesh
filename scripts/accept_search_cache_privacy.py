"""Verify search privacy headers and deletion over real loopback HTTP.

Creates only a new dedicated synthetic home. Does not claim browser cache
eviction, packaged-desktop acceptance or a complete traffic/privacy audit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn
from accept_local_search import configure, create


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    home = args.home.resolve()
    if home.exists() or not home.name.startswith('rynmesh-search-privacy-'):
        raise SystemExit('Use a new dedicated rynmesh-search-privacy- directory.')
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    configure(home, port)
    app = create(home)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    records = []
    marker = 'private-search-cache-acceptance-20260914'
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError('acceptance_node_not_started')
            time.sleep(.05)
        with httpx.Client(base_url=f'http://127.0.0.1:{port}/api/local/', trust_env=False, timeout=20) as client:
            stamp = datetime.now(UTC).isoformat()
            conversation = {'id': 'cache-privacy-acceptance', 'title': 'Synthetic privacy case',
                'serviceKey': 'acceptance::unavailable', 'serviceName': 'Synthetic unavailable model',
                'providerPeerId': 'acceptance', 'networkId': 'acceptance',
                'createdAt': stamp, 'updatedAt': stamp,
                'messages': [{'id': 'case-message', 'role': 'user', 'content': marker,
                    'status': 'complete', 'createdAt': stamp}]}
            saved = client.put('ask/conversations/' + conversation['id'],
                json={'conversation': conversation, 'expected_revision': 0})
            saved.raise_for_status()

            def check(label, response, expected):
                assert response.status_code == expected, (label, response.status_code)
                assert response.headers.get('cache-control') == 'no-store', label
                records.append({'case': label, 'status': response.status_code,
                    'cache_control': response.headers['cache-control'],
                    'response_sha256': hashlib.sha256(response.content).hexdigest()})
                return response

            check('rebuild', client.post('search/rebuild'), 200)
            result = check('query', client.post('search/query', json={'query': marker}), 200)
            assert result.json()['total'] == 1
            identifier = result.json()['results'][0]['id']
            body = check('open', client.get('search/open', params={'identifier': identifier}), 200)
            assert body.json()['text'] == marker
            check('status', client.get('search/status'), 200)
            check('owner_denied', client.get('search/open', params={'identifier': identifier},
                headers={'X-Forwarded-For': '198.51.100.88'}), 401)
            check('bad_query', client.post('search/query', json={}), 400)
            check('missing_identifier', client.get('search/open'), 422)
            deleted = client.request('DELETE', 'ask/conversations/' + conversation['id'],
                json={'expected_revision': saved.json()['revision']})
            deleted.raise_for_status()
            check('open_after_delete', client.get('search/open', params={'identifier': identifier}), 409)
            empty = check('query_after_delete', client.post('search/query', json={'query': marker}), 200)
            assert empty.json()['total'] == 0
            assert client.get('ask/conversations').json()['conversations'] == []
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        listener.close()
    assert not thread.is_alive(), 'acceptance_node_did_not_stop'
    output = {'recorded_at': datetime.now(UTC).isoformat(),
        'scope': 'Windows production app, synthetic conversation created/deleted through Owner TCP API',
        'responses': records, 'initial_matches': 1, 'deleted_matches': 0,
        'node_stopped': True, 'browser_cache_measured': False,
        'packaged_desktop': False, 'complete_privacy_audit': False}
    args.output.write_text(json.dumps(output, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'verified_responses': len(records), 'node_stopped': True}))


if __name__ == '__main__':
    main()
