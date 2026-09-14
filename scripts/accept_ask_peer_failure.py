"""Reopen existing paired acceptance nodes for real ICE and peer failure QA.

--hold-ice holds a real gathered ICE connection on the consumer's fixed port.
Creating release-ice in the dedicated control directory closes it normally.
No request, discovery response, task result, or model adapter is substituted.
watch-provider-running only records a new running task; it does not stop it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from accept_local_search import configure

CONTROL = Path('D:/code/rynmesh-peer-failure-acceptance-20260914')
NODES = {
    'consumer': (18924, Path('D:/code/rynmesh-ai-recovery-acceptance')),
    'provider': (18846, Path('D:/code/rynmesh-local-ai-acceptance-c124bb62d6654226903a51badd14d528')),
}


def stop_provider_on_running(*, stop=True):
    import httpx
    marker = CONTROL / 'provider.json'
    original = marker.read_text()
    pid = json.loads(original)['pid']
    receipt = CONTROL / ('provider-running-interruption.json' if stop else 'provider-browser-cancel-running.json')
    assert not receipt.exists(), 'This one-shot running observation already has a receipt.'
    with httpx.Client(base_url='http://127.0.0.1:18846/api/local/', trust_env=False, timeout=5) as client:
        def get(path):
            response = client.get(path)
            response.raise_for_status()
            return response.json()
        assert Path(get('privacy/status')['storage_root']).resolve() == NODES['provider'][1].resolve()
        baseline = {row['task_id'] for row in get('llm/provider-orders')['orders']}
        print(json.dumps({'state': 'watching_new_browser_task', 'provider_pid': pid}), flush=True)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            rows = get('llm/provider-orders')['orders']
            running = next((row for row in rows if row['task_id'] not in baseline and row['state'] == 'running'), None)
            if running:
                assert marker.read_text() == original
                value = {'task_id': running['task_id'], 'observed_state': 'running',
                    'at': datetime.now(UTC).isoformat(), 'provider_pid': pid}
                receipt.write_text(json.dumps(value) + '\n')
                if stop:
                    os.kill(pid, signal.SIGTERM)
                print(json.dumps(value), flush=True)
                return
            time.sleep(0.05)
    raise AssertionError('No new browser task reached running within 60 seconds.')


async def hold_connection():
    from rynmesh.atomic_io import atomic_write_json
    from rynmesh.llm_package.p2p import new_connection
    connection = new_connection(controlling=True)
    try:
        await connection.gather_candidates()
        assert connection.local_candidates
        atomic_write_json(CONTROL / 'ice-holder.json', {'state': 'held', 'port': 18961,
            'candidate_count': len(connection.local_candidates)})
        while not (CONTROL / 'release-ice').exists():
            await asyncio.sleep(0.2)
    finally:
        await connection.close()
    again = new_connection(controlling=True)
    try:
        await again.gather_candidates()
        assert again.local_candidates
        atomic_write_json(CONTROL / 'ice-holder.json', {'state': 'released_and_rebound', 'port': 18961,
            'candidate_count': len(again.local_candidates)})
    finally:
        await again.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('node', choices=[*NODES, 'stop-provider-on-running', 'watch-provider-running'])
    parser.add_argument('--transport', choices=['auto', 'direct', 'p2p'], default='auto')
    parser.add_argument('--hold-ice', action='store_true')
    args = parser.parse_args()
    if args.node == 'stop-provider-on-running':
        return stop_provider_on_running()
    if args.node == 'watch-provider-running':
        return stop_provider_on_running(stop=False)
    port, home = NODES[args.node]
    assert (home / 'llm/provider-settings.json').is_file()
    CONTROL.mkdir(exist_ok=True)
    configure(home, port)
    os.environ.update(RYNMESH_LLM_HOME=str(home / 'llm'), RYNMESH_PEER_ENDPOINT=f'http://127.0.0.1:{port}',
        RYNMESH_PEER_PORT=str(port), RYNMESH_REGISTRY_DIR=str(CONTROL / 'registry'),
        RYNMESH_LLM_TRANSPORT=args.transport, RYNMESH_LLM_FORCE_RELAY='0',
        RYNMESH_P2P_STUN='off', RYNMESH_P2P_REQUIRE_PUBLIC='0',
        RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC='0',
        RYNMESH_P2P_BIND_PORT='18961' if args.node == 'consumer' else '')
    import uvicorn

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore
    app = create_app(RynmeshStore(home=home, network_dir=home / 'network', node_name=f'Peer failure {args.node}'))
    if args.hold_ice:
        assert args.node == 'consumer' and args.transport == 'p2p'
        assert not (CONTROL / 'release-ice').exists(), 'Remove only the dedicated release marker before a new hold.'
        threading.Thread(target=lambda: asyncio.run(hold_connection()), daemon=True).start()
    (CONTROL / f'{args.node}.json').write_text(json.dumps({'pid': os.getpid(), 'port': port, 'transport': args.transport}))
    uvicorn.run(app, host='127.0.0.1', port=port, access_log=False)


if __name__ == '__main__':
    main()
