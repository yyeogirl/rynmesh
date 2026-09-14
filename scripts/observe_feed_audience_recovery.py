"""Read-only observations around browser publication and offline audience changes."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

OUTPUT = Path(__file__).resolve().parents[1] / 'docs/acceptance/friend-feed-development/audience-recovery-checkpoints-20260914.json'
NODES = {'author': (18860, 'author-20260911-v2'), 'bob': (18861, 'bob-20260911'), 'carol': (18862, 'carol-20260911')}


def observe(label, excluded):
    data = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {
        'scope': 'Chrome and three existing independent Windows loopback nodes; same previously paired identities',
        'checkpoints': [], 'packaged_desktop': False, 'public_nat': False}
    point = {'label': label, 'at': datetime.now(UTC).isoformat(), 'nodes': {}, 'excluded_stopped_nodes': excluded}
    for name, (port, suffix) in NODES.items():
        if name in excluded:
            continue
        home = Path('D:/code/rynmesh-feed-acceptance-' + suffix)
        with httpx.Client(base_url=f'http://127.0.0.1:{port}/api/local/', trust_env=False, timeout=20) as client:
            assert Path(client.get('privacy/status').json()['storage_root']).resolve() == home.resolve()
            pubs = client.get('friend-feed/publications').json()['publications']
            feed = client.get('friend-feed').json()
            row = {'publications': [], 'subscriptions': [], 'timeline': [], 'copies': []}
            for publication in pubs:
                draft, live = publication.get('draft'), publication.get('published')
                row['publications'].append({'id': publication['id'], 'revision': publication['revision'],
                    'stopped': publication['stopped'], 'draft_audience': draft['audience'] if draft else None,
                    'draft_sha256': draft['card'].get('sha256') if draft else None,
                    'published': {'revision': live['revision'], 'audience': live['audience'],
                        'sha256': live['card'].get('sha256')} if live else None,
                    'currently_allowed': [friend['relationship_id'] for friend in publication.get('current_audience', [])]})
            for subscription in feed['subscriptions']:
                row['subscriptions'].append({key: subscription.get(key) for key in ('relationship_id', 'enabled', 'revision', 'read')})
            for group in feed['timeline']:
                row['timeline'].append({'relationship_id': group['relationship_id'], 'checked_at': group['checked_at'],
                    'error_code': group['error_code'], 'rows': [{key: entry.get(key) for key in ('id', 'revision', 'read')}
                        for entry in group['rows']]})
            for saved in client.get('friends/documents').json()['documents']:
                body = client.get('friends/documents/' + saved['import_id'] + '/body')
                body.raise_for_status()
                row['copies'].append({'id': saved['import_id'], 'sha256': hashlib.sha256(body.json()['text'].encode()).hexdigest()})
            point['nodes'][name] = row
    data['checkpoints'].append(point)
    OUTPUT.write_text(json.dumps(data, indent=2) + '\n')
    print(json.dumps({'label': label, 'nodes': {name: {'publications': value['publications'],
        'visible_count': sum(len(group['rows']) for group in value['timeline']), 'copies': len(value['copies'])}
        for name, value in point['nodes'].items()}}))


def denied_fetches():
    data = json.loads(OUTPUT.read_text())
    records = []
    with httpx.Client(base_url='http://127.0.0.1:18861/api/local/', trust_env=False, timeout=20) as client:
        for revision in (7, 9):
            response = client.post('friend-feed/subscriptions/132921484033401689274aae70734cb1/'
                '259b24dbeee44ac38fbf7c67a43b697e/fetch', json={'expected_revision': revision})
            assert response.status_code == 409 and response.json()['detail'] == 'feed_publication_unavailable'
            records.append({'revision_requested': revision, 'status': response.status_code,
                'error': response.json()['detail'], 'at': datetime.now(UTC).isoformat()})
    data['denied_fetches_after_reconnect'] = records
    OUTPUT.write_text(json.dumps(data, indent=2) + '\n')
    print(json.dumps(records))


def verify():
    data = json.loads(OUTPUT.read_text())
    points = {row['label']: row['nodes'] for row in data['checkpoints']}
    original = points['draft-saved']['author']['publications']
    assert original[0]['revision'] == 6 and original[0]['stopped']
    for name in ('publish-write-failed', 'failed-draft-after-restart'):
        assert points[name]['author']['publications'] == original
        assert all(not group['rows'] for node in ('bob', 'carol') for group in points[name][node]['timeline'])
    bob, carol = '132921484033401689274aae70734cb1', '792eac87440f460a95afa08cb137d58c'
    both = points['published-both']['author']['publications'][0]
    assert both['revision'] == 7 and set(both['currently_allowed']) == {bob, carol}
    for name in ('bob', 'carol'):
        rows = points['published-both'][name]['timeline'][0]['rows']
        assert rows == [{'id': both['id'], 'revision': 7, 'read': False}]
    draft = points['narrowed-draft-only']['author']['publications'][0]
    assert draft['revision'] == 8 and draft['published'] == both['published']
    assert set(draft['currently_allowed']) == {bob, carol}
    assert draft['draft_audience']['relationship_ids'] == [carol]
    narrowed = points['narrowed-published']['author']['publications'][0]
    assert narrowed['revision'] == 9 and narrowed['currently_allowed'] == [carol]
    for label in ('bob-reconnected', 'stopped-sharing', 'final-restarted'):
        assert all(not group['rows'] for group in points[label]['bob']['timeline'])
        assert points[label]['bob']['copies'] == points['draft-saved']['bob']['copies']
        assert points[label]['bob']['subscriptions'] == points['published-both']['bob']['subscriptions']
    stopped = points['stopped-sharing']['author']['publications'][0]
    assert stopped['revision'] == 10 and stopped['stopped'] and not stopped['currently_allowed']
    assert points['final-restarted']['author']['publications'] == [stopped]
    assert all(not group['rows'] for group in points['final-restarted']['carol']['timeline'])
    assert [row['revision_requested'] for row in data['denied_fetches_after_reconnect']] == [7, 9]
    assert all(row['status'] == 409 and row['error'] == 'feed_publication_unavailable'
        for row in data['denied_fetches_after_reconnect'])
    print('Draft failure/restart, audience separation, offline revocation, independent copy and final restart verified.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('label')
    parser.add_argument('--exclude', nargs='*', choices=list(NODES), default=[])
    args = parser.parse_args()
    if args.label == 'denied-fetches':
        denied_fetches()
    elif args.label == 'verify':
        verify()
    else:
        observe(args.label, args.exclude)
