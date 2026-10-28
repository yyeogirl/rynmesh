"""Real encrypted files, interrupted writes, receipts and scope boundaries."""
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from test_device_sync_records import bookmark, reading

from rynmesh.atomic_io import atomic_write_json, read_json
from rynmesh.device_sync import records
from rynmesh.device_sync import store as storage
from rynmesh.device_sync.records import SyncError
from rynmesh.device_sync.store import ReplicaStore
from rynmesh.services.peer_box import load_or_create_messaging_key


def replica(path):
    return ReplicaStore(path, messaging_key=load_or_create_messaging_key(path / 'messaging.x25519'))


def put(store, scope, value):
    return store.write(scope, 'article', value, expected_revision=store.read(scope, 'article')['revision'])


def transfer(source, target, scopes):
    batch = source.pending(target.actor, scopes)
    receipt = target.receive(batch['records'], scopes=scopes)
    source.acknowledge(target.actor, receipt, scopes=scopes)
    return batch, receipt


def test_scope_filtering_explicit_receipts_and_restart(tmp_path):
    a, b = replica(tmp_path / 'a'), replica(tmp_path / 'b')
    put(a, 'bookmarks', bookmark())
    put(a, 'reading', reading(0.4))
    batch = a.pending(b.actor, ['bookmarks'])
    assert len(batch['records']) == 1 and batch['records'][0]['scope'] == 'bookmarks'
    receipt = b.receive(batch['records'], scopes=['bookmarks'])
    assert a.pending(b.actor, ['bookmarks'])['pending'] == 1  # Sending isn't acknowledgement.
    assert a.acknowledge(b.actor, receipt, scopes=['bookmarks']) == {'acknowledged': 1, 'outdated': 0}
    assert a.pending(b.actor, ['bookmarks'])['pending'] == 0
    assert a.pending(b.actor, ['reading'])['pending'] == 1
    restarted = replica(tmp_path / 'b')
    assert restarted.read('bookmarks', 'article')['bookmarked'] is True
    assert restarted.read('reading', 'article')['candidates'] == []
    assert b'A local article' not in a.path.read_bytes()
    assert b'example.test' not in b.path.read_bytes()
    assert a.pending(b.actor, []) == {'records': [], 'pending': 0}
    a.forget_device(b.actor)
    assert a.pending(b.actor, ['bookmarks'])['pending'] == 1


def test_late_receipt_does_not_confirm_newer_local_value(tmp_path):
    a, b = replica(tmp_path / 'a'), replica(tmp_path / 'b')
    put(a, 'bookmarks', bookmark())
    receipt = b.receive(a.pending(b.actor, ['bookmarks'])['records'], scopes=['bookmarks'])
    put(a, 'bookmarks', bookmark(False))
    assert a.acknowledge(b.actor, receipt, scopes=['bookmarks']) == {'acknowledged': 0, 'outdated': 1}
    assert a.pending(b.actor, ['bookmarks'])['pending'] == 1
    transfer(a, b, ['bookmarks'])
    assert not b.read('bookmarks', 'article')['bookmarked']


def test_combined_source_import_rejects_invalid_ack_and_scope_before_any_commit(tmp_path):
    a, b = replica(tmp_path / 'a'), replica(tmp_path / 'b')
    put(a, 'bookmarks', bookmark())
    put(a, 'reading', reading(.5))
    rows = a.pending(b.actor, ['bookmarks', 'reading'])['records']
    for operation in (
        lambda: b.source_pending(a.actor, rows, scopes=['bookmarks']),
        lambda: b.source_acknowledge(a.actor, rows, [{'scope': 'bookmarks', 'id': 'missing', 'revision': 'a' * 64}], scopes=['bookmarks', 'reading']),
        lambda: b.source_acknowledge(a.actor, rows, [{}] * 101, scopes=['bookmarks', 'reading']),
    ):
        with pytest.raises(SyncError):
            operation()
        assert not b.path.exists()
    assert b.source_pending(a.actor, rows, scopes=['bookmarks', 'reading'])['pending'] == 2


