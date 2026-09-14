"""Read export and privacy evidence from an owned first-reading fixture.

No export body, source URL, private key or article text is written to evidence.
This verifies the HTTP export payload; browser download persistence is separate.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--label', required=True)
    args = parser.parse_args()
    home = args.home.resolve()
    assert home.name.startswith('rynmesh-first-reading-acceptance-')
    marker = json.loads((home / '.first-reading-fixture.json').read_text(encoding='utf-8'))
    assert marker['kind'] == 'ryn.first-reading-acceptance.v1'
    with httpx.Client(base_url='http://127.0.0.1:18930', timeout=20, trust_env=False) as client:
        def read(path):
            response = client.get('/api/local/' + path)
            response.raise_for_status()
            return response.json()
        status = read('privacy/status')
        assert Path(status['storage_root']).resolve() == home
        payload = read('privacy/export')
        history = read('recommendations/signals?limit=100')['items']
        events = payload['recommendation_profile']['feedback_events']
        by_id = {row['event_id']: row for row in events}
        assert set(by_id) == {row['event_id'] for row in history}
        for row in history:
            for key in ('action', 'tags', 'publisher', 'platform', 'updated_at', 'undone_at'):
                assert row.get(key) == by_id[row['event_id']].get(key)
        serialized = json.dumps(payload, ensure_ascii=False)
        public = client.get('/api/v1/node')
        public.raise_for_status()
        observed_strings = list(strings(payload['assistant_audit'])) + list(strings(public.json()))
        checked_blocks = 0
        for file in (home / 'reader-cache').glob('*.json'):
            cached = json.loads(file.read_text(encoding='utf-8'))
            # The cache envelope contains a result object; find paragraph fields.
            def walk(value):
                nonlocal checked_blocks
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key == 'text' and isinstance(child, str) and len(child) > 80:
                            assert not any(child in value for value in observed_strings)
                            checked_blocks += 1
                        else:
                            walk(child)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)
            walk(cached)
        for name in ('identity.ed25519', 'messaging.x25519'):
            key = (home / name).read_bytes()
            assert key.hex() not in serialized
            assert base64.b64encode(key).decode() not in serialized
            try:
                token = key.decode().strip()
                assert not token or token not in serialized
            except UnicodeDecodeError:
                pass
        denied = client.get('/api/local/privacy/export', headers={'X-Forwarded-For': '198.51.100.7'})
        assert denied.status_code in (401, 403)
        result = {'label': args.label, 'at': datetime.now(UTC).isoformat(), 'node_pid': marker['pid'],
            'export_digest': digest(payload), 'feedback_events': len(events),
            'feedback_event_ids': list(by_id), 'export_matches_explanation_fields': True,
            'profile_digest': digest(payload['recommendation_profile']),
            'digest_preferences_digest': digest(payload['digest_preferences']),
            'digest_preferences_empty': not payload['digest_preferences'],
            'reading_history_items': len(payload['reading_history']),
            'reading_history_digest': digest(payload['reading_history']),
            'cached_discovery_items': status['cached_discovery_items'],
            'reader_cache_files': status['reader_cache_files'],
            'source_catalog_digest': digest(payload['sources']),
            'migration_backup_exists': (home / 'recommendation-profile.json.migrated').exists(),
            'audit_events': len(payload['assistant_audit']), 'audit_digest': digest(payload['assistant_audit']),
            'body_blocks_absent_from_audit_and_public_node': checked_blocks,
            'known_identity_keys_absent_from_export': True, 'forwarded_export_status': denied.status_code}
    path = Path(__file__).resolve().parents[1] / 'docs/acceptance/first-reading-development/privacy-checkpoints-20260914.json'
    evidence = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'scope': 'Windows production node HTTP observations accompanying browser privacy actions',
        'fixture': home.name, 'observations': []}
    assert evidence['fixture'] == home.name
    evidence['observations'].append(result)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
