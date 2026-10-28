from concurrent.futures import ThreadPoolExecutor

import pytest
from test_device_sync_reading import ITEM, SCOPES
from test_device_sync_store import replica

from rynmesh.device_sync import store as replica_storage
from rynmesh.device_sync.reading_bridge import ReadingBridge
from rynmesh.device_sync.records import SyncError
from rynmesh.services import consumption
from rynmesh.services.consumption import ConsumptionStore


def device(path, *, enable=True):
    target = replica(path)
    source = ConsumptionStore(path / 'consumption.json')
    if enable:
        source.enable_sync(target.actor, SCOPES)
    return ReadingBridge(source, target)


def test_outbox_recovery_and_acknowledgement_mean_source_is_readable(tmp_path):
    a, b = device(tmp_path / 'a'), device(tmp_path / 'b')
    a.source.record(ITEM, 'bookmark')
    a.source.record(ITEM, 'progress', progress=.7)
    assert not a.replica.path.exists()  # Local edits need no replica write.
    a = device(tmp_path / 'a', enable=False)
    batch = a.pending(b.replica.actor, SCOPES)
    assert batch['pending'] == 2
    receipt = b.receive(batch['records'], scopes=SCOPES)
    b = device(tmp_path / 'b', enable=False)
    assert b.source.list()[0]['bookmarked'] and b.source.list()[0]['progress'] == .7
    assert a.pending(b.replica.actor, SCOPES)['pending'] == 2
    assert a.acknowledge(b.replica.actor, receipt, scopes=SCOPES)['acknowledged'] == 2
    assert a.pending(b.replica.actor, SCOPES)['pending'] == 0


@pytest.mark.parametrize('failure_location', ['source', 'replica'])
def test_interrupted_receiver_does_not_acknowledge_and_replays_after_restart(tmp_path, monkeypatch, failure_location):
    a, b = device(tmp_path / 'a'), device(tmp_path / 'b')
    a.source.record(ITEM, 'bookmark')
    batch = a.pending(b.replica.actor, SCOPES)
    with monkeypatch.context() as patch:
        module = consumption if failure_location == 'source' else replica_storage
        patch.setattr(module, 'atomic_write_json', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('disk failed')))
        with pytest.raises(OSError):
            b.receive(batch['records'], scopes=SCOPES)
    assert a.pending(b.replica.actor, SCOPES)['pending'] == 1
    assert bool(b.source.list()) is (failure_location == 'replica')
    b = device(tmp_path / 'b', enable=False)
    receipt = b.receive(batch['records'], scopes=SCOPES)
    assert b.source.list()[0]['bookmarked']
    assert a.acknowledge(b.replica.actor, receipt, scopes=SCOPES)['acknowledged'] == 1


def test_late_receipt_flushes_new_local_edit_before_confirming(tmp_path):
    a, b = device(tmp_path / 'a'), device(tmp_path / 'b')
    a.source.record(ITEM, 'bookmark')
    receipt = b.receive(a.pending(b.replica.actor, SCOPES)['records'], scopes=SCOPES)
    a.source.record(ITEM, 'unbookmark')
    assert a.acknowledge(b.replica.actor, receipt, scopes=SCOPES) == {'acknowledged': 0, 'outdated': 1}
    assert a.pending(b.replica.actor, SCOPES)['pending'] == 1
    receipt = b.receive(a.pending(b.replica.actor, SCOPES)['records'], scopes=SCOPES)
    a.acknowledge(b.replica.actor, receipt, scopes=SCOPES)
    assert not b.source.sync_read('bookmarks', ITEM['item_id'])['bookmarked']
    assert b.source.list() == []  # No local reading history was created by a bookmark transfer.
    assert a.pending(b.replica.actor, SCOPES)['pending'] == 0


def test_broken_replica_never_blocks_local_reading_and_can_be_rebuilt(tmp_path):
    a, b = device(tmp_path / 'a'), device(tmp_path / 'b')
    a.source.record(ITEM, 'bookmark')
    a.pending(b.replica.actor, SCOPES)
    valid_replica = a.replica.path.read_bytes()
    a.replica.path.write_bytes(b'corrupt')
    a.source.record(ITEM, 'progress', progress=.6)
    assert a.source.list()[0]['progress'] == .6
    with pytest.raises(SyncError, match='unavailable'):
        a.pending(b.replica.actor, SCOPES)
    assert a.replica.path.read_bytes() == b'corrupt'
    a.replica.path.write_bytes(valid_replica)
    assert a.pending(b.replica.actor, SCOPES)['pending'] == 2