def test_unselected_batch_is_rejected_atomically_and_disk_failure_has_no_receipt(tmp_path, monkeypatch):
    a, b = replica(tmp_path / 'a'), replica(tmp_path / 'b')
    put(a, 'bookmarks', bookmark())
    put(a, 'reading', reading(0.6))
    batch = a.pending(b.actor, ['bookmarks', 'reading'])['records']
    with pytest.raises(SyncError, match='sync_scope_denied'):
        b.receive(batch, scopes=['bookmarks'])
    assert not b.path.exists()
    def disk_full(*args, **kwargs):
        raise OSError('disk full')
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'atomic_write_json', disk_full)
        with pytest.raises(OSError):
            b.receive(batch, scopes=['bookmarks', 'reading'])
    assert not b.path.exists()
    assert a.pending(b.actor, ['bookmarks', 'reading'])['pending'] == 2
    transfer(a, b, ['bookmarks', 'reading'])
    assert b.read('reading', 'article')['value']['progress'] == 0.6


def test_stale_ui_cannot_resolve_over_an_unseen_remote_change(tmp_path):
    a, b = replica(tmp_path / 'a'), replica(tmp_path / 'b')
    current = put(a, 'reading', reading(0.4))
    transfer(a, b, ['reading'])
    put(b, 'reading', reading(0.7))
    transfer(b, a, ['reading'])
    with pytest.raises(SyncError, match='sync_revision_conflict'):
        a.write('reading', 'article', reading(0.2), expected_revision=current['revision'])
    assert a.read('reading', 'article')['value']['progress'] == 0.7


def test_future_and_corrupt_envelopes_are_preserved(tmp_path):
    a = replica(tmp_path / 'a')
    put(a, 'bookmarks', bookmark())
    original = read_json(a.path)
    atomic_write_json(a.path, original | {'version': 'ryn.device-replica.v999'})
    before = a.path.read_bytes()
    with pytest.raises(SyncError, match='sync_version_unsupported'):
        a.receive([], scopes=['bookmarks'])
    assert a.path.read_bytes() == before
    atomic_write_json(a.path, original | {'ciphertext': 'broken'})
    before = a.path.read_bytes()
    with pytest.raises(SyncError, match='sync_store_unavailable'):
        a.read('bookmarks', 'article')
    assert a.path.read_bytes() == before


def test_unknown_local_fields_survive_mutation_but_unknown_wire_fields_are_denied(tmp_path):
    a, b = replica(tmp_path / 'a'), replica(tmp_path / 'b')
    put(a, 'bookmarks', bookmark())
    a._mutate(lambda data: data.update(future_local_metadata={'keep': True}))
    entity_key = a._key('bookmarks', 'article')
    a._mutate(lambda data: data['records'][entity_key].update(future_entity_metadata='private local extension'))
    envelope = read_json(a.path)
    atomic_write_json(a.path, envelope | {'future_envelope_metadata': 'keep'})
    put(a, 'reading', reading(0.5))
    assert a._read()[1]['future_local_metadata'] == {'keep': True}
    assert read_json(a.path)['future_envelope_metadata'] == 'keep'
    batch = a.pending(b.actor, ['bookmarks'])['records']
    assert 'future_entity_metadata' not in batch[0]
    put(a, 'bookmarks', bookmark(False))
    assert a._read()[1]['records'][entity_key]['future_entity_metadata'] == 'private local extension'
    a.receive(batch, scopes=['bookmarks'])
    assert a._read()[1]['records'][entity_key]['future_entity_metadata'] == 'private local extension'
    forged = deepcopy(batch)
    forged[0]['record']['heads'][0]['value']['private_key'] = 'Do not propagate'
    with pytest.raises(SyncError, match='sync_value_invalid'):
        b.receive(forged, scopes=['bookmarks'])
    assert not b.path.exists()


def test_failed_multi_record_capture_does_not_commit_a_prefix(tmp_path):
    a = replica(tmp_path / 'a')
    empty_revision = records.fingerprint(records.empty())
    with pytest.raises(SyncError, match='sync_revision_conflict'):
        a.write_many([{'scope': 'bookmarks', 'id': 'article', 'value': bookmark(), 'expected_revision': empty_revision},
                      {'scope': 'reading', 'id': 'article', 'value': reading(0.4), 'expected_revision': 'stale'}])
    assert not a.path.exists()


