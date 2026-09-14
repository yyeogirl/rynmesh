"""TCP search timing and change recovery against a running dedicated scale node.

First create that synthetic node with accept_local_search.py --serve. This
probe creates one synthetic conversation and removes it even if a check fails.
It does not constitute packaged-desktop or network-isolation acceptance.
"""
from __future__ import annotations

import argparse
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from rynmesh.atomic_io import atomic_write_json


def measure(client):
    def query(term):
        response = client.post('/api/local/search/query', json={'query': term})
        response.raise_for_status()
        return response.json()

    baseline = query('Python')
    assert baseline['total'] == 10_000
    assert any(row['id'].startswith('content:scale-article-') for row in baseline['results'])
    durations, partial = [], 0
    for term in ['城市', 'Python', '中文检索', '阅读 guide', 'testing'] * 4:
        started = time.perf_counter()
        result = query(term)
        durations.append(time.perf_counter() - started)
        assert result['total'] == 10_000 and len(result['results']) == 20
        partial += int(result['partial'])
    cid = 'flow-' + uuid.uuid4().hex
    first, second = 'create-' + uuid.uuid4().hex, 'rename-' + uuid.uuid4().hex
    stamp = datetime.now(timezone.utc).isoformat()
    row = {'id': cid, 'title': first, 'serviceKey': 'scale-provider::scale-model',
           'serviceName': 'Scale model', 'providerPeerId': 'scale-provider', 'networkId': 'scale',
           'createdAt': stamp, 'updatedAt': stamp,
           'messages': [{'id': 'flow-message', 'role': 'user', 'content': 'Synthetic change acceptance',
                         'createdAt': stamp, 'status': 'complete'}]}
    revision = None
    endpoint = '/api/local/ask/conversations/' + cid

    def wait_match(term, started):
        while time.perf_counter() - started < 20:
            if query(term)['total'] == 1:
                return time.perf_counter() - started
            time.sleep(.1)
        raise AssertionError('Index did not update within the observation window')

    try:
        response = client.put(endpoint, json={'conversation': row, 'expected_revision': 0})
        response.raise_for_status()
        revision = response.json()['revision']
        created = wait_match(first, time.perf_counter())
        row['title'] = second
        response = client.put(endpoint, json={'conversation': row, 'expected_revision': revision})
        response.raise_for_status()
        revision = response.json()['revision']
        started = time.perf_counter()
        old_hidden = query(first)['total'] == 0
        renamed = wait_match(second, started)
        response = client.request('DELETE', endpoint, json={'expected_revision': revision})
        response.raise_for_status()
        revision = None
        deleted_hidden = query(second)['total'] == 0
        opened = client.get('/api/local/search/open', params={'identifier': f'ask:{cid}:flow-message'})
        assert old_hidden and deleted_hidden and opened.status_code == 409
        assert opened.json()['detail'] == 'search_result_unavailable'
        return {'recorded_at': datetime.now(timezone.utc).isoformat(),
                'scope': 'Actual loopback TCP HTTP, real 10k node stores and automatic worker; no browser paint or packaged-desktop timing',
                'query_seconds': durations, 'query_p95_seconds': sorted(durations)[18],
                'partial_responses': partial, 'record_count': 10_000,
                'create_visible_seconds': created, 'rename_visible_seconds': renamed,
                'old_title_hidden_on_first_query': old_hidden, 'deleted_title_hidden_on_first_query': deleted_hidden,
                'deleted_result_open_status': opened.status_code, 'deleted_result_open_error': opened.json()['detail'],
                'temporary_conversation_deleted': True}
    finally:
        if revision is not None:
            client.request('DELETE', endpoint, json={'expected_revision': revision}).raise_for_status()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18852)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with httpx.Client(base_url=f'http://127.0.0.1:{args.port}', timeout=20) as client:
        result = measure(client)
    atomic_write_json(args.output, result)
    print({key: result[key] for key in ('query_p95_seconds', 'create_visible_seconds', 'rename_visible_seconds')})


if __name__ == '__main__':
    main()
