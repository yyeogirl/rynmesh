"""Reading erasure source transaction: causal replay, consent and failures."""
import json
import os
from copy import deepcopy

import pytest

from rynmesh.atomic_io import AtomicIOError, atomic_write_json, read_json
from rynmesh.device_sync import records
from rynmesh.services import consumption, reading_privacy
from rynmesh.services.consumption import PRIVACY_VERSION, ConsumptionError, ConsumptionStore
from rynmesh.services.reading_privacy import ReadingPrivacy

A, B = 'a' * 64, 'b' * 64
SCOPES = ['bookmarks', 'reading']
ITEM = {'item_id': 'article', 'title': 'Synthetic reading', 'link': 'https://example.com/article'}


def make_store(tmp_path, actor=A, selected=SCOPES):
    source = ConsumptionStore(tmp_path / actor / 'history.json')
    if selected is not None:
        source.enable_sync(actor, selected)
    return source


def erase(source, extra=None):
    privacy = ReadingPrivacy(source, actor=A)
    token = privacy.preview(additional_records=extra)['review_token']
    return token, privacy.erase_source(review_token=token, additional_records=extra)


def test_reviewed_replica_and_projected_values_cannot_replay_after_restart(tmp_path):
    source, peer = make_store(tmp_path), make_store(tmp_path, B)
    source.record(ITEM, 'progress', progress=.2)
    peer.record(ITEM, 'progress', progress=.8)
    peer.record(ITEM, 'bookmark')
    # Replica-only bookmark and concurrent position were not yet in the source.
    extra = peer.sync_export(SCOPES)
    old = source.sync_export(SCOPES)
    token, result = erase(source, extra)
    assert result == {'scope': 'reading_source', 'source_cleared': True, 'identities': 1, 'remote_confirmed': False}
    restarted = ConsumptionStore(source.path)
    for batch in (old, extra):
        restarted.sync_receive(batch, scopes=SCOPES)
    assert restarted.list() == []
    assert restarted.sync_issues() == []
    document = read_json(source.path)
    assert 'Synthetic reading' not in source.path.read_text()
    assert document['version'] == PRIVACY_VERSION
    restarted.record(ITEM, 'bookmark')
    restarted.record(ITEM, 'opened')
    new_bytes = source.path.read_bytes()
    assert ReadingPrivacy(restarted, actor=A).erase_source(review_token=token, additional_records=extra) == result
    assert source.path.read_bytes() == new_bytes
    assert restarted.list()[0]['bookmarked']
    assert all(row['record']['heads'][0]['value'] is not None for row in restarted.sync_export(SCOPES))


def test_cleanup_without_opt_in_preserves_consent_and_later_new_bookmarks(tmp_path):
    source, peer = make_store(tmp_path, selected=None), make_store(tmp_path, B)
    source.record(ITEM, 'bookmark')
    peer.record(ITEM, 'bookmark')
    peer.record(ITEM, 'progress', progress=.8)
    extra = peer.sync_export(SCOPES)
    original = source.path.read_bytes()
    erase(source, extra)
    assert read_json(source.path)['sync'] is None
    assert source.path.with_name('history.json.migrated').read_bytes() == original
    with pytest.raises(records.SyncError, match='sync_not_enabled'):
        source.sync_export(SCOPES)
    source.record(ITEM, 'bookmark')
    source.enable_sync(A, ['bookmarks'])
    assert read_json(source.path)['sync']['scopes'] == ['bookmarks']
    source.sync_receive([row for row in extra if row['scope'] == 'bookmarks'], scopes=['bookmarks'])
    assert source.list()[0]['bookmarked']
    # The inactive reading scope's barrier survives until explicit consent.
    source.enable_sync(A, SCOPES)
    source.sync_receive(extra, scopes=SCOPES)
    assert source.list()[0]['progress'] == 0
    assert source.sync_issues() == []
    with pytest.raises(records.SyncError, match='identity_changed'):
        source.enable_sync(B, SCOPES)


