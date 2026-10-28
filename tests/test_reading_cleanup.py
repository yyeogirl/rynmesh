"""Actual source, encrypted replica/index, and recoverable reviewed file cleanup."""
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_local_search import document

from rynmesh.atomic_io import atomic_write_json, read_json
from rynmesh.device_sync.store import ReplicaStore
from rynmesh.local_search.index import LocalSearchIndex
from rynmesh.services.consumption import ConsumptionError, ConsumptionStore
from rynmesh.services.reading_cleanup import STEPS, ReadingCleanup

ITEM = {'item_id': 'article', 'title': 'SYNTHETIC-PRIVATE-READING-MARKER', 'link': 'https://example.com/article'}
SCOPES = ['bookmarks', 'reading']


def fixture(tmp_path, *, enabled=True, original=None):
    if original:
        source, replica = ConsumptionStore(original.source.path), original.replica
    else:
        source = ConsumptionStore(tmp_path / 'reading' / 'history.json')
        replica = ReplicaStore(tmp_path, messaging_key=X25519PrivateKey.generate())
        source.record(ITEM, 'bookmark')
        source.record(ITEM, 'progress', progress=.7)
        if enabled:
            source.enable_sync(replica.actor, SCOPES)
            replica.reconcile_source(source.sync_export(SCOPES), scopes=SCOPES)
    search = LocalSearchIndex(tmp_path / 'local-search', messaging_key=replica.key,
        source=lambda: [document(row['item_id'], text=row['item']['title'], kinds=['history']) for row in source.list()])
    search.rebuild(force=True)
    return ReadingCleanup(tmp_path, source=source, replica=replica, search=search,
                          pairing_lock=tmp_path / 'device-sync' / '.pairings.lock')


@pytest.mark.parametrize('enabled', [False, True])
def test_clear_reviewed_sources_replica_backups_index_and_keep_new_work(tmp_path, enabled):
    cleanup = fixture(tmp_path, enabled=enabled)
    orphan = cleanup.source.path.with_name('.history.json.' + 'a' * 32 + '.tmp')
    orphan.write_bytes(cleanup.source.path.read_bytes())
    unrelated = cleanup.source.path.with_name('user-backup.json')
    unrelated.write_bytes(b'private user backup outside this review')
    preview = cleanup.preview()
    assert preview['backup_files'] == (3 if enabled else 2)
    result = cleanup.begin(review_token=preview['review_token'])
    assert result['done'] == list(STEPS) and result['local_copies_complete']
    assert not result['remote_confirmed']
    assert cleanup.source.list() == []
    assert not cleanup.replica.read('bookmarks', ITEM['item_id'])['bookmarked']
    assert cleanup.search._read()[1]['documents'] == []
    assert not list(cleanup.source.path.parent.glob('*.migrated'))
    assert not orphan.exists() and unrelated.exists()
    assert ITEM['title'] not in cleanup.path.read_text()
    cleanup.source.record(ITEM, 'bookmark')
    restarted = fixture(tmp_path, original=cleanup)
    assert restarted.begin(review_token=preview['review_token']) == result
    assert restarted.source.list()[0]['bookmarked']


@pytest.mark.parametrize('change', ['source', 'replica', 'backup'])
def test_stale_review_does_not_create_journal_or_clear_anything(tmp_path, change):
    cleanup = fixture(tmp_path)
    token = cleanup.preview()['review_token']
    if change == 'source':
        cleanup.source.record({**ITEM, 'item_id': 'new'}, 'bookmark')
    elif change == 'replica':
        cleanup.replica._mutate(lambda data: data.update(extension='new'))
    else:
        cleanup.source.path.with_name('history.json.migrated').write_bytes(b'changed')
    before = cleanup.source.path.read_bytes(), cleanup.replica.path.read_bytes()
    with pytest.raises(ConsumptionError, match='review_changed'):
        cleanup.begin(review_token=token)
    assert before == (cleanup.source.path.read_bytes(), cleanup.replica.path.read_bytes())
    assert not cleanup.path.exists()


