"""Read bounded metadata from the two existing real-model acceptance nodes."""
import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

NODES = {
    'consumer': (18924, 'rynmesh-ai-recovery-acceptance'),
    'provider': (18846, 'rynmesh-local-ai-acceptance-c124bb62d6654226903a51badd14d528'),
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', required=True)
    parser.add_argument('--peer-failures', action='store_true', help='Write a separate ICE and peer failure evidence file.')
    parser.add_argument('--cancel-timeout', action='store_true', help='Write separate browser cancellation/timeout evidence.')
    parser.add_argument('--consumer-only', action='store_true', help='Observe the consumer while the provider is deliberately stopped.')
    args = parser.parse_args()
    observation = {'label': args.label, 'at': datetime.now(UTC).isoformat(), 'nodes': {}}
    for name, (port, directory) in NODES.items():
        if args.consumer_only and name != 'consumer':
            continue
        with httpx.Client(base_url=f'http://127.0.0.1:{port}/api/local/', timeout=10, trust_env=False) as client:
            def read(path):
                response = client.get(path)
                response.raise_for_status()
                return response.json()
            assert Path(read('privacy/status')['storage_root']).resolve() == Path('D:/code', directory).resolve()
            row = {'conversations': [], 'orders': {}, 'grants': []}
            for conversation in read('ask/conversations')['conversations']:
                row['conversations'].append({**{k: conversation.get(k) for k in ('id', 'providerPeerId', 'serviceKey', 'contextIds')},
                    'digest': digest(conversation), 'message_count': len(conversation['messages']),
                    'draft_bytes': len(conversation.get('draft', '').encode()),
                    'messages': [{**{k: m.get(k) for k in ('id', 'role', 'taskId', 'status', 'cost', 'contextIds', 'promptSha256')},
                                  'digest': digest(m.get('content', ''))} for m in conversation['messages']]})
            for kind in ('orders', 'provider-orders'):
                row['orders'][kind] = [{**{k: o.get(k) for k in ('task_id', 'state', 'provider_peer_id', 'service_id', 'amount', 'transport', 'error_code')},
                                        **({'last_error_code': next((event.get('error_code') for event in reversed(o.get('history') or []) if event.get('error_code')), None)} if args.peer_failures else {}),
                                        **({'transport_evidence': {k: o['transport_evidence'].get(k) for k in ('transport', 'path_kind', 'relay_used', 'request_bytes', 'response_bytes', 'public_nat_traversal_required', 'distinct_public_egress_required')}}
                                           if args.peer_failures and isinstance(o.get('transport_evidence'), dict) else {})}
                                       for o in read('llm/' + kind)['orders']]
            row['grants'] = [{k: g.get(k) for k in ('relationship_id', 'service_id', 'peer_id', 'allowed', 'revision', 'effective')}
                             for g in read('ai-access')['grants']]
            balance = read('task-balance')
            row['balance'] = {k: v for k, v in balance.items() if isinstance(v, (int, float))}
            row['balance_events'] = [{k: e.get(k) for k in ('task_id', 'type', 'kind', 'amount', 'service_id', 'provider_peer_id')}
                                     for e in balance.get('events', [])]
            service = read('llm/service/status')
            row['service'] = {k: service.get(k) for k in ('ready', 'publication_enabled')}
            observation['nodes'][name] = row
    filename = 'cancel-timeout-checkpoints-20260914.json' if args.cancel_timeout else 'peer-failure-checkpoints-20260914.json' if args.peer_failures else 'model-switch-checkpoints-20260914.json'
    path = Path(__file__).resolve().parents[1] / 'docs/acceptance/ask-ryn-development' / filename
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'scope': 'Actual browser operations on two existing isolated native-model nodes; loopback not public NAT', 'observations': []}
    data['observations'].append(observation)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'label': args.label, 'nodes': {name: {'conversations': len(row['conversations']),
        'consumer_orders': len(row['orders']['orders']), 'provider_orders': len(row['orders']['provider-orders'])}
        for name, row in observation['nodes'].items()}}))


if __name__ == '__main__':
    main()
