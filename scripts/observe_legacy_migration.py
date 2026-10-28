"""Record safe node metadata after explicit browser legacy-migration actions."""
import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', required=True)
    args = parser.parse_args()
    home = Path('D:/code/rynmesh-ai-recovery-acceptance')
    marker = json.loads((home / '.ai-recovery-fixture.json').read_text(encoding='utf-8'))
    row = {'label': args.label, 'at': datetime.now(UTC).isoformat(), 'pid': marker['pid']}
    with httpx.Client(base_url='http://127.0.0.1:18924/api/local/', timeout=10, trust_env=False) as client:
        def read(path):
            response = client.get(path)
            response.raise_for_status()
            return response.json()
        assert Path(read('privacy/status')['storage_root']).resolve() == home.resolve()
        rows = read('ask/conversations')['conversations']
        row['node_conversation_count'] = len(rows)
        row['original_histories'] = {r['id']: digest(r) for r in rows if not r['id'].startswith('migration-acceptance-20260914-')}
        row['migrated'] = []
        for r in rows:
            if r['id'].startswith('migration-acceptance-20260914-'):
                row['migrated'].append({**{k: r.get(k) for k in ('id', 'serviceKey', 'providerPeerId', 'createdAt', 'updatedAt', 'revision')},
                    'digest': digest(r), 'title_digest': digest(r['title']),
                    'messages': [{**{k: m.get(k) for k in ('id', 'role', 'status', 'taskId', 'createdAt', 'cost')},
                                  'content_digest': digest(m['content'])} for m in r['messages']]})
        row['order_ids'] = sorted(o['task_id'] for o in read('llm/orders')['orders'])
    raw = (home / 'ask-ryn/history.json').read_bytes()
    row['node_file_contains_fixture_plaintext'] = b'Synthetic legacy' in raw or b'legacy-provider-one' in raw
    assert not row['node_file_contains_fixture_plaintext']
    path = Path(__file__).resolve().parents[1] / 'docs/acceptance/ask-ryn-development/migration-checkpoints-20260914.json'
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'scope': 'Real encrypted browser legacy input and production node migration UI; synthetic history, no model inference', 'observations': []}
    data['observations'].append(row)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'label': args.label, 'node_conversations': len(rows), 'migrated': len(row['migrated']), 'orders': len(row['order_ids'])}))


if __name__ == '__main__':
    main()
