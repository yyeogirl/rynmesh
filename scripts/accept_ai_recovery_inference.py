"""Submit and inspect one real local Ask Ryn run in the recovery fixture.

Uses production owner HTTP APIs and the actual installed model. A private
fixture receipt retains task identity across script retries. Exported evidence
contains only states, checksums and counts, never model input/output or keys.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

QUESTION = '计算 7+8，只回答数字。'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--action', choices=('begin', 'observe', 'verify-restart', 'verify-followup'), required=True)
    parser.add_argument('--browser-conversation', help='Already completed conversation created through the real UI.')
    args = parser.parse_args()
    home = args.home.resolve()
    if home.name != 'rynmesh-ai-recovery-acceptance':
        raise SystemExit('Use the dedicated recovery fixture.')
    marker = json.loads((home / '.ai-recovery-fixture.json').read_text(encoding='utf-8'))
    if marker.get('kind') != 'ryn.ai-recovery-acceptance.v1':
        raise SystemExit('Wrong fixture marker.')
    receipt_path = home / '.ai-recovery-inference.json'
    receipt = json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.exists() else None

    def save():
        from rynmesh.atomic_io import atomic_write_json
        atomic_write_json(receipt_path, receipt)

    with httpx.Client(base_url='http://127.0.0.1:18924', timeout=30) as client:
        def call(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        privacy = call('GET', '/api/local/privacy/status')
        if Path(privacy['storage_root']).resolve() != home:
            raise SystemExit('The server is not this fixture.')
        setup = call('GET', '/api/local/llm/setup/status')
        service = call('GET', '/api/local/llm/service/status')
        if args.action == 'begin':
            assert setup['state'] == 'succeeded' and service['ready']
            assert service['publication_enabled'] is False
            services = call('GET', '/api/local/llm/services')['services']
            choices = [row for row in services if row.get('access') == 'self'
                       and row['service']['package_id'] == 'local-small' and row.get('online')]
            assert len(choices) == 1
            chosen = choices[0]
            assert all(chosen['service']['pricing'][key] == 0
                       for key in ('input_per_1k', 'output_per_1k', 'minimum'))
            if receipt is None:
                receipt = {'kind': 'ryn.ai-recovery-inference.v1',
                    'conversation_id': 'recovery-' + uuid.uuid4().hex,
                    'task_id': 'task_' + uuid.uuid4().hex, 'question_digest': digest(QUESTION),
                    'provider': chosen['peer_id'], 'service_key': chosen['peer_id'] + '::local-small',
                    'created_at': datetime.now(UTC).isoformat()}
                save()
            assert receipt['provider'] == chosen['peer_id'] and receipt['question_digest'] == digest(QUESTION)
            path = '/api/local/ask/conversations/' + receipt['conversation_id']
            existing = client.get(path)
            if existing.status_code == 404:
                row = {'id': receipt['conversation_id'], 'title': 'Recovery acceptance',
                    'serviceKey': receipt['service_key'], 'providerPeerId': receipt['provider'],
                    'serviceName': 'Local model', 'networkId': 'rynmesh-main',
                    'createdAt': receipt['created_at'], 'updatedAt': receipt['created_at'], 'messages': []}
                conversation = call('PUT', path, json={'conversation': row, 'expected_revision': 0})
            else:
                existing.raise_for_status()
                conversation = existing.json()
            if 'request' not in receipt:
                preview_request = {'conversation_id': receipt['conversation_id'],
                    'expected_revision': conversation['revision'], 'question': QUESTION}
                preview = call('POST', '/api/local/ask/preview', json=preview_request)
                assert preview['provider_peer_id'] == receipt['provider']
                assert preview['service_id'] == 'local-small' and not preview.get('ai_permission')
                receipt['request'] = {key: value for key, value in preview_request.items() if key != 'question'}
                receipt['request'].update(task_id=receipt['task_id'], prompt_sha256=preview['prompt_sha256'])
                save()
            result = call('POST', '/api/local/ask/runs', json={**receipt['request'], 'question': QUESTION})
            print(json.dumps({'action': 'begin', **result}))
            return

        assert receipt and receipt['kind'] == 'ryn.ai-recovery-inference.v1'
        run = call('GET', '/api/local/ask/runs/' + receipt['task_id'])
        if run['state'] != 'succeeded':
            print(json.dumps({'action': args.action, **run}))
            raise SystemExit(2 if run['state'] in {'queued', 'running'} else 1)
        conversation = call('GET', '/api/local/ask/conversations/' + receipt['conversation_id'])
        order = call('GET', '/api/local/llm/orders/' + receipt['task_id'])
        assert conversation['serviceKey'] == receipt['service_key']
        messages = conversation['messages']
        assert len(messages) == 2 and all(row['taskId'] == receipt['task_id'] for row in messages)
        answer = next(row for row in messages if row['role'] == 'assistant')
        assert answer['status'] == 'complete' and answer['content'].strip()
        assert order['state'] == 'succeeded' and order['transport'] == 'local_runtime'
        assert order['amount'] == 0 and answer['cost'] == 0
        repeated = call('POST', '/api/local/ask/runs', json={**receipt['request'], 'question': QUESTION})
        assert repeated['task_id'] == receipt['task_id'] and repeated['state'] == 'succeeded'
        current = call('GET', '/api/local/ask/conversations/' + receipt['conversation_id'])
        assert digest(current) == digest(conversation)
        history_digest = digest(conversation)
        browser_evidence = None
        browser_id = args.browser_conversation or receipt.get('browser_conversation_id')
        if browser_id:
            browser_conversation = call('GET', '/api/local/ask/conversations/' + browser_id)
            browser_messages = browser_conversation['messages']
            followup = args.action == 'verify-followup'
            assert len(browser_messages) == (4 if followup else 2)
            assert browser_conversation['serviceKey'] == receipt['service_key']
            assert all(row['status'] == 'complete' for row in browser_messages)
            browser_answer = next(row for row in browser_messages if row['role'] == 'assistant')
            assert browser_answer['content'].strip() == '15' and browser_answer['cost'] == 0
            browser_order = call('GET', '/api/local/llm/orders/' + browser_answer['taskId'])
            assert browser_order['state'] == 'succeeded' and browser_order['transport'] == 'local_runtime'
            assert browser_order['amount'] == 0
            if followup:
                assert service['ready'] and service['publication_enabled'] is False
                assert receipt['verified_node_pid'] != marker['pid']
                latest = browser_messages[-1]
                assert latest['role'] == 'assistant' and latest['content'].strip() == '13'
                assert latest['taskId'] != browser_answer['taskId'] and latest['cost'] == 0
                latest_order = call('GET', '/api/local/llm/orders/' + latest['taskId'])
                assert latest_order['state'] == 'succeeded' and latest_order['transport'] == 'local_runtime'
                assert latest_order['amount'] == 0
            browser_digest = digest(browser_conversation)
            if args.action == 'verify-restart':
                assert receipt['browser_history_digest'] == browser_digest
            elif not followup:
                receipt['browser_conversation_id'] = browser_id
                receipt['browser_history_digest'] = browser_digest
            browser_evidence = {'conversation_id': browser_id, 'history_digest': browser_digest,
                'message_count': len(browser_messages), 'answer_correct': True, 'amount': 0,
                'transport': 'local_runtime', 'binding_preserved': True,
                'new_inference_after_restart': followup,
                'scope': 'API inspection of the separately observed browser-created conversation'}
        if args.action in {'verify-restart', 'verify-followup'}:
            assert receipt['verified_history_digest'] == history_digest
            assert receipt['verified_node_pid'] != marker['pid']
        else:
            receipt['verified_history_digest'] = history_digest
            receipt['verified_node_pid'] = marker['pid']
            save()
        observation = {'action': args.action, 'at': datetime.now(UTC).isoformat(),
            'task_id': receipt['task_id'], 'conversation_id': receipt['conversation_id'],
            'history_digest': history_digest, 'state': run['state'], 'message_count': len(messages),
            'node_pid': marker['pid'],
            'answer_nonempty': True, 'arithmetic_match': bool(re.search(r'(?<!\d)15(?!\d)', answer['content'])),
            'transport': order['transport'], 'amount': order['amount'],
            'duplicate_request_preserved_history': True, 'binding_preserved': True,
            'configured': service.get('configured'), 'ready': service.get('ready'),
            'publication_enabled': service.get('publication_enabled'),
            'runtime': service.get('lifecycle', {}).get('runtime', {})}
        if browser_evidence:
            observation['browser_conversation'] = browser_evidence
        output = Path(__file__).resolve().parents[1] / 'docs/acceptance/local-ai-development/recovery-real-inference.json'
        evidence = json.loads(output.read_text(encoding='utf-8')) if output.exists() else {
            'scope': 'Windows loopback production owner HTTP APIs and real native model; not browser or packaged desktop',
            'observations': []}
        evidence['observations'].append(observation)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(observation))


if __name__ == '__main__':
    main()