def test_changed_review_and_wrong_replica_retry_preserve_new_data(tmp_path):
    source = make_store(tmp_path)
    privacy = ReadingPrivacy(source, actor=A)
    review = privacy.preview()['review_token']
    source.record(ITEM, 'bookmark')
    before = source.path.read_bytes()
    with pytest.raises(ConsumptionError, match='review_changed'):
        privacy.erase_source(review_token=review)
    assert source.path.read_bytes() == before
    token, _ = erase(source)
    peer = make_store(tmp_path, B)
    peer.record(ITEM, 'bookmark')
    with pytest.raises(ConsumptionError, match='review_changed'):
        privacy.erase_source(review_token=token, additional_records=peer.sync_export(SCOPES))
    assert privacy.preview()['review_token'] != token


@pytest.mark.parametrize('after_commit', [False, True])
def test_atomic_failure_and_lost_response_resume_original_review(tmp_path, monkeypatch, after_commit):
    source = make_store(tmp_path)
    source.record(ITEM, 'bookmark')
    privacy = ReadingPrivacy(source, actor=A)
    token = privacy.preview()['review_token']
    original_bytes = source.path.read_bytes()
    original_write = consumption.atomic_write_json

    def fail(*args, **kwargs):
        if after_commit:
            original_write(*args, **kwargs)
        raise OSError('synthetic disk failure')

    with monkeypatch.context() as patch:
        patch.setattr(consumption, 'atomic_write_json', fail)
        with pytest.raises(OSError):
            privacy.erase_source(review_token=token)
    restarted = ConsumptionStore(source.path)
    if after_commit:
        restarted.record({**ITEM, 'item_id': 'new'}, 'bookmark')
    else:
        assert source.path.read_bytes() == original_bytes
    result = ReadingPrivacy(restarted, actor=A).erase_source(review_token=token)
    assert result['source_cleared']
    assert [row['item_id'] for row in restarted.list()] == (['new'] if after_commit else [])
    assert source.path.with_name('history.json.v3.migrated').read_bytes() == original_bytes


def test_backup_failure_future_receipt_and_unknown_headers(tmp_path, monkeypatch):
    source = make_store(tmp_path)
    source.record(ITEM, 'bookmark')
    data = read_json(source.path)
    data['extension'] = {'keep': True}
    data['sync']['extension'] = 'keep'
    atomic_write_json(source.path, data)
    privacy = ReadingPrivacy(source, actor=A)
    review = privacy.preview()['review_token']
    before = source.path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(reading_privacy, 'migration_backup', lambda *a, **kw: None)
        with pytest.raises(ConsumptionError, match='backup_failed'):
            privacy.erase_source(review_token=review)
    assert source.path.read_bytes() == before
    privacy.erase_source(review_token=review)
    source.record(ITEM, 'bookmark')
    assert read_json(source.path)['extension'] == {'keep': True}
    assert read_json(source.path)['sync']['extension'] == 'keep'
    data = read_json(source.path)
    data['privacy_erasure']['version'] = 'ryn.reading-source-erasure.future'
    atomic_write_json(source.path, data)
    future = source.path.read_bytes()
    for action in (source.list, source.clear, privacy.preview, lambda: source.record(ITEM, 'bookmark')):
        with pytest.raises(ConsumptionError, match='version_unsupported'):
            action()
        assert source.path.read_bytes() == future


def test_unobserved_concurrent_edits_remain_explicit_conflicts(tmp_path):
    source, peer = make_store(tmp_path), make_store(tmp_path, B)
    source.record(ITEM, 'progress', progress=.2)
    peer.sync_receive(source.sync_export(SCOPES), scopes=SCOPES)
    erase(source)
    peer.record(ITEM, 'progress', progress=.9)
    source.sync_receive(peer.sync_export(SCOPES), scopes=SCOPES)
    # Source-only cleanup cannot claim unseen devices were erased.
    assert source.sync_issues()[0]['scope'] == 'reading'
    assert source.list()[0]['progress'] == 0


