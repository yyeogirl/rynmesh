"""Real source-store commits, restarts, conflicts and interrupted delivery."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from rynmesh.atomic_io import atomic_write_json, read_json
from rynmesh.device_sync import records
from rynmesh.device_sync.records import SyncError
from rynmesh.services import consumption
from rynmesh.services.consumption import ConsumptionStore

A, B, C = ('a' * 64, 'b' * 64, 'c' * 64)
SCOPES = ['bookmarks', 'reading']
ITEM = {'item_id': 'article', 'title': 'An article', 'link': 'https://example.com/article'}


def store(tmp_path, actor=A, selected=SCOPES, max_items=1000):
    result = ConsumptionStore(tmp_path / actor / 'consumption.json', max_items=max_items)
    result.enable_sync(actor, selected)
    return result


def deliver(source, target, selected=SCOPES):
    return target.sync_receive(source.sync_export(selected), scopes=selected)


def test_opt_in_migration_preserves_backup_extensions_and_seeds_only_selected_data(tmp_path):
    source = ConsumptionStore(tmp_path / 'consumption.json')
    source.record(ITEM, 'bookmark')
    source.record({**ITEM, 'item_id': 'opened'}, 'progress', progress=.4)
    data = read_json(source.path)
    data['article']['extension'] = {'retain': True}
    data['legacy_note'] = 'keep me'
    atomic_write_json(source.path, data)
    before = source.path.read_bytes()
    source.enable_sync(A, ['bookmarks'])
    assert source.path.with_name('consumption.json.migrated').read_bytes() == before
    assert [(row['scope'], row['id']) for row in source.sync_export(['bookmarks'])] == [('bookmarks', 'article')]
    assert read_json(source.path)['legacy_metadata'] == {'legacy_note': 'keep me'}
    assert next(row for row in source.list() if row['item_id'] == 'article')['extension'] == {'retain': True}
    source.enable_sync(A, SCOPES)
    assert source.path.with_name('consumption.json.migrated').read_bytes() == before
    assert [row['id'] for row in source.sync_export(['reading'])] == ['opened']
    with pytest.raises(SyncError, match='identity_changed'):
        source.enable_sync(B, SCOPES)


def test_source_commit_survives_restart_and_history_eviction(tmp_path):
    source = store(tmp_path, max_items=1)
    source.record(ITEM, 'bookmark', now_unix=1)
    source.record(ITEM, 'progress', progress=.72, content_version='v1', now_unix=2)
    source.record({**ITEM, 'item_id': 'new'}, 'opened', now_unix=3)
    assert len(read_json(source.path)['records']) == 1
    restarted = ConsumptionStore(source.path, max_items=1)
    row = next(row for row in restarted.list() if row['item_id'] == 'article')
    assert row['bookmarked'] and row['progress'] == .72 and row['content_version'] == 'v1'
    target = store(tmp_path, B)
    receipts = deliver(restarted, target)
    assert len(receipts) == 3
    assert next(row for row in target.list() if row['item_id'] == 'article')['progress'] == .72


def test_atomic_local_failure_cannot_split_visible_state_from_causal_identity(tmp_path, monkeypatch):
    source = store(tmp_path)
    source.record(ITEM, 'progress', progress=.2)
    before = source.path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(consumption, 'atomic_write_json', lambda *a, **kw: (_ for _ in ()).throw(OSError('disk failed')))
        with pytest.raises(OSError):
            source.record(ITEM, 'progress', progress=.8)
    assert source.path.read_bytes() == before
    source.record(ITEM, 'progress', progress=.8)
    row = source.sync_export(['reading'])[0]['record']
    assert row['clock'] == {A: 2}
    assert source.list()[0]['progress'] == .8


@pytest.mark.parametrize('after_commit', [False, True])
def test_receive_failure_withholds_receipt_and_retry_after_restart_is_idempotent(tmp_path, monkeypatch, after_commit):
    source, target = store(tmp_path), store(tmp_path, B)
    source.record(ITEM, 'progress', progress=.7)
    batch = source.sync_export(SCOPES)
    original = consumption.atomic_write_json

    def fail(*args, **kwargs):
        if after_commit:
            original(*args, **kwargs)
        raise OSError('response lost' if after_commit else 'disk failed')

    with monkeypatch.context() as patch:
        patch.setattr(consumption, 'atomic_write_json', fail)
        with pytest.raises(OSError):
            target.sync_receive(batch, scopes=SCOPES)
    restarted = ConsumptionStore(target.path)
    assert bool(restarted.list()) is after_commit
    receipt = restarted.sync_receive(batch, scopes=SCOPES)
    before = restarted.sync_export(SCOPES)
    assert restarted.sync_receive(batch, scopes=SCOPES) == receipt
    assert restarted.sync_export(SCOPES) == before
    assert restarted.list()[0]['progress'] == .7


def test_concurrent_positions_keep_local_position_until_explicit_review(tmp_path):
    left, right = store(tmp_path), store(tmp_path, B)
    left.record(ITEM, 'progress', progress=.4)
    deliver(left, right)
    left.record(ITEM, 'progress', progress=.8)
    right.record(ITEM, 'progress', progress=.2)
    deliver(left, right)
    deliver(right, left)
    assert left.list()[0]['progress'] == .8
    assert right.list()[0]['progress'] == .2
    conflict = right.sync_read('reading', 'article')
    assert conflict['conflict'] and len(conflict['candidates']) == 2
    right.record(ITEM, 'progress', progress=.3)
    assert right.sync_read('reading', 'article')['conflict']
    with pytest.raises(SyncError, match='revision_conflict'):
        right.record(ITEM, 'progress', progress=.8, expected_sync_revision=conflict['revision'])
    revision = right.sync_read('reading', 'article')['revision']
    right.record(ITEM, 'progress', progress=.8, expected_sync_revision=revision)
    deliver(right, left)
    assert not left.sync_read('reading', 'article')['conflict']
    assert left.list()[0]['progress'] == right.list()[0]['progress'] == .8


def test_third_device_does_not_choose_arbitrary_conflicting_position(tmp_path):
    left, right, third = store(tmp_path), store(tmp_path, B), store(tmp_path, C)
    left.record(ITEM, 'progress', progress=.9)
    right.record(ITEM, 'progress', progress=.2)
    deliver(left, right)
    deliver(right, third)
    assert third.list()[0]['progress'] == 0
    assert third.list()[0]['sync_conflicts']['reading']
    assert {row['value']['progress'] for row in third.sync_read('reading', 'article')['candidates']} == {.2, .9}


def test_concurrent_bookmark_cancel_and_clear_survive_old_delivery(tmp_path):
    left, right = store(tmp_path), store(tmp_path, B)
    left.record(ITEM, 'bookmark')
    deliver(left, right)
    left.record(ITEM, 'bookmark')
    right.record(ITEM, 'unbookmark')
    deliver(left, right)
    assert not right.list()[0]['bookmarked']
    # Unreviewed actions cannot discard the other device's competing choice.
    deliver(right, left)
    left.record(ITEM, 'bookmark')
    assert not left.list()[0]['bookmarked']
    revision = left.sync_read('bookmarks', 'article')['revision']
    left.record(ITEM, 'bookmark', expected_sync_revision=revision)
    assert left.list()[0]['bookmarked']
    left.record(ITEM, 'progress', progress=.6)
    stale = left.sync_export(SCOPES)
    left.clear()
    assert left.list() == []
    left.sync_receive(stale, scopes=SCOPES)
    assert left.list() == []
    deliver(left, right)
    assert not right.list()[0]['bookmarked'] and right.list()[0]['progress'] == 0


def test_unknown_extensions_survive_and_do_not_enter_wire(tmp_path):
    source = store(tmp_path)
    source.record({**ITEM, 'summary': 'private body', 'link': 'https://user:secret@example.com/article'}, 'bookmark')
    assert source.sync_export(['bookmarks'])[0]['record']['heads'][0]['value']['item']['link'] == ITEM['link']
    data = read_json(source.path)
    data['extension'] = {'secret': True}
    data['sync']['extension'] = {'secret': True}
    next(iter(data['sync']['entities'].values()))['extension'] = {'secret': True}
    atomic_write_json(source.path, data)
    source.record(ITEM, 'unbookmark')
    updated = read_json(source.path)
    assert updated['extension'] == updated['sync']['extension'] == {'secret': True}
    assert next(iter(updated['sync']['entities'].values()))['extension'] == {'secret': True}
    wire = source.sync_export(SCOPES)
    assert 'secret' not in str(wire) and 'private body' not in str(wire) and 'summary' not in str(wire)


def test_invalid_batch_scope_or_future_document_preserves_source(tmp_path):
    source, target = store(tmp_path), store(tmp_path, B, ['bookmarks'])
    source.record(ITEM, 'bookmark')
    source.record(ITEM, 'progress', progress=.5)
    before = target.path.read_bytes()
    with pytest.raises(SyncError, match='scope_denied'):
        deliver(source, target)
    assert target.path.read_bytes() == before
    batch = source.sync_export(['bookmarks'])
    broken = deepcopy(batch[0])
    broken['id'] = 'other'
    with pytest.raises(SyncError):
        target.sync_receive(batch + [broken], scopes=['bookmarks'])
    assert target.path.read_bytes() == before
    data = read_json(target.path)
    data['version'] = 'ryn.consumption.v99'
    atomic_write_json(target.path, data)
    future = target.path.read_bytes()
    for operation in (target.clear, lambda: target.record(ITEM, 'bookmark'), lambda: target.enable_sync(B, SCOPES)):
        with pytest.raises(SyncError, match='version_unsupported'):
            operation()
        assert target.path.read_bytes() == future


def test_multiple_source_instances_do_not_lose_operations(tmp_path):
    source = store(tmp_path)
    instances = [ConsumptionStore(source.path) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda instance: instance.record(ITEM, 'opened'), instances))
    assert source.list()[0]['open_count'] == 8
    # Reopening updates local history without inventing eight position edits.
    assert source.sync_export(['reading'])[0]['record']['clock'] == {A: 1}
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda instance: instance.record(ITEM, 'progress', progress=.4), instances))
    assert source.sync_export(['reading'])[0]['record']['clock'] == {A: 9}


def test_opening_synced_conflict_keeps_candidates_and_review_revision(tmp_path):
    left, right = store(tmp_path), store(tmp_path, B)
    left.record(ITEM, 'progress', progress=.8, content_version='text-a')
    right.record(ITEM, 'progress', progress=.2, content_version='text-b')
    deliver(left, right)
    before = right.sync_export(['reading'])
    issue = right.sync_issues()[0]
    right.record(ITEM, 'opened')
    assert right.sync_export(['reading']) == before
    assert right.sync_issues() == [issue]
    assert right.list()[0]['open_count'] == 1


def test_reader_snapshot_cannot_overwrite_new_remote_position(tmp_path):
    left, right = store(tmp_path), store(tmp_path, B)
    left.record(ITEM, 'progress', progress=.6, content_version='same-text')
    deliver(left, right)
    revision = right.list()[0]['sync_revisions']['reading']
    right.record(ITEM, 'opened')
    assert right.list()[0]['sync_revisions']['reading'] == revision
    left.record(ITEM, 'progress', progress=.2, content_version='same-text')
    deliver(left, right)
    before = right.path.read_bytes()
    with pytest.raises(SyncError, match='revision_conflict'):
        right.record(ITEM, 'progress', progress=.7, content_version='same-text', expected_sync_revision=revision)
    assert right.path.read_bytes() == before
    assert right.list()[0]['progress'] == .2


def test_edit_projects_only_target_without_losing_unrelated_remote_state(tmp_path):
    left, right = store(tmp_path, max_items=1), store(tmp_path, B)
    remote = {**ITEM, 'item_id': 'remote'}
    right.record(remote, 'bookmark')
    right.record(remote, 'progress', progress=.7, content_version='original')
    deliver(right, left)
    before = left.sync_export(SCOPES)
    left.record(ITEM, 'opened')
    left.record(ITEM, 'progress', progress=.2)
    left.record(ITEM, 'bookmark')
    restarted = ConsumptionStore(left.path, max_items=1)
    assert [row for row in restarted.sync_export(SCOPES) if row['id'] == 'remote'] == before
    remote_row = next(row for row in restarted.list() if row['item_id'] == 'remote')
    assert remote_row['bookmarked'] and remote_row['progress'] == .7
    assert remote_row['content_version'] == 'original' and remote_row['open_count'] == 0


def test_equal_python_values_do_not_skip_projected_record_validation(tmp_path):
    source = store(tmp_path)
    source.record(ITEM, 'progress', progress=0)
    source.list()  # Prime the persisted-record validation fingerprints.
    data = read_json(source.path)
    entity = next(iter(data['sync']['entities'].values()))
    entity['projected'] = deepcopy(entity['record'])
    entity['projected']['heads'][0]['value']['progress'] = False
    atomic_write_json(source.path, data)
    before = source.path.read_bytes()
    with pytest.raises(SyncError, match='sync_value_invalid'):
        source.record(ITEM, 'opened')
    assert source.path.read_bytes() == before


def test_observed_rereading_may_move_backwards_and_older_snapshot_does_not_restore_it(tmp_path):
    source, target = store(tmp_path), store(tmp_path, B)
    source.record(ITEM, 'progress', progress=.8)
    stale = source.sync_export(SCOPES)
    deliver(source, target)
    target.record(ITEM, 'progress', progress=.1)
    deliver(target, source)
    source.sync_receive(stale, scopes=SCOPES)
    assert source.list()[0]['progress'] == .1
    assert not source.sync_read('reading', 'article')['conflict']


def test_disabled_sync_has_no_causal_store_or_backup(tmp_path):
    source = ConsumptionStore(tmp_path / 'consumption.json')
    source.record(ITEM, 'bookmark')
    assert 'sync' not in read_json(source.path)
    assert not source.path.with_name('consumption.json.migrated').exists()
    with pytest.raises(SyncError, match='not_enabled'):
        source.sync_export(SCOPES)
    assert source.list()[0]['bookmarked']


def test_large_legacy_history_backup_uses_source_budget(tmp_path):
    source = ConsumptionStore(tmp_path / 'consumption.json')
    source.record(ITEM, 'bookmark')
    legacy = read_json(source.path)
    legacy['article']['extension'] = 'x' * (17 * 1024 * 1024)
    atomic_write_json(source.path, legacy, max_bytes=consumption.MAX_HISTORY_BYTES)
    before = source.path.read_bytes()
    source.enable_sync(A, ['bookmarks'])
    assert source.path.with_name('consumption.json.migrated').read_bytes() == before
    assert source.list()[0]['extension'] == legacy['article']['extension']


def test_failed_backup_never_changes_legacy_source(tmp_path, monkeypatch):
    source = ConsumptionStore(tmp_path / 'consumption.json')
    source.record(ITEM, 'bookmark')
    before = source.path.read_bytes()
    monkeypatch.setattr(consumption, 'migration_backup', lambda *args, **kwargs: None)
    with pytest.raises(consumption.ConsumptionError, match='backup_failed'):
        source.enable_sync(A, SCOPES)
    assert source.path.read_bytes() == before


def test_local_http_revision_conflict_and_unavailable_history_are_explicit(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv('RYNMESH_HOME', str(tmp_path / 'node'))
    monkeypatch.setenv('RYNMESH_AUTO_REGISTER', '0')
    monkeypatch.setenv('RYNMESH_ALLOW_REMOTE_CONTROL', '1')
    app = create_app(RynmeshStore())
    source = app.state.consumption_store
    source.enable_sync(A, SCOPES)
    client = TestClient(app)
    saved = client.post('/api/local/consumption', json={'item': ITEM, 'action': 'progress', 'progress': .5, 'content_version': 'v1'})
    assert saved.status_code == 200
    assert saved.json()['content_version'] == 'v1'
    revision = saved.json()['sync_revisions']['reading']
    body = {'item': ITEM, 'action': 'progress', 'progress': .7, 'expected_sync_revision': revision}
    assert client.post('/api/local/consumption', json=body).status_code == 200
    stale = client.post('/api/local/consumption', json=body)
    assert stale.status_code == 409 and stale.json()['detail'] == 'sync_revision_conflict'
    data = read_json(source.path)
    data['version'] = 'ryn.consumption.future'
    atomic_write_json(source.path, data)
    before = source.path.read_bytes()
    assert client.get('/api/local/consumption').status_code == 503
    assert client.post('/api/local/consumption', json=body).status_code == 503
    assert client.delete('/api/local/consumption').status_code == 503
    assert source.path.read_bytes() == before


def test_reading_commit_does_not_block_http_event_loop(tmp_path, monkeypatch):
    import asyncio
    from threading import Event

    import httpx

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv('RYNMESH_HOME', str(tmp_path / 'node'))
    monkeypatch.setenv('RYNMESH_AUTO_REGISTER', '0')
    monkeypatch.setenv('RYNMESH_ALLOW_REMOTE_CONTROL', '1')
    app = create_app(RynmeshStore())
    source = app.state.consumption_store
    source.enable_sync(A, SCOPES)
    started, release = Event(), Event()
    original = source.record

    def delayed_record(*args, **kwargs):
        started.set()
        release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(source, 'record', delayed_record)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            saving = asyncio.create_task(client.post('/api/local/consumption', json={'item': ITEM, 'action': 'bookmark'}))
            try:
                assert await asyncio.to_thread(started.wait, 1)
                assert not saving.done()
                health = await asyncio.wait_for(client.get('/health'), timeout=1)
                assert health.status_code == 200 and not saving.done()
            finally:
                release.set()
                response = await saving
            assert response.status_code == 200
    asyncio.run(exercise())


def test_scope_capture_continues_for_pause_without_enabling_unselected_data(tmp_path):
    source = store(tmp_path, selected=['bookmarks'])
    source.record(ITEM, 'progress', progress=.5)
    source.record(ITEM, 'bookmark')
    assert len(source.sync_export(['bookmarks'])) == 1
    with pytest.raises(SyncError, match='scope_denied'):
        source.sync_export(['reading'])
    source.enable_sync(A, [])
    source.record(ITEM, 'unbookmark')
    assert source.sync_export(['bookmarks'])[0]['record']['heads'][0]['value']['bookmarked'] is False
    with pytest.raises(SyncError, match='scope_denied'):
        source.record(ITEM, 'progress', progress=.7, expected_sync_revision=records.fingerprint(records.empty()))


def legacy_v2_source(source):
    document, local, sync = source._document()
    atomic_write_json(source.path, {**document, 'version': consumption.PREVIOUS_SYNC_VERSION,
                                   'records': local, 'sync': sync.value})
    return source.path.read_bytes()


def test_compact_projection_migration_backs_up_v2_and_preserves_unknown_fields(tmp_path):
    source = store(tmp_path)
    source.record(ITEM, 'bookmark')
    document, local, sync = source._document()
    document['extension'] = {'retained': True}
    sync.value['extension'] = {'retained': True}
    next(iter(sync.value['entities'].values()))['extension'] = {'retained': True}
    source._save_document(document, local, sync)
    original = legacy_v2_source(source)
    source.list()
    assert source.path.read_bytes() == original
    source.record(ITEM, 'unbookmark')
    assert source.path.with_name(source.path.name + '.v2.migrated').read_bytes() == original
    compact = read_json(source.path)
    assert compact['version'] == consumption.SYNC_VERSION
    entity = next(iter(compact['sync']['entities'].values()))
    assert entity['projected'] == 'record'
    assert compact['extension'] == compact['sync']['extension'] == entity['extension'] == {'retained': True}
    restarted = ConsumptionStore(source.path)
    assert not restarted.list()[0]['bookmarked']
    wire = restarted.sync_export(['bookmarks'])
    assert 'projected' not in wire[0] and wire[0]['record']['clock'] == {A: 2}
    restarted.record(ITEM, 'bookmark')
    assert source.path.with_name(source.path.name + '.v2.migrated').read_bytes() == original


@pytest.mark.parametrize('failure', ['backup', 'write'])
def test_compact_migration_failure_retains_original_and_retries_once(tmp_path, monkeypatch, failure):
    source = store(tmp_path)
    source.record(ITEM, 'bookmark')
    original = legacy_v2_source(source)
    with monkeypatch.context() as patch:
        if failure == 'backup':
            patch.setattr(consumption, 'migration_backup', lambda *args, **kwargs: None)
        else:
            patch.setattr(consumption, 'atomic_write_json', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('full')))
        with pytest.raises((consumption.ConsumptionError, OSError)):
            source.record(ITEM, 'unbookmark')
    assert source.path.read_bytes() == original
    source.record(ITEM, 'unbookmark')
    assert source.sync_export(['bookmarks'])[0]['record']['clock'] == {A: 2}
    assert source.path.with_name(source.path.name + '.v2.migrated').read_bytes() == original


def test_compact_marker_is_versioned_and_cannot_skip_invalid_record_checks(tmp_path):
    source = store(tmp_path)
    source.record(ITEM, 'progress', progress=0)
    valid = read_json(source.path)
    assert next(iter(valid['sync']['entities'].values()))['projected'] == 'record'
    source.list()
    for version, projection, invalid_record in ((consumption.PREVIOUS_SYNC_VERSION, 'record', False),
                                               (consumption.SYNC_VERSION, 'unknown-reference', False),
                                               (consumption.SYNC_VERSION, 'record', True)):
        data = deepcopy(valid)
        data['version'] = version
        entity = next(iter(data['sync']['entities'].values()))
        entity['projected'] = projection
        if invalid_record:
            entity['record']['heads'][0]['value']['progress'] = False
        atomic_write_json(source.path, data)
        before = source.path.read_bytes()
        with pytest.raises(SyncError):
            source.list()
        assert source.path.read_bytes() == before


def test_compact_format_retains_distinct_concurrent_projection_after_restart(tmp_path):
    a, b = store(tmp_path, A), store(tmp_path, B)
    a.record(ITEM, 'progress', progress=.2)
    deliver(a, b)
    b.record(ITEM, 'progress', progress=.8)
    a.record(ITEM, 'progress', progress=.4)
    deliver(b, a)
    persisted = read_json(a.path)
    entity = next(iter(persisted['sync']['entities'].values()))
    assert isinstance(entity['projected'], dict)
    restarted = ConsumptionStore(a.path)
    issue = restarted.sync_issues()[0]
    assert {row['value']['progress'] for row in issue['candidates']} == {.4, .8}
    assert restarted.list()[0]['progress'] == .4


def test_approved_scope_enable_and_receive_preserve_original_v2_backup(tmp_path):
    a = store(tmp_path, A, selected=['bookmarks'])
    a.record(ITEM, 'bookmark')
    original = legacy_v2_source(a)
    b = store(tmp_path, B, selected=['reading'])
    b.record(ITEM, 'progress', progress=.7)
    rows = b.sync_export(['reading'])
    with pytest.raises(SyncError, match='identity_changed'):
        a.sync_receive(rows, scopes=['reading'], expected_actor=C, enable=True)
    assert a.path.read_bytes() == original
    a.sync_receive(rows, scopes=['reading'], expected_actor=A, enable=True)
    assert a.path.with_name(a.path.name + '.v2.migrated').read_bytes() == original
    assert a.list()[0]['progress'] == .7 and a.list()[0]['bookmarked']
    assert len(a.sync_export(SCOPES)) == 2