def test_source_commit_lost_journal_write_retry_preserves_new_source_and_replica(tmp_path, monkeypatch):
    cleanup = fixture(tmp_path)
    token = cleanup.preview()['review_token']
    save, writes = cleanup._save, []

    def fail_second(*args):
        writes.append(1)
        if len(writes) == 2:
            raise OSError('synthetic journal unavailable')
        save(*args)

    with monkeypatch.context() as patch:
        patch.setattr(cleanup, '_save', fail_second)
        with pytest.raises(OSError):
            cleanup.begin(review_token=token)
    assert cleanup.status()['done'] == []
    cleanup.source.record(ITEM, 'bookmark')
    cleanup.replica.reconcile_source(cleanup.source.sync_export(SCOPES), scopes=SCOPES)
    restarted = fixture(tmp_path, original=cleanup)
    with pytest.raises(ConsumptionError, match='already_started'):
        restarted.cancel_uncommitted(token)
    assert restarted.resume(token)['local_copies_complete']
    assert restarted.source.list()[0]['bookmarked']
    assert restarted.replica.read('bookmarks', ITEM['item_id'])['bookmarked']


def test_file_failure_restart_and_changed_backup_require_another_review(tmp_path, monkeypatch):
    cleanup = fixture(tmp_path)
    token = cleanup.preview()['review_token']
    original_unlink = Path.unlink
    backup = cleanup.source.path.with_name('history.json.v3.migrated')

    def fail(path, *args, **kwargs):
        if path == backup:
            raise OSError('synthetic file busy')
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail)
        with pytest.raises(OSError):
            cleanup.begin(review_token=token)
    assert cleanup.status()['done'] == ['source', 'replica']
    cleanup.source.record({**ITEM, 'item_id': 'new'}, 'bookmark')
    backup.write_bytes(b'changed reviewed backup')
    untouched = cleanup.source.path.with_name('.history.json.' + 'b' * 32 + '.tmp')
    untouched.write_bytes(b'new unreviewed file')
    restarted = fixture(tmp_path, original=cleanup)
    with pytest.raises(ConsumptionError, match='backup_changed'):
        restarted.resume(token)
    with pytest.raises(ConsumptionError, match='backup_changed'):
        restarted.approve_backups(token, review_token=None)
    approval = restarted.review_backups(token)
    result = restarted.approve_backups(token, review_token=approval['review_token'])
    assert result['local_copies_complete']
    assert restarted.approve_backups(token, review_token=approval['review_token']) == result
    assert not backup.exists() and untouched.exists()
    assert restarted.source.list()[0]['item_id'] == 'new'


def test_uncommitted_review_can_be_cancelled_and_old_review_never_reused(tmp_path, monkeypatch):
    cleanup = fixture(tmp_path)
    token = cleanup.preview()['review_token']
    with monkeypatch.context() as patch:
        patch.setattr(cleanup, '_step_source', lambda job: (_ for _ in ()).throw(OSError('synthetic source busy')))
        with pytest.raises(OSError):
            cleanup.begin(review_token=token)
    cleanup.source.record({**ITEM, 'item_id': 'new'}, 'bookmark')
    with pytest.raises(ConsumptionError, match='review_changed'):
        cleanup.resume(token)
    assert cleanup.cancel_uncommitted(token)['cancelled']
    assert cleanup.begin(review_token=token)['cancelled']
    next_token = cleanup.preview()['review_token']
    assert next_token != token
    cleanup.begin(review_token=next_token)
    with pytest.raises(ConsumptionError, match='review_changed'):
        cleanup.begin(review_token=token)


def test_future_journal_preserves_sources(tmp_path):
    cleanup = fixture(tmp_path)
    before = cleanup.source.path.read_bytes()
    atomic_write_json(cleanup.path, {'version': 'ryn.reading-cleanup.future'})
    with pytest.raises(ConsumptionError, match='version_unsupported'):
        cleanup.preview()
    assert cleanup.source.path.read_bytes() == before
    assert read_json(cleanup.path)['version'] == 'ryn.reading-cleanup.future'
