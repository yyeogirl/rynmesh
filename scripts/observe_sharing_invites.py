"""Read-only evidence for three fresh browser invitation/attachment nodes."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx

OUTPUT = Path(__file__).resolve().parents[1] / 'docs/acceptance/friends-development/invite-attachment-checkpoints-20260914.json'


def observe(label):
    snapshot = {'label': label, 'at': datetime.now(UTC).isoformat(), 'nodes': {}}
    for name, port in (('A', 18970), ('B', 18971), ('C', 18972)):
        home = Path(f'D:/code/rynmesh-first-sharing-acceptance-invites-20260914-{name}')
        assert json.loads((home / '.first-sharing-fixture.json').read_text())['port'] == port
        with httpx.Client(base_url=f'http://127.0.0.1:{port}/api/local/', trust_env=False, timeout=15) as client:
            def get(path):
                response = client.get(path)
                response.raise_for_status()
                return response.json()

            assert Path(get('privacy/status')['storage_root']).resolve() == home.resolve()
            friends = get('friends')['friends']
            node = {'friends': [{k: row.get(k) for k in ('peer_id', 'node_name', 'relationship_id', 'permissions', 'status')} for row in friends],
                'invites': [{k: row.get(k) for k in ('invite_id', 'status', 'created_at', 'expires_at')} for row in get('friends/invites')['invites']],
                'messages': []}
            for friend in friends:
                for row in get('friends/' + quote(friend['peer_id'], safe='') + '/messages')['messages']:
                    node['messages'].append({k: row.get(k) for k in ('msg_id', 'dir', 'kind', 'delivered', 'delivery_state', 'error', 'attachment')})
            snapshot['nodes'][name] = node
    data = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {
        'scope': 'Windows browser and three independent loopback nodes; no preseeded relationships/messages; no mailbox server', 'checkpoints': []}
    data['checkpoints'].append(snapshot)
    OUTPUT.write_text(json.dumps(data, indent=2) + '\n')
    print(json.dumps({name: {'friends': len(node['friends']), 'messages': len(node['messages']),
        'invites': [row['status'] for row in node['invites']]} for name, node in snapshot['nodes'].items()}))


def downloads():
    source = Path('D:/code/rynmesh-first-sharing-acceptance-invites-20260914-A/attachment-acceptance')
    checks = []
    for name in ('friend-note-20260914-a.txt', 'friend-note-20260914-b.bin', 'friend-limit-20260914.bin'):
        original = source / name
        downloaded = Path.home() / 'Downloads' / name
        expected = hashlib.sha256(original.read_bytes()).hexdigest()
        present = downloaded.is_file()
        actual = hashlib.sha256(downloaded.read_bytes()).hexdigest() if present else None
        checks.append({'filename': name, 'bytes': downloaded.stat().st_size if present else None,
            'sha256': actual, 'matches_original': actual == expected, 'present': present})
    data = json.loads(OUTPUT.read_text())
    data['downloaded_files'] = checks
    OUTPUT.write_text(json.dumps(data, indent=2) + '\n')
    print(json.dumps(checks))
    if not all(row['matches_original'] for row in checks):
        raise SystemExit('Not all browser-saved files have been verified.')


def verify():
    data = json.loads(OUTPUT.read_text())
    checkpoints = {row['label']: row for row in data['checkpoints']}
    baseline = checkpoints['before-race']['nodes']
    assert all(not row['friends'] and not row['messages'] for row in baseline.values())
    race = checkpoints['after-race']['nodes']
    assert len(race['A']['friends']) == len(race['C']['friends']) == 1 and not race['B']['friends']
    relation = race['A']['friends'][0]['relationship_id']
    assert race['C']['friends'][0]['relationship_id'] == relation
    assert all(not row['messages'] for row in checkpoints['oversize-blocked']['nodes'].values())
    repeat = checkpoints['attachments-and-repeat']['nodes']
    assert repeat['A']['friends'] == race['A']['friends'] and repeat['C']['friends'] == race['C']['friends']
    assert {row['msg_id'] for row in repeat['A']['messages']} == {row['msg_id'] for row in repeat['C']['messages']}
    assert len(repeat['A']['messages']) == 3
    restarted = checkpoints['after-restart']['nodes']
    before_restart = checkpoints['expired']['nodes']
    assert restarted == before_restart
    final = checkpoints['final']['nodes']
    for name in final:
        assert final[name]['friends'] == restarted[name]['friends']
        assert final[name]['messages'] == restarted[name]['messages']
        expected_invites = [{**row, 'status': 'expired'} if row['status'] == 'active' else row for row in restarted[name]['invites']]
        assert final[name]['invites'] == expected_invites
    assert sorted(row['status'] for row in final['A']['invites']) == ['cancelled', 'expired', 'used', 'used']
    permissions = {'friend.message', 'friend.attachment.small', 'friend.content-card'}
    assert all(set(friend['permissions']) == permissions for node in final.values() for friend in node['friends'])

    def events(name, kind):
        path = Path(f'D:/code/rynmesh-first-sharing-acceptance-invites-20260914-{name}/{kind}-observations.jsonl')
        return [json.loads(line) for line in path.read_text().splitlines() if json.loads(line)['path'] == '/api/peer/friends/accept']

    start_b = min(datetime.fromisoformat(row['at']) for row in events('B', 'outbound'))
    start_c = min((datetime.fromisoformat(row['at']) for row in events('C', 'outbound')), key=lambda at: abs((at-start_b).total_seconds()))
    ends = [datetime.fromisoformat(row['at']) for row in events('A', 'peer') if 0 <= (datetime.fromisoformat(row['at'])-start_b).total_seconds() < 1]
    assert len(ends) == 2 and max(start_b, start_c) < min(ends)
    data['assertions'] = {'single_race_winner': True, 'accept_requests_overlap': True,
        'request_starts': [start_b.isoformat(), start_c.isoformat()], 'response_ends': [at.isoformat() for at in ends],
        'oversize_created_no_messages': True, 'successful_repeat_preserved_relationship': True,
        'states_and_messages_preserved_after_restart': True, 'sharing_permissions_only': True}
    OUTPUT.write_text(json.dumps(data, indent=2) + '\n')
    print(json.dumps(data['assertions']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('label')
    args = parser.parse_args()
    if args.label == 'verify-downloads':
        downloads()
    elif args.label == 'verify':
        verify()
    else:
        observe(args.label)