def test_scope_and_identity_mismatch_refused_without_implicit_opt_in(tmp_path):
    a = device(tmp_path / 'a', enable=False)
    with pytest.raises(SyncError, match='not_enabled'):
        a.pending('b' * 64, SCOPES)
    assert not a.source.path.exists()
    a.source.enable_sync('c' * 64, SCOPES)
    before = a.source.path.read_bytes()
    with pytest.raises(SyncError, match='identity_changed'):
        a.receive([], scopes=SCOPES)
    assert a.source.path.read_bytes() == before
    with pytest.raises(SyncError, match='scope_invalid'):
        a.pending('b' * 64, ['conversations'])


def test_concurrent_flush_and_source_actions_have_consistent_lock_order(tmp_path):
    a, b = device(tmp_path / 'a'), device(tmp_path / 'b')

    def operation(index):
        local = device(tmp_path / 'a', enable=False)
        if index % 2:
            local.source.record(ITEM, 'opened')
        else:
            local.pending(b.replica.actor, SCOPES)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(operation, range(16)))
    assert a.source.list()[0]['open_count'] == 8
    batch = a.pending(b.replica.actor, SCOPES)
    assert batch['records'][0]['record']['clock'] == {a.replica.actor: 1}
    receipt = b.receive(batch['records'], scopes=SCOPES)
    assert a.acknowledge(b.replica.actor, receipt, scopes=SCOPES)['acknowledged'] == 1


def test_combined_receipt_failure_preserves_source_outbox_and_rejects_old_ack(tmp_path, monkeypatch):
    a, b = device(tmp_path / 'a'), device(tmp_path / 'b')
    a.source.record(ITEM, 'bookmark')
    first = a.pending(b.replica.actor, ['bookmarks'])
    receipt = b.receive(first['records'], scopes=['bookmarks'])
    a.source.record(ITEM, 'unbookmark')
    source_before, replica_before = a.source.path.read_bytes(), a.replica.path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(replica_storage, 'atomic_write_json', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('full')))
        with pytest.raises(OSError):
            a.acknowledge(b.replica.actor, receipt, scopes=['bookmarks'])
    assert a.source.path.read_bytes() == source_before
    assert a.replica.path.read_bytes() == replica_before
    a = device(tmp_path / 'a', enable=False)
    assert a.acknowledge(b.replica.actor, receipt, scopes=['bookmarks']) == {'acknowledged': 0, 'outdated': 1}
    second = a.pending(b.replica.actor, ['bookmarks'])
    assert second['pending'] == 1 and not second['records'][0]['record']['heads'][0]['value']['bookmarked']
    receipt = b.receive(second['records'], scopes=['bookmarks'])
    assert a.acknowledge(b.replica.actor, receipt, scopes=['bookmarks'])['acknowledged'] == 1


def test_pending_snapshot_failure_returns_no_batch_and_status_retains_scope_conflicts(tmp_path, monkeypatch):
    a, b = device(tmp_path / 'a'), device(tmp_path / 'b')
    a.source.record(ITEM, 'bookmark')
    with monkeypatch.context() as patch:
        patch.setattr(replica_storage, 'atomic_write_json', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('full')))
        with pytest.raises(OSError):
            a.pending(b.replica.actor, SCOPES)
    assert not a.replica.path.exists() and a.source.list()[0]['bookmarked']
    assert a.status(b.replica.actor, ['bookmarks']) == {'pending': 1, 'conflicts': 0}
    a.source.record(ITEM, 'progress', progress=.2)
    b.source.record(ITEM, 'progress', progress=.8)
    a.receive(b.pending(a.replica.actor, ['reading'])['records'], scopes=['reading'])
    assert a.status(b.replica.actor, ['reading']) == {'pending': 1, 'conflicts': 1}
    assert a.status(b.replica.actor, ['bookmarks']) == {'pending': 1, 'conflicts': 0}
