import json
from pathlib import Path

import pytest

from rynmesh.services.library_cleanup import LibraryCleanup, control
from rynmesh.services.library_imports import LibraryImportError, LibraryImportStore


def sample(tmp_path):
    store = LibraryImportStore(tmp_path / 'imports')
    row = store.save(b'Private synthetic article', filename='article.txt', mime='text/plain')
    return store, row, LibraryCleanup(store)


def test_review_change_completed_replay_and_old_generation(tmp_path):
    store, row, cleanup = sample(tmp_path)
    review = cleanup.preview()
    another = store.save(b'Another article', filename='article.txt', mime='text/plain')
    with pytest.raises(LibraryImportError, match='review_changed'):
        cleanup.begin(review_token=review['review_token'])
    review = cleanup.preview(row['import_id'])
    generation = store.generation()
    result = cleanup.begin(review_token=review['review_token'], scope=row['import_id'])
    assert result['local_copies_complete'] and not result['remote_confirmed']
    assert store.body(another['import_id'])['text'] == 'Another article'
    with pytest.raises(LibraryImportError, match='cancelled_by_cleanup'):
        store.save(b'Private synthetic article', filename='article.txt', mime='text/plain', expected_generation=generation)
    recreated = store.save(b'Private synthetic article', filename='article.txt', mime='text/plain')
    assert cleanup.begin(review_token=review['review_token'], scope=row['import_id']) == result
    assert store.body(recreated['import_id'])['text'] == 'Private synthetic article'
    with pytest.raises(LibraryImportError, match='review_changed'):
        cleanup.begin(review_token=review['review_token'])


def test_partial_delete_restart_changed_files_and_new_unselected_work(tmp_path, monkeypatch):
    store, row, cleanup = sample(tmp_path)
    review = cleanup.preview()
    real_unlink = Path.unlink
    def fail(path, *args, **kwargs):
        if path.name == row['blob_name']:
            raise PermissionError('synthetic held file')
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail)
    with pytest.raises(PermissionError):
        cleanup.begin(review_token=review['review_token'])
    restarted = LibraryImportStore(store.root)
    cleanup = LibraryCleanup(restarted)
    assert cleanup.status()['pending'] == ['files']
    assert restarted.list() == []
    with pytest.raises(LibraryImportError, match='cleanup_pending'):
        restarted.read_bytes(row['import_id'])
    with pytest.raises(LibraryImportError, match='cleanup_pending'):
        restarted.save(b'Private synthetic article', filename='article.txt', mime='text/plain', repair=True)
    fresh = restarted.save(b'Later different article', filename='article.txt', mime='text/plain')
    path = restarted._directory(row['import_id']) / row['blob_name']
    path.write_bytes(b'Changed interrupted copy')
    monkeypatch.setattr(Path, 'unlink', real_unlink)
    with pytest.raises(LibraryImportError, match='files_changed'):
        cleanup.resume(review['review_token'])
    approval = cleanup.review_files(review['review_token'])
    done = cleanup.approve_files(review['review_token'], review_token=approval['review_token'])
    assert done['local_copies_complete']
    assert cleanup.approve_files(review['review_token'], review_token=approval['review_token']) == done
    assert restarted.body(fresh['import_id'])['text'] == 'Later different article'


@pytest.mark.parametrize('stage', ['intent', 'finish', 'response'])
def test_write_and_response_failure_recover_original_operation(tmp_path, monkeypatch, stage):
    import rynmesh.services.library_cleanup as module
    store, row, cleanup = sample(tmp_path)
    review = cleanup.preview()
    real = module.atomic_write_json
    def fail(path, value, **kwargs):
        done = value.get('cleanup', {}).get('done')
        if stage == 'intent' and done == ['source'] or stage in {'finish', 'response'} and done == ['source', 'files']:
            if stage == 'response':
                real(path, value, **kwargs)
            raise OSError('synthetic failure')
        return real(path, value, **kwargs)
    monkeypatch.setattr(module, 'atomic_write_json', fail)
    with pytest.raises(OSError):
        cleanup.begin(review_token=review['review_token'])
    if stage == 'intent':
        assert store.body(row['import_id'])['text'] == 'Private synthetic article'
    else:
        assert not store._directory(row['import_id']).exists()
    monkeypatch.setattr(module, 'atomic_write_json', real)
    assert LibraryCleanup(LibraryImportStore(store.root)).begin(review_token=review['review_token'])['local_copies_complete']


def test_legacy_control_migration_extensions_backup_and_future_refusal(tmp_path, monkeypatch):
    import rynmesh.services.library_cleanup as module
    store, row, cleanup = sample(tmp_path)
    path = store.root / 'control.json'
    path.write_text(json.dumps({'version': 1, 'generation': 12, 'extension': {'keep': True}}))
    original = path.read_bytes()
    review = cleanup.preview()
    real = module.migration_backup
    monkeypatch.setattr(module, 'migration_backup', lambda *a, **kw: None)
    with pytest.raises(LibraryImportError, match='backup_failed'):
        cleanup.begin(review_token=review['review_token'])
    assert path.read_bytes() == original and store.get(row['import_id'])
    monkeypatch.setattr(module, 'migration_backup', real)
    cleanup.begin(review_token=review['review_token'])
    assert path.with_name('control.json.v1.migrated').read_bytes() == original
    state = control(store)
    assert state['version'] == 2 and state['extension'] == {'keep': True}
    state['cleanup']['version'] = 'future'
    path.write_text(json.dumps(state))
    original = path.read_bytes()
    with pytest.raises(LibraryImportError, match='version_unsupported'):
        cleanup.preview()
    assert path.read_bytes() == original


def test_unreviewed_names_and_future_extracted_data_preserved(tmp_path):
    store, row, cleanup = sample(tmp_path)
    directory = store._directory(row['import_id'])
    unexpected = directory / 'owner-note.txt'
    unexpected.write_text('not a store-managed basename')
    with pytest.raises(LibraryImportError, match='files_changed'):
        cleanup.preview()
    unexpected.unlink()
    (directory / 'extracted.json').write_text(json.dumps({'version': 999, 'text': 'future'}))
    with pytest.raises(LibraryImportError, match='version_unsupported'):
        cleanup.preview()
    assert store._blob(row).read_bytes() == b'Private synthetic article'


def test_completed_history_is_compact_and_prior_job_cannot_erase_new_work(tmp_path):
    store, row, cleanup = sample(tmp_path)
    first = cleanup.preview()
    cleanup.begin(review_token=first['review_token'])
    assert control(store)['cleanup']['files'] == control(store)['cleanup']['targets'] == []
    second = cleanup.preview()
    cleanup.begin(review_token=second['review_token'])
    store.save(b'New after cleanup', filename='article.txt', mime='text/plain')
    with pytest.raises(LibraryImportError, match='review_changed'):
        cleanup.begin(review_token=first['review_token'])
    assert len(store.list()) == 1


def test_counter_limit_preserves_copy_and_legacy_control(tmp_path):
    from rynmesh.device_sync.records import MAX_COUNTER
    store, row, cleanup = sample(tmp_path)
    path = store.root / 'control.json'
    path.write_text(json.dumps({'version': 1, 'generation': MAX_COUNTER}))
    original = path.read_bytes()
    with pytest.raises(LibraryImportError, match='cleanup_limit'):
        cleanup.preview()
    assert path.read_bytes() == original and store.body(row['import_id'])
