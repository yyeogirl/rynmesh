"""Measure real source commits, encrypted TCP transfer and source receipts.

Uses two isolated loopback FastAPI route packages with real stores and keys.
Synthetic legacy baselines are written once, then migrated through enable_sync.
No receipts are seeded: initial merge and later changes traverse normal routes.
The driver calls the registered worker's transfer operation directly, excluding
scheduler delay, unrelated node features, UI, physical devices and cross-NAT.
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


class Node:
    def __init__(self, home, *, port=0):
        import uvicorn
        from fastapi import FastAPI, HTTPException

        from rynmesh.ask_ryn.store import ConversationStore
        from rynmesh.background_workers import BackgroundWorkerRegistry
        from rynmesh.device_sync.routes import install_device_sync
        from rynmesh.peer_http import HttpPeerClient
        from rynmesh.services import peer_box
        from rynmesh.services.consumption import ConsumptionStore
        from rynmesh.store import RynmeshStore

        self.home, self.bytes_sent, self.bytes_received = home, 0, 0
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', port))
        self.endpoint = f'http://127.0.0.1:{self.socket.getsockname()[1]}'
        self.store = RynmeshStore(home=home, network_dir=home / 'network', node_name=home.name)
        self.key = peer_box.load_or_create_messaging_key(home / 'messaging.x25519')
        self.reader = ConsumptionStore(home / 'consumption.json')
        self.history = ConversationStore(home / 'ask', self.key)
        self.app, self.workers = FastAPI(), BackgroundWorkerRegistry()

        def owner(request):
            raise HTTPException(403, detail='benchmark_owner_api_disabled')

        def post(endpoint, path, value):
            from rynmesh.crypto import canonical_json
            from rynmesh.device_sync.transfer import MAX_WIRE_BYTES
            # Preserve the production five-second network deadline.
            self.bytes_sent += len(canonical_json(value))
            result = HttpPeerClient(endpoint, timeout_s=5).post_json(path, value, max_bytes=MAX_WIRE_BYTES)
            self.bytes_received += len(canonical_json(result))
            return result

        self.pairing = install_device_sync(self.app, store=self.store, home=home, workers=self.workers,
            local_control=owner, messaging_key=self.key, post_json=post, endpoint=self.endpoint, allow_loopback=True,
            reading=lambda: self.reader, conversations=lambda: self.history)
        self.transfer = self.app.state.device_sync.transfer
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level='error', access_log=False, lifespan='off'))
        self.thread = threading.Thread(target=lambda: self.server.run(sockets=[self.socket]), daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError('benchmark_server_start_failed')
            time.sleep(.02)

    def close(self):
        self.server.should_exit = True
        self.thread.join(timeout=8)
        self.socket.close()


def seed(node, start, stop):
    from rynmesh.atomic_io import atomic_write_json
    from rynmesh.services.consumption import MAX_HISTORY_BYTES

    rows = {}
    for index in range(start, stop):
        identifier = f'sync-scale-{index:05d}'
        rows[identifier] = {'item_id': identifier, 'item': {'item_id': identifier,
            'title': f'Synthetic reading record {index}', 'source_title': 'Scale acceptance',
            'link': f'https://example.test/sync-scale/{index}', 'content_kind': 'article'},
            'bookmarked': True, 'progress': 0, 'completed': False, 'open_count': 0,
            'first_opened_unix': 0, 'last_opened_unix': 0, 'last_activity_unix': 0}
    atomic_write_json(node.reader.path, rows, max_bytes=MAX_HISTORY_BYTES)
    node.reader.enable_sync(node.transfer.replica.actor, ['bookmarks'])


def converge(left, right, pair_id, *, expected_changes=None):
    start = time.perf_counter()
    rounds, confirmed = 0, 0
    for node in (left, right):
        while True:
            status = node.transfer.status(pair_id)
            if status['pending'] is None:
                raise RuntimeError('benchmark_source_unavailable')
            if status['pending'] == 0 and status['state'] == 'confirmed':
                break
            result = node.transfer.send(pair_id, 'bookmarks')
            rounds += 1
            confirmed += result['acknowledged']
            print(json.dumps({'stage': 'transfer', 'node': node.home.name, 'round': rounds,
                'pending_before': status['pending'], 'confirmed': result['acknowledged'],
                'elapsed_seconds': round(time.perf_counter() - start, 3)}), flush=True)
            if rounds > 1000:
                raise RuntimeError('benchmark_convergence_limit')
    # A receives B's initial distinct data after its own first send pass.
    pending = left.transfer.status(pair_id)['pending']
    if pending:
        while pending:
            result = left.transfer.send(pair_id, 'bookmarks')
            rounds += 1
            confirmed += result['acknowledged']
            pending = left.transfer.status(pair_id)['pending']
            print(json.dumps({'stage': 'final_confirmation', 'pending': pending}), flush=True)
    assert all(node.transfer.status(pair_id)['state'] == 'confirmed' for node in (left, right))
    if expected_changes is not None:
        assert confirmed == expected_changes * 2
    return {'seconds': time.perf_counter() - start, 'batches': rounds, 'acknowledged': confirmed}


def clear_reading_source(node):
    """Exercise the production reviewed cleanup on this synthetic source only."""
    from rynmesh.atomic_io import read_json
    from rynmesh.local_search.index import LocalSearchIndex
    from rynmesh.services.consumption import MAX_SYNC_HISTORY_BYTES, PRIVACY_VERSION
    from rynmesh.services.reading_cleanup import ReadingCleanup

    # Search population is outside this benchmark; use a real empty index.
    search = LocalSearchIndex(node.home / 'local-search', messaging_key=node.key, source=lambda: [])
    cleanup = ReadingCleanup(node.home, source=node.reader, replica=node.transfer.replica,
        pairing_lock=node.home / 'device-sync' / '.pairings.lock', search=search)
    started = time.perf_counter()
    review = cleanup.preview()
    outcome = cleanup.begin(review_token=review['review_token'])
    assert outcome['local_copies_complete'] and not outcome['remote_confirmed']
    assert node.reader.list() == []
    assert read_json(node.reader.path, max_bytes=MAX_SYNC_HISTORY_BYTES)['version'] == PRIVACY_VERSION
    return {'seconds': time.perf_counter() - started, 'local_copies_complete': True,
        'source_version': PRIVACY_VERSION, 'search_populated': False}


def measure(home, count, changes, result, *, baseline=None, reading_cleanup=False, resume_transfer=False):
    from rynmesh.crypto import canonical_json

    ports, pair_id = {}, None
    if baseline:
        from rynmesh.device_sync.pair_store import PairingStore
        from rynmesh.services.peer_box import load_or_create_messaging_key

        if not baseline.is_dir() or not baseline.name.startswith('rynmesh-device-sync-scale-'):
            raise ValueError('benchmark_baseline_not_synthetic')
        shutil.copytree(baseline, home)
        endpoints = {}
        for name in ('A', 'B'):
            key_path = home / name / 'messaging.x25519'
            if not key_path.is_file():
                raise ValueError('benchmark_baseline_identity_missing')
            pairs = PairingStore(home / name, load_or_create_messaging_key(key_path)).snapshot()['pairs']
            active = [row for row in pairs.values() if row['status'] == 'active']
            if len(active) != 1 or (pair_id and active[0]['id'] != pair_id):
                raise ValueError('benchmark_baseline_pair_mismatch')
            pair_id = active[0]['id']
            row = active[0]
            parsed = urlsplit(row['local']['endpoint'])
            if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port:
                raise ValueError('benchmark_baseline_endpoint_invalid')
            ports[name] = parsed.port
            endpoints[name] = (row['local']['endpoint'], row['remote']['endpoint'])
        if endpoints['A'] != tuple(reversed(endpoints['B'])) or ports['A'] == ports['B']:
            raise ValueError('benchmark_baseline_endpoint_mismatch')

    left, right = Node(home / 'A', port=ports.get('A', 0)), None
    try:
        right = Node(home / 'B', port=ports.get('B', 0))
        if baseline is None:
            invite = left.pairing.create_invite(['bookmarks'])
            pair = right.pairing.join(invite['uri'], ['bookmarks'])
            pair_id = pair['id']
            left.pairing.approve(pair_id, review_token=pair['review_token'], scopes=['bookmarks'])
            right.pairing.retry(pair_id)
            result['phase'] = 'baseline_migration'
            started = time.perf_counter()
            seed(left, 0, count // 2)
            seed(right, count // 2, count)
            result['baseline_migration_seconds'] = time.perf_counter() - started
            print(json.dumps({'stage': result['phase'], 'seconds': result['baseline_migration_seconds']}), flush=True)
            result['phase'] = 'initial_merge'
            result['initial_merge'] = converge(left, right, pair_id)
        else:
            if resume_transfer:
                result['phase'] = 'resume_persisted_transfer'
                result['resumed_transfer'] = converge(left, right, pair_id)
            result['phase'] = 'verify_persisted_baseline'
            assert all(node.transfer.status(pair_id)['state'] == 'confirmed' for node in (left, right))
        for node in (left, right):
            rows = node.reader.sync_export(['bookmarks'])
            assert len(rows) == count
            assert all(len(row['record']['heads']) == 1 for row in rows)
            saved_ids = {row['id'] for row in rows if row['record']['heads'][0]['value'] and row['record']['heads'][0]['value']['bookmarked']}
            # A previously cancelled bookmark need not remain in recent history;
            # its causal metadata must still be present in the source above.
            assert {row['item_id'] for row in node.reader.list() if row['bookmarked']} == saved_ids
        initial = left.reader.sync_export(['bookmarks'])
        assert canonical_json(initial) == canonical_json(right.reader.sync_export(['bookmarks']))
        if reading_cleanup:
            result['phase'] = 'reviewed_reading_cleanup'
            result['reading_cleanup'] = clear_reading_source(left)
            print(json.dumps({'stage': result['phase'], **result['reading_cleanup']}), flush=True)
            result['phase'] = 'cleanup_transfer'
            result['cleanup_transfer'] = converge(left, right, pair_id)
            initial = left.reader.sync_export(['bookmarks'])
            assert len(initial) == count
            assert canonical_json(initial) == canonical_json(right.reader.sync_export(['bookmarks']))
            assert all(len(row['record']['heads']) == 1 and row['record']['heads'][0]['value'] is None for row in initial)
            assert all(not any(row['bookmarked'] for row in node.reader.list()) for node in (left, right))
        selected = [row for row in initial if int(row['id'].rsplit('-', 1)[1]) < changes]
        assert len(selected) == changes
        before_values = {bool(row['record']['heads'][0]['value'] and row['record']['heads'][0]['value']['bookmarked']) for row in selected}
        assert len(before_values) == 1
        bookmarked = not before_values.pop()
        result['change_action'] = 'bookmark' if bookmarked else 'unbookmark'
        result['phase'] = 'incremental_source_writes'
        before_bytes = sum(node.bytes_sent + node.bytes_received for node in (left, right))
        started = time.perf_counter()
        for index in range(changes):
            identifier = f'sync-scale-{index:05d}'
            left.reader.record({'item_id': identifier, 'title': f'Synthetic reading record {index}',
                'source_title': 'Scale acceptance', 'link': f'https://example.test/sync-scale/{index}',
                'content_kind': 'article'}, result['change_action'])
            if (index + 1) % 10 == 0:
                print(json.dumps({'stage': result['phase'], 'written': index + 1,
                    'elapsed_seconds': round(time.perf_counter() - started, 3)}), flush=True)
        result['source_write_seconds'] = time.perf_counter() - started
        result['phase'] = 'incremental_transfer'
        result['incremental_transfer'] = converge(left, right, pair_id, expected_changes=changes)
        result['incremental_total_seconds'] = time.perf_counter() - started
        result['incremental_json_bytes'] = sum(node.bytes_sent + node.bytes_received for node in (left, right)) - before_bytes
        for node in (left, right):
            rows = node.reader.sync_export(['bookmarks'])
            assert len(rows) == count
            changed = [row for row in rows if int(row['id'].rsplit('-', 1)[1]) < changes]
            assert len(changed) == changes and all(row['record']['heads'][0]['value']['bookmarked'] is bookmarked for row in changed)
        result.update(phase='finished', status='measured', target_met=result['incremental_total_seconds'] <= 60)
        from rynmesh.atomic_io import read_json
        from rynmesh.services.consumption import MAX_SYNC_HISTORY_BYTES

        result['source_versions'] = {node.home.name: read_json(node.reader.path,
            max_bytes=MAX_SYNC_HISTORY_BYTES)['version'] for node in (left, right)}
    finally:
        if right:
            right.close()
        left.close()


def main():
    from rynmesh.atomic_io import atomic_write_json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--records', type=int, default=10000)
    parser.add_argument('--changes', type=int, default=100)
    parser.add_argument('--baseline-home', type=Path, help='Copy a stopped, genuinely confirmed synthetic baseline; do not remeasure initial merge or seed receipts')
    parser.add_argument('--reading-cleanup', action='store_true',
        help='Clear A through reviewed production cleanup, confirm deletions, then measure new v4 bookmark writes to B')
    parser.add_argument('--resume-baseline-transfer', action='store_true',
        help='Resume incomplete real receipts in a copied baseline before measuring incremental writes; do not repeat cleanup')
    args = parser.parse_args()
    home = args.home.resolve()
    if home.exists() or not home.name.startswith('rynmesh-device-sync-scale-'):
        raise SystemExit('Use a new rynmesh-device-sync-scale-* directory.')
    if not 100 <= args.records <= 10000 or not 1 <= args.changes <= min(100, args.records // 2):
        raise SystemExit('Use 100–10000 records and 1–100 changes, no more than half the baseline.')
    if args.resume_baseline_transfer and (not args.baseline_home or args.reading_cleanup):
        raise SystemExit('Resuming requires a baseline and must not repeat reading cleanup.')
    result = {'recorded_at_unix': time.time(), 'platform': platform.platform(), 'python': platform.python_version(),
        'records_per_converged_source': args.records, 'changed_records': args.changes,
        'scope': 'Bookmarks; two loopback route packages; no scheduler wait/UI/other workers/cross-NAT',
        'baseline': 'Disjoint synthetic legacy files migrated through source API; no seeded receipts',
        'target_seconds': 60, 'status': 'running', 'phase': 'pairing'}
    if args.baseline_home:
        result['baseline'] = 'Copy of persisted synthetic paired source/replica state from an earlier real TCP run; confirmed state verified, no receipts created by the harness'
        result['initial_merge_remeasured'] = False
    if args.reading_cleanup:
        result['storage_scenario'] = 'A upgraded by reviewed reading cleanup to v4; B retains v3; cleanup and deletion transfer measured separately before incremental writes'
    if args.resume_baseline_transfer:
        result['baseline'] = 'Copy of interrupted synthetic TCP state; persisted receipts resumed and confirmed before incremental writes; no receipts seeded and no repeated cleanup'
    try:
        measure(home, args.records, args.changes, result,
            baseline=args.baseline_home.resolve() if args.baseline_home else None,
            reading_cleanup=args.reading_cleanup, resume_transfer=args.resume_baseline_transfer)
    except Exception as exc:
        result.update(status='failed', error_type=type(exc).__name__, target_met=False)
        causes, error = [], exc
        while error is not None and len(causes) < 8:
            causes.append(type(error).__name__)
            error = error.__cause__
        result['error_types'] = causes  # No exception text, URLs or request/response bodies.
        print(json.dumps({'stage': result['phase'], 'error_type': result['error_type']}), flush=True)
    finally:
        atomic_write_json(args.output, result)
        print(json.dumps(result), flush=True)
    if result['status'] != 'measured':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
