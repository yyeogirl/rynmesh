"""Read bounded evidence from an isolated first-reading browser fixture.

The script never performs product actions or seeds content. Operate the UI,
then record a named checkpoint. Body text, URLs and personal paths are omitted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    home = args.home.resolve()
    if not home.name.startswith('rynmesh-first-reading-acceptance-'):
        raise SystemExit('Use a dedicated first-reading fixture.')
    marker = json.loads((home / '.first-reading-fixture.json').read_text(encoding='utf-8'))
    assert marker['kind'] == 'ryn.first-reading-acceptance.v1'
    with httpx.Client(base_url='http://127.0.0.1:18930', timeout=20) as client:
        def read(path):
            response = client.get('/api/local/' + path)
            response.raise_for_status()
            return response.json()
        assert Path(read('privacy/status')['storage_root']).resolve() == home
        started = time.perf_counter()
        health = client.get('/health')
        health.raise_for_status()
        observation = {'label': args.label, 'at': datetime.now(UTC).isoformat(),
            'node_pid': marker['pid'], 'node_started_at': marker['started_at'],
            'health_seconds': time.perf_counter() - started,
            'first_success': read('first-success')}
        observation['history'] = [{**{key: row.get(key) for key in (
            'item_id', 'first_opened_unix', 'last_opened_unix', 'open_count',
            'bookmarked', 'progress', 'completed')}, 'record_digest': digest(row)}
            for row in read('consumption')]
        observation['sources'] = [{key: row.get(key) for key in (
            'id', 'status', 'ok', 'error', 'item_count', 'last_checked_unix',
            'last_success_unix', 'consecutive_failures', 'using_cached_items')}
            for row in read('sources/health')]
        signals = read('recommendations/signals?offset=0&limit=100')
        observation['feedback'] = [{key: row.get(key) for key in (
            'event_id', 'content_id', 'action', 'updated_at', 'undone_at', 'active')}
            for row in signals['items']]
        observation['model_configured'] = read('llm/service/status').get('configured', False)
    path = args.output or Path(__file__).resolve().parents[1] / 'docs/acceptance/first-reading-development/browser-checkpoints-20260914.json'
    evidence = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'scope': 'API checkpoints supplementing actual browser actions; Windows production node, not packaged desktop',
        'fixture': home.name, 'observations': []}
    assert evidence['fixture'] == home.name
    evidence['observations'].append(observation)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: observation[key] for key in ('label', 'node_pid', 'first_success', 'history', 'feedback', 'model_configured')}))


if __name__ == '__main__':
    main()
