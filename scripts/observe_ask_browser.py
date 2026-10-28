"""Read safe evidence for browser-created conversations on the real AI fixture."""
from __future__ import annotations

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
    parser.add_argument('--conversation', required=True)
    parser.add_argument('--preview-question')
    parser.add_argument('--preview-draft', action='store_true')
    args = parser.parse_args()
    home = Path('D:/code/rynmesh-ai-recovery-acceptance')
    marker = json.loads((home / '.ai-recovery-fixture.json').read_text(encoding='utf-8'))
    assert marker['kind'] == 'ryn.ai-recovery-acceptance.v1'
    row = {'label': args.label, 'at': datetime.now(UTC).isoformat(), 'pid': marker['pid']}
    with httpx.Client(base_url='http://127.0.0.1:18924', timeout=15, trust_env=False) as client:
        def call(path, **kwargs):
            response = client.post('/api/local/' + path, **kwargs) if kwargs else client.get('/api/local/' + path)
            response.raise_for_status()
            return response.json()
        assert Path(call('privacy/status')['storage_root']).resolve() == home.resolve()
        history = call('ask/conversations')['conversations']
        conversation = call('ask/conversations/' + args.conversation)
        row['conversation_count'] = len(history)
        row['history_digest'] = digest(conversation)
        row['conversation'] = {k: conversation.get(k) for k in ('id', 'revision', 'providerPeerId', 'serviceKey', 'contextIds')}
        row['messages'] = []
        for message in conversation['messages']:
            saved = {k: message.get(k) for k in ('id', 'role', 'status', 'taskId', 'cost', 'contextIds', 'contextBytes', 'promptSha256')}
            saved['content_digest'] = digest(message.get('content'))
            saved['content_bytes'] = len(message.get('content', '').encode())
            if message.get('taskId') and message['role'] == 'assistant':
                saved['run'] = call('ask/runs/' + message['taskId'])
                response = client.get('/api/local/llm/orders/' + message['taskId'])
                saved['order_status'] = response.status_code
                if response.is_success:
                    saved['order'] = {k: response.json().get(k) for k in ('task_id', 'state', 'transport', 'amount', 'error_code')}
            row['messages'].append(saved)
        question = conversation.get('draft', '').strip() if args.preview_draft else args.preview_question
        if question:
            preview = call('ask/preview', json={'conversation_id': conversation['id'],
                'expected_revision': conversation['revision'], 'question': question})
            row['preview'] = {k: preview.get(k) for k in ('provider_peer_id', 'service_id', 'prompt_sha256',
                'context_window', 'input_token_upper_estimate', 'framing_reserve', 'max_output_tokens', 'history_messages_omitted')}
            row['preview']['sources'] = [{k: source.get(k) for k in ('library_id', 'sha256', 'text_bytes',
                'included_bytes', 'budget_truncated', 'extraction_truncated')} for source in preview['sources']]
            assert sum(preview[k] for k in ('input_token_upper_estimate', 'framing_reserve', 'max_output_tokens')) <= preview['context_window']
        service = call('llm/service/status')
        row['service'] = {k: service.get(k) for k in ('ready', 'publication_enabled')}
    path = Path(__file__).resolve().parents[1] / 'docs/acceptance/ask-ryn-development/browser-checkpoints-20260914.json'
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'scope': 'Real native CPU model and browser-created conversation; synthetic article input; loopback, not packaged desktop',
        'observations': []}
    data['observations'].append(row)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'label': args.label, 'messages': len(row['messages']), 'states': [m['status'] for m in row['messages']],
        'preview': row.get('preview')}))


if __name__ == '__main__':
    main()