def test_response_loss_after_durable_commit_is_safe_to_retry(tmp_path, monkeypatch):
    a, b = replica(tmp_path / 'a'), replica(tmp_path / 'b')
    put(a, 'bookmarks', bookmark())
    batch = a.pending(b.actor, ['bookmarks'])['records']
    real_write = storage.atomic_write_json
    def commit_then_fail(*args, **kwargs):
        real_write(*args, **kwargs)
        raise OSError('simulated process interruption before receipt')
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'atomic_write_json', commit_then_fail)
        with pytest.raises(OSError):
            b.receive(batch, scopes=['bookmarks'])
    committed = b.path.read_bytes()
    restarted = replica(tmp_path / 'b')
    receipt = restarted.receive(batch, scopes=['bookmarks'])
    assert restarted.path.read_bytes() == committed
    a.acknowledge(b.actor, receipt, scopes=['bookmarks'])
    assert a.pending(b.actor, ['bookmarks'])['pending'] == 0


def test_reconcile_does_not_treat_python_numeric_equality_as_identical_operation(tmp_path):
    a, b = replica(tmp_path / 'a'), replica(tmp_path / 'b')
    put(a, 'reading', reading(0))
    batch = a.pending(b.actor, ['reading'])['records']
    b.reconcile_source(batch, scopes=['reading'])
    before = b.path.read_bytes()
    # The same operation dot cannot acquire a differently encoded value.
    changed = deepcopy(batch)
    old = changed[0]['record']['heads'][0]['value']['progress']
    changed[0]['record']['heads'][0]['value']['progress'] = float(old) if type(old) is int else int(old)
    with pytest.raises(SyncError, match='sync_dot_conflict'):
        b.reconcile_source(changed, scopes=['reading'])
    assert b.path.read_bytes() == before


def test_parallel_instances_cannot_both_write_the_same_revision(tmp_path):
    replicas = [replica(tmp_path / 'a') for _ in range(8)]
    revision = records.fingerprint(records.empty())
    def write(store):
        try:
            store.write('reading', 'article', reading(0.4), expected_revision=revision)
            return 'saved'
        except SyncError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(write, replicas))
    assert results.count('saved') == 1 and results.count('sync_revision_conflict') == 7


def test_one_hundred_new_changes_after_ten_thousand_record_baseline(tmp_path):
    """Storage-layer timing only; pairing, real network and projections are absent."""
    a, b = replica(tmp_path / 'a'), replica(tmp_path / 'b')
    baseline = {}
    for index in range(10000):
        identifier = f'baseline-{index}'
        value = bookmark()
        value['item'] = value['item'] | {'item_id': identifier}
        baseline[a._key('bookmarks', identifier)] = {'scope': 'bookmarks', 'id': identifier,
            'record': records.write('bookmarks', identifier, records.empty(), a.actor, value)}
    # Seed the state of two replicas that already acknowledged their initial
    # merge. The benchmark starts after that baseline, as SYNC04 specifies.
    a._mutate(lambda data: data.update(records=deepcopy(baseline), receipts={b.actor: {key: records.fingerprint(row['record']) for key, row in baseline.items()}}))
    b._mutate(lambda data: data.update(records=deepcopy(baseline)))
    changes = []
    for index in range(100):
        identifier = f'added-{index}'
        value = bookmark()
        value['item'] = value['item'] | {'item_id': identifier}
        changes.append({'scope': 'bookmarks', 'id': identifier, 'value': value, 'expected_revision': records.fingerprint(records.empty())})
    start = time.perf_counter()
    a.write_many(changes)
    batch, _ = transfer(a, b, ['bookmarks'])
    assert batch['pending'] == len(batch['records']) == 100
    assert a.pending(b.actor, ['bookmarks'])['pending'] == 0
    elapsed = time.perf_counter() - start
    assert len(b._read()[1]['records']) == 10100
    assert b.read('bookmarks', 'added-99')['bookmarked']
    print(f' storage-only 100 changes with 10000 prior rows: {elapsed:.3f}s')
    assert elapsed < 60
