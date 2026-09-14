"""Record bounded, read-only evidence after actual browser sharing actions."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import httpx


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', required=True)
    parser.add_argument('--offline', choices=('A', 'B'), action='append', default=[])
    parser.add_argument('--private-fetch-status', type=int, choices=(200, 403))
    args = parser.parse_args()
    observation = {'label': args.label, 'at': datetime.now(UTC).isoformat(), 'nodes': {}}
    for name, port in (('A', 18940), ('B', 18941)):
        home = Path(f'D:/code/rynmesh-first-sharing-acceptance-20260914-{name}')
        marker = json.loads((home / '.first-sharing-fixture.json').read_text(encoding='utf-8'))
        assert marker['kind'] == 'ryn.first-sharing-acceptance.v1' and marker['port'] == port
        row = {'pid': marker['pid']}
        for kind in ('peer', 'outbound'):
            path = home / f'{kind}-observations.jsonl'
            records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []
            row[kind + '_requests'] = dict(Counter(record['path'] for record in records))
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=5, trust_env=False) as client:
            try:
                health = client.get('/health')
                health.raise_for_status()
                assert name not in args.offline
            except httpx.ConnectError:
                assert name in args.offline
                row['online'] = False
                observation['nodes'][name] = row
                continue

            def read(path):
                response = client.get('/api/local/' + path)
                response.raise_for_status()
                return response.json()

            row['online'] = True
            assert Path(read('privacy/status')['storage_root']).resolve() == home.resolve()
            friends = read('friends')['friends']
            row['friends'] = [{k: friend.get(k) for k in ('relationship_id', 'peer_id', 'permissions',
                'status', 'created_at', 'revoked_at', 'revocation_delivery')} for friend in friends]
            row['invites'] = [{k: invite.get(k) for k in ('invite_id', 'status', 'created_at', 'expires_at')}
                             for invite in read('friends/invites')['invites']]
            row['messages'] = []
            for friend in friends:
                result = client.get('/api/local/friends/' + quote(friend['peer_id'], safe='') + '/messages')
                result.raise_for_status()
                for message in result.json()['messages']:
                    row['messages'].append({**{k: message.get(k) for k in ('msg_id', 'dir', 'kind', 'ts',
                        'delivered', 'delivery_state', 'error', 'expires_at')}, 'text_digest': digest(message.get('text', ''))})
            row['cards'] = []
            for card in read('friends/cards')['cards']:
                saved = {k: card.get(k) for k in ('card_id', 'dir', 'created_at', 'fetch_state',
                    'fetched_library_id', 'sha256_verified', 'delivered', 'delivery_state', 'error')}
                if card.get('fetched_library_id'):
                    key = card['fetched_library_id'].removeprefix('import:')
                    body = read('friends/documents/' + quote(key, safe='') + '/body')
                    saved['body_digest'] = digest(body['text'])
                row['cards'].append(saved)
            row['document_count'] = len(read('friends/documents')['documents'])
            row['reading_count'] = len(read('consumption'))
            row['model_configured'] = read('llm/service/status').get('configured', False)
        observation['nodes'][name] = row
    path = Path(__file__).resolve().parents[1] / 'docs/acceptance/friends-development/browser-checkpoints-20260914.json'
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'scope': 'Fresh unseeded nodes; real loopback browser operations; synthetic article source; not packaged desktop or NAT',
        'observations': []}
    if args.private_fetch_status is not None:
        from friend_product_e2e import protected_fetch_status

        from rynmesh.friends.store import FriendStore

        baseline = next(item for item in data['observations'] if item['label'] == 'metadata-before-download')
        alice = baseline['nodes']['A']
        bob = baseline['nodes']['B']
        relation_id = bob['friends'][0]['relationship_id']
        secret = FriendStore('D:/code/rynmesh-first-sharing-acceptance-20260914-B').secret(relation_id)
        assert secret, 'Use the existing isolated recipient credentials before it processes revocation.'
        status = protected_fetch_status(
            SimpleNamespace(peer_id=alice['friends'][0]['peer_id']),
            SimpleNamespace(peer_id=bob['friends'][0]['peer_id'], url='http://127.0.0.1:18940'),
            relation_id, secret, bob['cards'][0]['card_id'])
        observation['private_fetch_status'] = status
        assert status == args.private_fetch_status
    data['observations'].append(observation)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(observation))


if __name__ == '__main__':
    main()
