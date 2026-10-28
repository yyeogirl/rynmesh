"""Read-only observations around browser export/deletion in the isolated fixture."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

HOME = Path('D:/code/rynmesh-ask-errors-acceptance-20260914')
OUTPUT = Path(__file__).resolve().parents[1] / 'docs/acceptance/ask-ryn-development/export-privacy-checkpoints-20260914.json'
DRAFT = 'Ask export privacy acceptance 20260914 — unsent draft'
DELETED = 'f64fd70a-974b-43f9-a6d1-64cc5eff4468'


def observe(label):
    assert json.loads((HOME / '.ask-errors-fixture.json').read_text())['kind'] == 'ryn.ask-errors.v1'
    with httpx.Client(base_url='http://127.0.0.1:18950/api/local/', trust_env=False, timeout=15) as client:
        assert Path(client.get('privacy/status').json()['storage_root']).resolve() == HOME.resolve()
        responses = {path: client.get('ask/' + path) for path in ('export', 'conversations', 'draft')}
        for response in responses.values():
            response.raise_for_status()
            assert response.headers.get('cache-control') == 'no-store'
        denied = client.get('ask/export', headers={'X-Forwarded-For': '198.51.100.88'})
        assert denied.status_code == 401 and denied.headers.get('cache-control') == 'no-store'
        exported = responses['export'].json()
        assert set(exported) == {'version', 'conversations', 'draft', 'sync_conflicts'}
        assert exported['draft']['text'] == DRAFT
        ids = sorted(row['id'] for row in exported['conversations'])
        assert ids == sorted(row['id'] for row in responses['conversations'].json()['conversations'])
        if label != 'exported':
            assert DELETED not in ids
            missing = client.get('ask/conversations/' + DELETED)
            assert missing.status_code == 404 and missing.headers.get('cache-control') == 'no-store'
        orders = client.get('llm/orders').json()['orders']

    payload = responses['export'].content
    download = {}
    if label == 'exported':
        downloaded = Path.home() / 'Downloads/ryn-conversations.json'
        downloaded_bytes = downloaded.read_bytes()
        assert json.loads(downloaded_bytes) == exported
        download = {'file': str(downloaded), 'bytes': len(downloaded_bytes),
            'sha256': hashlib.sha256(downloaded_bytes).hexdigest(), 'matches_owner_export': True}
    log = (HOME / 'ask-privacy-node.log').read_bytes()
    sensitive = [DRAFT.encode()]
    for row in exported['conversations']:
        sensitive.extend(message['content'].encode() for message in row['messages'] if message.get('content'))
    # PowerShell redirection can use UTF-16; inspect decoded text as well.
    log_texts = [log.decode(encoding, errors='ignore') for encoding in ('utf-8', 'utf-16-le')]
    assert all(value.decode() not in text for value in sensitive for text in log_texts)
    checks = {}
    for name in ('identity.ed25519', 'messaging.x25519', 'control_token'):
        raw = (HOME / name).read_bytes().strip()
        assert raw
        forms = [raw, base64.b64encode(raw), raw.hex().encode()]
        assert not any(value in payload or value in log for value in forms)
        assert not any(value.decode('ascii', errors='ignore') in text for value in forms if value.isascii() for text in log_texts)
        checks[name] = 'absent from response and captured log in stored/base64/hex forms'
    record = {'label': label, 'at': datetime.now(UTC).isoformat(), 'conversation_ids': ids,
        'draft_sha256': hashlib.sha256(DRAFT.encode()).hexdigest(), 'response_bytes': len(payload),
        'cache_control': {path: response.headers['cache-control'] for path, response in responses.items()},
        'unauthenticated_export_status': denied.status_code, 'credential_checks': checks, 'download': download,
        'log_bytes_checked': len(log), 'body_strings_absent_from_captured_log': len(sensitive),
        'orders': sorted(({'task_id': row['task_id'], 'state': row['state']} for row in orders), key=lambda row: row['task_id'])}
    data = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {
        'scope': 'Windows loopback node and browser; synthetic existing conversations; ordinary export/deletion only', 'checkpoints': []}
    if data['checkpoints']:
        assert record['orders'] == data['checkpoints'][0]['orders']
    data['checkpoints'] = [row for row in data['checkpoints'] if row['label'] != label] + [record]
    OUTPUT.write_text(json.dumps(data, indent=2) + '\n')
    print(json.dumps({'label': label, 'conversations': len(ids), 'orders': len(orders), 'checks': 'passed'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('label', choices=('exported', 'deleted', 'restarted'))
    observe(parser.parse_args().label)
