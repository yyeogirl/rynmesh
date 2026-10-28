"""Read-only checkpoints around actual browser sharing/revocation acceptance."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

HOME = Path('D:/code/rynmesh-first-sharing-acceptance-invites-20260914-C')
OUTPUT = Path(__file__).resolve().parents[1] / 'docs/acceptance/search-development/revocation-checkpoints-20260914.json'
PEER = 'Hgrcmc6hOapgKiChYwrKSBIPeORSKNh2ZVkFFHveIEI='


def observe(label):
    data = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {
        'scope': 'Chrome and two existing independent Windows loopback nodes; real prior browser pairing',
        'checkpoints': [], 'packaged_desktop': False, 'public_nat': False}
    with httpx.Client(base_url='http://127.0.0.1:18972/api/local/', trust_env=False, timeout=20) as client:
        assert Path(client.get('privacy/status').json()['storage_root']).resolve() == HOME.resolve()
        row = {'label': label, 'at': datetime.now(UTC).isoformat(),
            'index_sha256': hashlib.sha256((HOME / 'local-search/index.json').read_bytes()).hexdigest(),
            'friends': [{key: friend.get(key) for key in ('peer_id', 'relationship_id', 'status')}
                for friend in client.get('friends').json()['friends']], 'queries': {}, 'opens': {}}
        for name, query, friend in [('protected_messages', 'friend-note', ''),
                ('article_metadata', 'First sharing article', ''),
                ('saved_body', '共享正文验收', ''), ('friend_filtered_body', '共享正文验收', PEER)]:
            response = client.post('search/query', json={'query': query, 'friend_id': friend})
            response.raise_for_status()
            value = response.json()
            row['queries'][name] = {'total': value['total'], 'partial': value['partial'],
                'index': {key: value['index'].get(key) for key in ('state', 'indexed_count', 'error_code')},
                'results': [{key: result.get(key) for key in ('id', 'kinds', 'body_state')} for result in value['results']]}
            for result in value['results']:
                opened = client.get('search/open', params={'identifier': result['id']})
                row['opens'][result['id']] = {'status': opened.status_code,
                    'body_sha256': hashlib.sha256(opened.json().get('text', '').encode()).hexdigest()}
        prior = next((point for point in data['checkpoints'] if point['label'] == 'before-revoke'), None)
        if prior:
            for identifier in prior['opens']:
                opened = client.get('search/open', params={'identifier': identifier})
                row['opens'][identifier] = {'status': opened.status_code,
                    'body_sha256': hashlib.sha256(opened.json().get('text', '').encode()).hexdigest()}
        row['cards'] = [{key: card.get(key) for key in ('card_id', 'dir', 'fetch_state', 'fetched_library_id', 'sha256_verified')}
            for card in client.get('friends/cards').json()['cards']]
    data['checkpoints'].append(row)
    OUTPUT.write_text(json.dumps(data, indent=2) + '\n')
    print(json.dumps({'label': label, 'queries': {key: value['total'] for key, value in row['queries'].items()},
        'open_statuses': [value['status'] for value in row['opens'].values()]}))


def verify():
    data = json.loads(OUTPUT.read_text())
    points = {row['label']: row for row in data['checkpoints']}
    metadata, before, frozen, recovered, restarted = [points[label] for label in
        ('metadata-only', 'before-revoke', 'revoked-frozen-index', 'recovered', 'restarted')]
    assert metadata['queries']['saved_body']['total'] == 0
    assert metadata['queries']['article_metadata']['total'] == 1
    protected = [row['id'] for row in before['queries']['protected_messages']['results']]
    saved = before['queries']['saved_body']['results'][0]['id']
    assert len(protected) == 2 and before['queries']['saved_body']['total'] == 1
    assert before['queries']['friend_filtered_body']['total'] == 1
    assert frozen['index_sha256'] == before['index_sha256']
    assert frozen['queries']['protected_messages']['total'] == 0
    for point in (frozen, recovered, restarted):
        assert point['queries']['friend_filtered_body']['total'] == 0
        assert all(point['opens'][identifier]['status'] == 409 for identifier in protected)
        assert point['opens'][saved] == before['opens'][saved]
    for point in (recovered, restarted):
        assert point['queries']['saved_body']['total'] == 1
        assert set(point['queries']['saved_body']['results'][0]['kinds']) == {'history', 'saved'}
        assert point['queries']['protected_messages']['total'] == 0
    print('Revoked results rejected with unchanged old index; independent saved body preserved through restart.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase')
    args = parser.parse_args()
    verify() if args.phase == 'verify' else observe(args.phase)
