"""Prepare/verify synthetic three-node feed cleanup around the actual Settings UI."""
from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

PREFIX = '/api/local/privacy/friend-feed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=['prepare', 'verify'], required=True)
    parser.add_argument('--home', type=Path, required=True, help='Dedicated author home')
    args = parser.parse_args()
    home = args.home.resolve()
    if home.name != 'rynmesh-feed-acceptance-cleanup-author':
        raise SystemExit('Use the dedicated cleanup-author acceptance home.')
    clients = [httpx.Client(base_url=f'http://127.0.0.1:{18920 + i}', timeout=20) for i in range(3)]
    a, b, c = clients

    def call(client, method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f'acceptance request failed: {response.status_code}')
        return response.json()

    def draft_and_publish():
        identifier = uuid.uuid4().hex
        item = call(a, 'GET', '/api/local/consumption')[0]
        friends = call(a, 'GET', '/api/local/friends')['friends']
        bob = next(row for row in friends if row['node_name'] == 'Bob')
        row = call(a, 'POST', f'/api/local/friend-feed/publications/{identifier}/draft', json={
            'reference': {'item_id': item['item_id']}, 'audience': {'mode': 'selected', 'relationship_ids': [bob['relationship_id']]},
            'expected_revision': 0, 'operation_id': uuid.uuid4().hex})
        return call(a, 'POST', f'/api/local/friend-feed/publications/{identifier}/publish', json={
            'expected_revision': row['revision'], 'operation_id': uuid.uuid4().hex})

    case_file = home / '.feed-cleanup-case.json'
    try:
        for client, suffix in zip(clients, ('author', 'bob', 'carol'), strict=True):
            status = call(client, 'GET', '/api/local/privacy/status')
            expected = home.parent / f'rynmesh-feed-acceptance-cleanup-{suffix}'
            assert Path(status['storage_root']).resolve() == expected
        if args.phase == 'prepare':
            assert not case_file.exists()
            assert call(a, 'GET', '/api/local/friends')['friends'] == []
            relationships = []
            for client in (b, c):
                invite = call(a, 'POST', '/api/local/friends/invites', json={})
                call(client, 'POST', '/api/local/friends/join', json={'invite_uri': invite['invite_uri']})
                rid = call(client, 'GET', '/api/local/friends')['friends'][0]['relationship_id']
                relationships.append(rid)
                call(client, 'PUT', '/api/local/friend-feed/subscriptions/' + rid, json={'enabled': True, 'expected_revision': 0})
            publication = draft_and_publish()
            for client, rid in zip((b, c), relationships, strict=True):
                call(client, 'POST', f'/api/local/friend-feed/subscriptions/{rid}/refresh', json={})
            assert len(call(b, 'GET', '/api/local/friend-feed')['timeline'][0]['rows']) == 1
            assert call(c, 'GET', '/api/local/friend-feed')['timeline'][0]['rows'] == []
            rid = relationships[0]
            copy = call(b, 'POST', f'/api/local/friend-feed/subscriptions/{rid}/{publication["id"]}/fetch', json={'expected_revision': publication['revision']})
            call(b, 'POST', f'/api/local/friend-feed/subscriptions/{rid}/{publication["id"]}/read', json={'expected_revision': publication['revision']})
            body = call(b, 'GET', '/api/local/friends/documents/' + copy['library_id'].removeprefix('import:') + '/body')
            case = {'publication': publication['id'], 'revision': publication['revision'], 'bob_relationship': rid,
                    'library_id': copy['library_id'], 'copy_hash': hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()}
            case_file.write_text(json.dumps(case), encoding='utf-8')
            print('Prepared: Bob sees one update and has saved a copy; Carol sees none. Clear Bob and Author from Settings.')
            return
        case = json.loads(case_file.read_text(encoding='utf-8'))
        jobs = [call(client, 'GET', PREFIX + '/job')['job'] for client in (a, b)]
        assert all(job and job['local_copies_complete'] and not job['remote_confirmed'] for job in jobs)
        assert call(a, 'GET', '/api/local/friend-feed/publications')['publications'] == []
        snapshot = call(b, 'GET', '/api/local/friend-feed')
        assert snapshot['timeline'] == [] and not snapshot['subscriptions'][0]['enabled']
        response = b.post(f'/api/local/friend-feed/subscriptions/{case["bob_relationship"]}/{case["publication"]}/fetch', json={'expected_revision': case['revision']})
        assert response.status_code == 409 and response.json()['detail'] == 'feed_publication_unavailable'
        body = call(b, 'GET', '/api/local/friends/documents/' + case['library_id'].removeprefix('import:') + '/body')
        assert hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() == case['copy_hash']
        assert len(call(a, 'GET', '/api/local/friends')['friends']) == 2
        new = draft_and_publish()
        subscription = snapshot['subscriptions'][0]
        call(b, 'PUT', '/api/local/friend-feed/subscriptions/' + case['bob_relationship'], json={'enabled': True, 'expected_revision': subscription['revision']})
        call(b, 'POST', '/api/local/friend-feed/subscriptions/' + case['bob_relationship'] + '/refresh', json={})
        for client, job in zip((a, b), jobs, strict=True):
            assert call(client, 'POST', PREFIX + '/job', json={'review_token': job['id']}) == job
        assert call(a, 'GET', '/api/local/friend-feed/publications')['publications'][0]['id'] == new['id']
        assert call(b, 'GET', '/api/local/friend-feed')['timeline'][0]['rows'][0]['id'] == new['id']
        result = {'verified_at': datetime.now(UTC).isoformat(), 'environment': 'Windows, three independent loopback nodes',
            'selected_bob_initially_visible': True, 'carol_initially_denied': True, 'publisher_cleanup_confirmed': True,
            'receiver_cleanup_confirmed': True, 'old_publication_fetch_denied': True, 'saved_copy_hash_unchanged': True,
            'friendships_retained': True, 'new_publication_and_refollow_succeeded': True, 'completed_cleanup_replay_kept_new_work': True,
            'remote_erasure_confirmed': False, 'packaged_desktop': False}
        output = Path(__file__).resolve().parents[1] / 'docs/acceptance/friend-feed-development/cleanup-http.json'
        output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(result))
    finally:
        for client in clients:
            client.close()


if __name__ == '__main__':
    main()
