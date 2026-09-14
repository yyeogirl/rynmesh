"""Cancel or interrupt a real remote-model Ask run after observing it running.

Only the dedicated acceptance consumer may be terminated. Requests use normal
owner preview/run APIs and the existing explicitly granted friend model.
Receipts retain task identity; rerunning an action never creates another task.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

HOME = Path('D:/code/rynmesh-ai-recovery-acceptance')
PROVIDER = '31QPVqwHR8puKwMvslGKIWcZC9eYhFZCt+H8D6qi4YA='
QUESTION = 'Write a numbered list of 100 practical gardening tips. Use a complete sentence for each tip and continue until all 100 are listed.'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('cancel', 'interrupt', 'verify-restart'))
    args = parser.parse_args()
    from rynmesh.atomic_io import atomic_write_json

    receipt_path = HOME / '.ask-running-recovery.json'
    receipts = json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.exists() else {}
    with httpx.Client(base_url='http://127.0.0.1:18924/api/local/', timeout=10, trust_env=False) as consumer, \
            httpx.Client(base_url='http://127.0.0.1:18846/api/local/', timeout=10, trust_env=False) as provider:
        def call(client, method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()
        assert Path(call(consumer, 'GET', 'privacy/status')['storage_root']).resolve() == HOME.resolve()
        assert Path(call(provider, 'GET', 'privacy/status')['storage_root']).name == 'rynmesh-local-ai-acceptance-c124bb62d6654226903a51badd14d528'
        marker = json.loads((HOME / '.ai-recovery-fixture.json').read_text(encoding='utf-8'))
        assert marker['kind'] == 'ryn.ai-recovery-acceptance.v1'
        action = 'interrupt' if args.action == 'verify-restart' else args.action
        receipt = receipts.get(action)
        if receipt is None:
            assert args.action != 'verify-restart'
            service = next(s for s in call(consumer, 'GET', 'llm/services')['services'] if s['peer_id'] == PROVIDER)
            assert service['service']['pricing']['currency'] == 'DEV_TASK_BALANCE'
            assert service['service']['pricing']['minimum'] <= 0.001
            assert service['online'] and service.get('ai_permission')
            receipt = {'conversation_id': 'running-' + uuid.uuid4().hex, 'task_id': 'task_' + uuid.uuid4().hex,
                       'initial_pid': marker['pid'], 'created_at': datetime.now(UTC).isoformat()}
            receipts[action] = receipt
            atomic_write_json(receipt_path, receipts)
        task = receipt['task_id']
        path = 'ask/conversations/' + receipt['conversation_id']
        existing = consumer.get(path)
        if existing.status_code == 404:
            assert args.action != 'verify-restart'
            row = {'id': receipt['conversation_id'], 'title': f'Real running task {action} acceptance',
                   'serviceKey': PROVIDER + '::local-small', 'serviceName': 'Friend native model',
                   'providerPeerId': PROVIDER, 'networkId': 'rynmesh-main', 'createdAt': receipt['created_at'],
                   'updatedAt': receipt['created_at'], 'messages': []}
            conversation = call(consumer, 'PUT', path, json={'conversation': row, 'expected_revision': 0})
        else:
            existing.raise_for_status()
            conversation = existing.json()
        if 'request' not in receipt:
            preview = call(consumer, 'POST', 'ask/preview', json={'conversation_id': conversation['id'],
                'expected_revision': conversation['revision'], 'question': QUESTION})
            receipt['request'] = {'task_id': task, 'conversation_id': conversation['id'],
                'expected_revision': conversation['revision'], 'question': QUESTION,
                'prompt_sha256': preview['prompt_sha256'], 'ai_permission': preview['ai_permission']}
            atomic_write_json(receipt_path, receipts)
        if args.action != 'verify-restart' and 'observed_running' not in receipt:
            call(consumer, 'POST', 'ask/runs', json=receipt['request'])
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                rows = call(provider, 'GET', 'llm/provider-orders')['orders']
                order = next((r for r in rows if r['task_id'] == task), None)
                if order and order['state'] == 'running':
                    receipt['observed_running'] = {'at': datetime.now(UTC).isoformat(), 'state': order['state']}
                    atomic_write_json(receipt_path, receipts)
                    if action == 'cancel':
                        receipt['cancel_response'] = call(consumer, 'POST', 'ask/runs/' + task + '/cancel')
                        atomic_write_json(receipt_path, receipts)
                    else:
                        # Recheck the fixture marker immediately before stopping its process.
                        current = json.loads((HOME / '.ai-recovery-fixture.json').read_text(encoding='utf-8'))
                        assert current['pid'] == marker['pid'] == receipt['initial_pid']
                        os.kill(current['pid'], signal.SIGTERM)
                        receipt['interrupted_at'] = datetime.now(UTC).isoformat()
                        atomic_write_json(receipt_path, receipts)
                        print(json.dumps({'action': action, 'task_id': task, 'observed_running': receipt['observed_running'], 'stopped_pid': current['pid']}))
                        return
                    break
                if order and order['state'] not in ('created', 'accepted', 'queued'):
                    raise AssertionError('Model completed before the interruption window; do not count this as active recovery.')
                time.sleep(0.02)
            else:
                raise AssertionError('Real provider did not enter running within the observation deadline.')
        if args.action == 'verify-restart':
            assert receipt.get('interrupted_at'), 'No recorded consumer interruption to verify.'
            assert marker['pid'] != receipt['initial_pid']
        if action == 'cancel':
            assert receipt.get('cancel_response', {}).get('cancel_requested'), 'Cancellation intent was not confirmed.'
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            run = call(consumer, 'GET', 'ask/runs/' + task)
            if run['state'] in ('succeeded', 'cancelled', 'failed', 'timed_out', 'interrupted'):
                break
            time.sleep(0.1)
        else:
            raise AssertionError('Original run did not reach a confirmed terminal state.')
        conversation = call(consumer, 'GET', path)
        assert len(conversation['messages']) == 2
        assert {m['taskId'] for m in conversation['messages']} == {task}
        repeated = call(consumer, 'POST', 'ask/runs', json=receipt['request'])
        assert repeated['task_id'] == task and repeated['state'] == run['state']
        orders = call(provider, 'GET', 'llm/provider-orders')['orders']
        assert len([r for r in orders if r['task_id'] == task]) == 1
        receipt['verified'] = {'at': datetime.now(UTC).isoformat(), 'run': run,
            'pid': marker['pid'], 'message_count': 2, 'provider_order_count': 1,
            'same_request_returned_original': True}
        atomic_write_json(receipt_path, receipts)
        output = Path(__file__).resolve().parents[1] / 'docs/acceptance/ask-ryn-development/running-recovery-20260914.json'
        safe = {key: {k: v for k, v in value.items() if k != 'request'} for key, value in receipts.items()}
        atomic_write_json(output, {'scope': 'Production owner APIs, real remote native CPU model on loopback; no inference stub; not browser cancel or GPU acceptance', 'actions': safe})
        print(json.dumps({'action': args.action, **receipt['verified']}))


if __name__ == '__main__':
    main()
