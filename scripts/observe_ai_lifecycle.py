"""Read-only lifecycle observations for the existing real-model fixture."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

HOME = Path('D:/code/rynmesh-ai-recovery-acceptance')
OUTPUT = Path(__file__).resolve().parents[1] / 'docs/acceptance/local-ai-development/lifecycle-browser-checkpoints-20260914.json'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def observe(label):
    assert json.loads((HOME / '.ai-recovery-fixture.json').read_text())['kind'] == 'ryn.ai-recovery-acceptance.v1'
    with httpx.Client(base_url='http://127.0.0.1:18924/api/local/', trust_env=False, timeout=30) as client:
        def get(path):
            response = client.get(path)
            response.raise_for_status()
            return response.json()

        assert Path(get('privacy/status')['storage_root']).resolve() == HOME.resolve()
        provider = get('llm/service/status')
        conversations = sorted(get('ask/conversations')['conversations'], key=lambda row: row['id'])
        orders = sorted(({'task_id': row['task_id'], 'state': row['state']} for row in get('llm/orders')['orders']), key=lambda row: row['task_id'])
    lifecycle = provider.get('lifecycle') or {}
    model = HOME / 'llm/models/local-small/light.gguf'
    manifest = json.loads((HOME / 'llm/packages/local-small/manifest.json').read_text())
    assert Path(manifest['model_path']).resolve() == model.resolve() and manifest['model_owned']
    record = {'label': label, 'at': datetime.now(UTC).isoformat(),
        'service': {key: provider.get(key) for key in ('configured', 'ready', 'online', 'publication_enabled')},
        'runtime': lifecycle.get('runtime'), 'storage': lifecycle.get('storage'), 'error': lifecycle.get('error'),
        'model_bytes': model.stat().st_size if model.exists() else 0,
        'runtime_binary_exists': (HOME / 'llm/runtime/llama-b10774/llama-server.exe').exists(),
        'conversation_count': len(conversations), 'conversations_sha256': digest(conversations),
        'orders': orders, 'manifest_exists': True}
    if model.exists():
        with model.open('rb') as source:
            record['model_sha256'] = hashlib.file_digest(source, 'sha256').hexdigest()
    data = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {
        'scope': 'Windows loopback browser; existing real Qwen model and native runtime; not packaged desktop', 'checkpoints': []}
    if data['checkpoints']:
        baseline = data['checkpoints'][0]
        assert record['conversations_sha256'] == baseline['conversations_sha256']
        assert record['orders'] == baseline['orders']
        if model.exists():
            assert record['model_sha256'] == baseline['model_sha256']
    data['checkpoints'].append(record)
    OUTPUT.write_text(json.dumps(data, indent=2) + '\n')
    print(json.dumps({key: record[key] for key in ('label', 'service', 'runtime', 'storage', 'model_bytes', 'conversation_count')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('label')
    observe(parser.parse_args().label)
