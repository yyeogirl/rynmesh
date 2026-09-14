"""Verify completed synthetic reading cleanup without clearing new data.

Run after the documented browser flow. Only the already completed current
operation is replayed; an unfinished job or a different home is refused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18880)
    args = parser.parse_args()
    home = args.home.resolve()
    if home.name != 'rynmesh-offline-acceptance-reading-cleanup' or not home.is_dir():
        raise SystemExit('Use the dedicated reading-cleanup acceptance home.')
    with httpx.Client(base_url=f'http://127.0.0.1:{args.port}', timeout=20) as client:
        status = client.get('/api/local/privacy/status')
        status.raise_for_status()
        assert Path(status.json()['storage_root']).resolve() == home
        response = client.get('/api/local/privacy/reading/job')
        response.raise_for_status()
        job = response.json()['job']
        assert job['local_copies_complete'] and job['sequence'] == 2 and not job['remote_confirmed']
        path = home / 'consumption.json'
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        response = client.post('/api/local/privacy/reading/job', json={'review_token': job['id']})
        response.raise_for_status()
        assert response.json() == job
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before
        data = json.loads(path.read_bytes())
        assert data['version'] == 'ryn.consumption.v4' and data['sync'] is None
        assert list(data['records']) == ['reading-cleanup-recovery']
        assert data['records']['reading-cleanup-recovery']['bookmarked'] is True
        assert not (home / '.consumption.json.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.tmp').exists()
        assert not list(home.glob('consumption.json*.migrated'))
        result = {'verified_at': datetime.now(UTC).isoformat(), 'environment': 'Windows loopback node',
            'sequence': job['sequence'], 'done': job['done'], 'remaining_steps': job['pending'],
            'original_completed_request_replayed': True, 'new_source_bytes_unchanged': True,
            'new_bookmark_retained': True, 'reviewed_orphan_removed': True,
            'migration_backups_removed': True, 'sync_opt_in_unchanged': True,
            'remote_confirmed': False, 'packaged_desktop': False}
        output = Path(__file__).resolve().parents[1] / 'docs/acceptance/device-sync-development/reading-cleanup-http.json'
        output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(result))


if __name__ == '__main__':
    main()