@pytest.mark.parametrize('damage', ['version', 'actor', 'clock', 'value', 'result'])
def test_changed_receipt_rejected_after_warm_reads_even_with_old_timestamp(tmp_path, damage):
    source = make_store(tmp_path)
    source.record(ITEM, 'bookmark')
    erase(source)
    assert source.list() == []
    source.record(ITEM, 'bookmark')
    assert source.list()[0]['bookmarked']
    stamp = source.path.stat()
    data = read_json(source.path)
    receipt = data['privacy_erasure']
    barrier = next(iter(receipt['barriers'].values()))['record']
    if damage == 'version':
        receipt['version'] = 'ryn.reading-source-erasure.future'
    elif damage == 'actor':
        receipt['actor'] = B
    elif damage == 'clock':
        barrier['clock'][A] = -1
    elif damage == 'value':
        barrier['heads'][0]['value'] = {'bookmarked': False, 'item': ITEM}
    else:
        receipt['result']['source_cleared'] = False
    atomic_write_json(source.path, data)
    os.utime(source.path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    damaged = source.path.read_bytes()
    for owner in (source, ConsumptionStore(source.path)):
        for action in (owner.list, lambda owner=owner: owner.record(ITEM, 'bookmark')):
            with pytest.raises((ConsumptionError, records.SyncError)):
                action()
            assert source.path.read_bytes() == damaged


def test_malformed_replica_rows_and_receipts_are_rejected_without_writes(tmp_path):
    source = make_store(tmp_path)
    source.record(ITEM, 'bookmark')
    row = source.sync_export(SCOPES)[0]
    privacy = ReadingPrivacy(source, actor=A)
    before = source.path.read_bytes()
    for rows in ([row, row], [{**row, 'scope': 'conversations'}], [{**row, 'record': {}}]):
        with pytest.raises((ConsumptionError, records.SyncError)):
            privacy.preview(additional_records=rows)
        assert source.path.read_bytes() == before
    erase(source)
    data = read_json(source.path)
    barrier = next(iter(data['privacy_erasure']['barriers'].values()))
    barrier['record'] = deepcopy(row['record'])
    atomic_write_json(source.path, data)
    with pytest.raises(ConsumptionError, match='receipt_invalid'):
        source.list()


def test_maximum_owned_erasure_fields_fit_source_storage_budget():
    from rynmesh.device_sync.reading import MAX_ENTITIES

    # Both v4 collections retain the same tombstone. Use longest UTF-8 IDs,
    # all actor slots and maximum counters; no full 250 MiB allocation needed.
    clock = {f'{i:064x}': records.MAX_COUNTER for i in range(records.MAX_ACTORS - 1)}
    clock[A] = records.MAX_COUNTER
    record = {'clock': clock, 'heads': [{'actor': A, 'counter': records.MAX_COUNTER, 'value': None}], 'erased': False}
    row = {'scope': 'bookmarks', 'id': '\U0001f600' * 256, 'record': record}
    records.validate('bookmarks', row['id'], record)

    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())

    barrier_entry = size({A: row}) - 2
    sync_entry = size({A: {**row, 'projected': 'record'}}) - 2
    # Include separators and a conservative 1 MiB for owned headers/receipt.
    assert (barrier_entry + sync_entry + 2) * MAX_ENTITIES + 1024 * 1024 < consumption.MAX_SYNC_HISTORY_BYTES


def test_real_size_limit_rejects_before_source_commit_and_can_retry(tmp_path, monkeypatch):
    source = make_store(tmp_path)
    source.record(ITEM, 'bookmark')
    privacy = ReadingPrivacy(source, actor=A)
    token = privacy.preview()['review_token']
    before = source.path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(consumption, 'MAX_SYNC_HISTORY_BYTES', len(before) + 10)
        with pytest.raises(AtomicIOError, match='max_bytes'):
            privacy.erase_source(review_token=token)
    assert source.path.read_bytes() == before
    assert privacy.erase_source(review_token=token)['source_cleared']
