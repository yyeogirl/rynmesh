"""Actual encrypted stores and durable retries for local conversation copies."""
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_ask_history import sample
from test_local_search import document

from rynmesh.ask_ryn.cleanup import (
    CHANNEL,
    MAX_HISTORY,
    PREVIOUS_VERSION,
    STEPS,
    VERSION,
    ConversationCleanup,
)
from rynmesh.ask_ryn.privacy import ConversationPrivacy
from rynmesh.ask_ryn.store import ConversationError, ConversationStore
from rynmesh.atomic_io import atomic_write_json
from rynmesh.crypto import canonical_json
from rynmesh.device_sync import records
from rynmesh.device_sync.store import ReplicaStore
from rynmesh.local_search.index import LocalSearchIndex, SearchError
from rynmesh.services import peer_box


def fixture(tmp_path, *, enabled=True, source=None, replica=None, orders=None):
    source = source or ConversationStore(tmp_path / 'ask-ryn', X25519PrivateKey.generate())
    if not source.path.exists():
        source.save(sample(), expected_revision=0)
    replica = replica or ReplicaStore(tmp_path, messaging_key=source.key)
    if enabled:
        source.enable_sync()
        replica.reconcile_source(source.sync_export(), scopes=['conversations'])
    def rows():
        return [document(row['id'], text=' '.join(message['content'] for message in row['messages']), kinds=['chat'])
                for row in source.list()]
    search = LocalSearchIndex(tmp_path / 'local-search', messaging_key=source.key, source=rows)
    search.rebuild(force=True)
    order_calls = [] if orders is None else orders
    job = ConversationCleanup(tmp_path, source=source, replica=replica,
        pairing_lock=tmp_path / 'device-sync' / '.pairings.lock', search=search,
        erase_order_results=lambda ids: order_calls.append(ids))
    return job, order_calls


def test_reviewed_copies_clear_and_restart_retry_keeps_new_work(tmp_path):
    job, calls = fixture(tmp_path)
    backup = job.source.path.with_name('history.json.migrated')
    assert backup.exists()
    orphan = job.source.path.with_name('.history.json.' + 'a' * 32 + '.tmp')
    orphan.write_bytes(job.source.path.read_bytes())
    unrelated = job.source.path.with_name('user-backup.json')
    unrelated.write_bytes(b'leave this file')
    preview = job.preview()
    assert preview['backup_files'] == 2 and preview['identities'] == 1
    result = job.begin(review_token=preview['review_token'])
    assert result['done'] == list(STEPS) and result['local_copies_complete']
    assert result['browser_cleanup_required'] and not result['remote_confirmed']
    assert not backup.exists() and not orphan.exists() and unrelated.read_bytes() == b'leave this file'
    assert job.source.list() == []
    assert job.replica.read('conversations', sample()['id'])['erased']
    assert job.search._read()[1]['documents'] == []
    assert sample()['messages'][0]['content'] not in job.path.read_text()
    assert calls == [[]]
    new = {**sample(), 'id': 'new-chat'}
    job.source.save(new, expected_revision=0)
    restarted, _ = fixture(tmp_path, source=job.source, replica=job.replica, orders=calls)
    assert restarted.begin(review_token=preview['review_token']) == result
    assert restarted.source.get('new-chat')['messages'] == new['messages']
    assert calls == [[]]


def test_replica_only_identity_is_erased_without_opt_in_and_later_opt_in_keeps_barrier(tmp_path):
    job, _ = fixture(tmp_path, enabled=False)
    other = {**sample(), 'id': 'replica-only'}
    record = records.write('conversations', other['id'], records.empty(), 'b' * 64, other)
    stale = [{'scope': 'conversations', 'id': other['id'], 'record': record}]
    job.replica.receive(stale, scopes=['conversations'])
    preview = job.preview()
    assert preview['identities'] == 2
    job.begin(review_token=preview['review_token'])
    assert job.source._read()[1]['version'] == 'ryn.ask-history.v1'
    assert job.replica.read('conversations', other['id'])['erased']
    job.source.enable_sync()
    job.source.sync_receive(stale)
    assert job.source.list() == job.source.sync_conflicts() == []
    assert all(row['record']['erased'] for row in job.source.sync_export())


@pytest.mark.parametrize('change', ['source', 'backup', 'replica'])
def test_stale_review_mutates_no_source_or_journal(tmp_path, change):
    job, _ = fixture(tmp_path)
    preview = job.preview()
    if change == 'source':
        job.source.save_draft('new draft', expected_revision=0)
    elif change == 'backup':
        job.source.path.with_name('history.json.migrated').write_bytes(b'changed backup')
    else:
        row = {**sample(), 'id': 'new-replica'}
        job.replica.receive([{'scope': 'conversations', 'id': row['id'],
            'record': records.write('conversations', row['id'], records.empty(), 'b' * 64, row)}], scopes=['conversations'])
    source_bytes = job.source.path.read_bytes()
    replica_bytes = job.replica.path.read_bytes()
    with pytest.raises(ConversationError, match='ask_privacy_review_changed'):
        job.begin(review_token=preview['review_token'])
    assert job.source.path.read_bytes() == source_bytes and job.replica.path.read_bytes() == replica_bytes
    assert not job.path.exists()


def test_source_commit_then_lost_journal_write_resumes_without_erasing_new_work(tmp_path, monkeypatch):
    job, _ = fixture(tmp_path)
    preview = job.preview()
    save, writes = job._save, []
    def fail_second(envelope, data):
        writes.append(1)
        if len(writes) == 2:
            raise OSError('disk unavailable')
        save(envelope, data)
    monkeypatch.setattr(job, '_save', fail_second)
    with pytest.raises(OSError):
        job.begin(review_token=preview['review_token'])
    assert job.status(preview['review_token'])['done'] == []
    job.source.save({**sample(), 'id': 'created-after-failure'}, expected_revision=0)
    restarted, _ = fixture(tmp_path, source=job.source, replica=job.replica)
    assert restarted.resume(preview['review_token'])['local_copies_complete']
    assert restarted.source.get('created-after-failure')
    assert [row['id'] for row in restarted.search._read()[1]['documents']] == ['created-after-failure']


def test_partial_backup_failure_retries_only_reviewed_files(tmp_path, monkeypatch):
    job, _ = fixture(tmp_path)
    orphan = job.source.path.with_name('.history.json.' + 'c' * 32 + '.tmp')
    orphan.write_bytes(job.source.path.read_bytes())
    preview = job.preview()
    unlink = Path.unlink
    def fail_backup(path, *args, **kwargs):
        if path.name == 'history.json.migrated':
            raise OSError('backup locked')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail_backup)
    with pytest.raises(OSError):
        job.begin(review_token=preview['review_token'])
    assert job.status(preview['review_token'])['done'] == ['source', 'replica']
    assert not orphan.exists()
    # New orphan copies were not reviewed and must not be swept up on retry.
    later = job.source.path.with_name('.history.json.' + 'd' * 32 + '.tmp')
    later.write_bytes(b'new content')
    monkeypatch.setattr(Path, 'unlink', unlink)
    assert job.resume(preview['review_token'])['local_copies_complete']
    assert later.read_bytes() == b'new content'


def test_busy_index_leaves_cleanup_pending_and_retries_after_writer_releases(tmp_path, monkeypatch):
    job, calls = fixture(tmp_path)
    preview = job.preview()
    save = job._save
    def busy_after_backup(envelope, data):
        save(envelope, data)
        if data['jobs'][preview['review_token']]['done'] == ['source', 'replica', 'backups']:
            job.search.writer.acquire()
    monkeypatch.setattr(job, '_save', busy_after_backup)
    try:
        with pytest.raises(SearchError, match='search_index_busy'):
            job.begin(review_token=preview['review_token'])
    finally:
        job.search.writer.release()
    monkeypatch.setattr(job, '_save', save)
    assert job.status(preview['review_token'])['pending'] == ['search', 'orders']
    assert calls == []
    assert job.resume(preview['review_token'])['local_copies_complete']


def test_changed_remaining_backup_is_not_deleted_and_not_reported_complete(tmp_path, monkeypatch):
    job, _ = fixture(tmp_path)
    preview = job.preview()
    def fail(_):
        raise OSError('interrupted before backup cleanup')
    original = job._step_backups
    monkeypatch.setattr(job, '_step_backups', fail)
    with pytest.raises(OSError):
        job.begin(review_token=preview['review_token'])
    backup = job.source.path.with_name('history.json.migrated')
    backup.write_bytes(b'unreviewed backup')
    monkeypatch.setattr(job, '_step_backups', original)
    with pytest.raises(ConversationError, match='ask_cleanup_backup_changed'):
        job.resume(preview['review_token'])
    assert backup.read_bytes() == b'unreviewed backup'
    assert not job.status(preview['review_token'])['local_copies_complete']


def test_source_review_binds_additional_identities(tmp_path):
    job, _ = fixture(tmp_path, enabled=False)
    privacy = ConversationPrivacy(job.source)
    preview = privacy.preview(additional_identifiers=['old-replica'])
    before = job.source.path.read_bytes()
    with pytest.raises(ConversationError, match='ask_privacy_review_changed'):
        privacy.erase_source(review_token=preview['review_token'], additional_identifiers=['different'])
    assert job.source.path.read_bytes() == before


def test_changed_backup_needs_new_review_and_keeps_other_new_work(tmp_path, monkeypatch):
    job, _ = fixture(tmp_path)
    token = job.preview()['review_token']
    original = job._step_backups
    def fail(_):
        raise OSError('locked')
    monkeypatch.setattr(job, '_step_backups', fail)
    with pytest.raises(OSError):
        job.begin(review_token=token)
    monkeypatch.setattr(job, '_step_backups', original)
    backup = job.source.path.with_name('history.json.migrated')
    backup.write_bytes(b'replacement version')
    reviewed = job.review_backups(token)
    backup.write_bytes(b'another replacement')
    with pytest.raises(ConversationError, match='ask_cleanup_backup_changed'):
        job.approve_backups(token, review_token=reviewed['review_token'])
    reviewed = job.review_backups(token)
    job.source.save({**sample(), 'id': 'new-work'}, expected_revision=0)
    assert job.approve_backups(token, review_token=reviewed['review_token'])['local_copies_complete']
    assert not backup.exists() and job.source.get('new-work')


def test_abandon_uncommitted_failure_allows_fresh_review_but_old_request_stays_cancelled(tmp_path, monkeypatch):
    job, _ = fixture(tmp_path)
    token = job.preview()['review_token']
    original = job._step_source
    def fail(_):
        raise OSError('source unavailable')
    monkeypatch.setattr(job, '_step_source', fail)
    with pytest.raises(OSError):
        job.begin(review_token=token)
    assert job.cancel_uncommitted(token)['cancelled']
    assert job.begin(review_token=token)['cancelled']
    assert job.source.get(sample()['id'])
    fresh = job.preview()['review_token']
    assert fresh != token
    monkeypatch.setattr(job, '_step_source', original)
    assert job.begin(review_token=fresh)['local_copies_complete']
    assert job.cancel_uncommitted(token)['cancelled']
    with pytest.raises(ConversationError, match='ask_cleanup_already_started'):
        job.cancel_uncommitted(fresh)


def test_reviewed_search_orphan_is_removed_but_canonical_index_is_rebuilt(tmp_path):
    job, _ = fixture(tmp_path)
    old = job.search.path.read_bytes()
    orphan = job.search.path.with_name('.index.json.' + 'f' * 32 + '.tmp')
    orphan.write_bytes(old)
    token = job.preview()['review_token']
    assert job.begin(review_token=token)['local_copies_complete']
    assert not orphan.exists()
    assert job.search.path.exists() and job.search.path.read_bytes() != old
    assert job.search._read()[1]['documents'] == []


def write_journal(job, data, **extensions):
    nonce, ciphertext = peer_box.seal(job.source.key, job.source.pub, canonical_json(data), info=CHANNEL)
    atomic_write_json(job.path, {**extensions, 'version': data['version'], 'nonce': nonce, 'ciphertext': ciphertext})


def legacy_journal(job, *, done=None):
    plan = {**job._plan(), 'review_generation': 0, 'future_plan_field': {'keep': True}}
    token = records.fingerprint(plan)
    entry = {'id': token, 'review_token': token, 'plan': plan, 'done': list(STEPS) if done is None else done,
             'future_job_field': ['keep']}
    data = {'version': PREVIOUS_VERSION, 'jobs': {token: entry}, 'future_data_field': 42}
    write_journal(job, data, future_envelope_field='keep')
    return token


def test_more_than_32_cleanups_keep_monotonic_identity_and_replay_barriers(tmp_path, monkeypatch):
    job, calls = fixture(tmp_path)
    tokens = []
    original = job._step_source
    def fail(_):
        raise OSError('source unavailable')
    for number in range(MAX_HISTORY + 3):
        token = job.preview()['review_token']
        tokens.append(token)
        if number == 0:
            monkeypatch.setattr(job, '_step_source', fail)
            with pytest.raises(OSError):
                job.begin(review_token=token)
            assert job.cancel_uncommitted(token)['cancelled']
            monkeypatch.setattr(job, '_step_source', original)
        else:
            result = job.begin(review_token=token)
            assert result['local_copies_complete'] and result['sequence'] == number + 1
    assert len(set(tokens)) == MAX_HISTORY + 3
    assert [row['sequence'] for row in job.list_status()] == list(range(MAX_HISTORY + 3, 3, -1))
    _, journal = job._read()
    assert journal['next_generation'] == MAX_HISTORY + 3
    assert all(row['compacted'] and row['plan'] == {'review_generation': row['plan']['review_generation'],
                                                  'identity_count': 1} for row in journal['jobs'].values())
    job.source.save({**sample(), 'id': 'after-history-rollover'}, expected_revision=0)
    restarted, _ = fixture(tmp_path, source=job.source, replica=job.replica, orders=calls)
    before = restarted.source.path.read_bytes()
    # Both an evicted cancellation and an evicted successful operation must fail
    # closed, rather than becoming valid reviews of the new source.
    for old in tokens[:3]:
        with pytest.raises(ConversationError, match='ask_privacy_review_changed'):
            restarted.begin(review_token=old)
        with pytest.raises(ConversationError, match='ask_cleanup_not_found'):
            restarted.resume(old)
    assert restarted.source.path.read_bytes() == before
    assert restarted.replica.read('conversations', sample()['id'])['erased']
    assert restarted.begin(review_token=tokens[-1])['local_copies_complete']
    assert restarted.source.get('after-history-rollover')


def test_full_history_never_evicts_unfinished_operation(tmp_path, monkeypatch):
    job, _ = fixture(tmp_path)
    # Seed only completed summaries; the last operation below uses the real
    # source/replica commit and deliberately fails before removing a backup.
    entries = {}
    for generation in range(MAX_HISTORY - 1):
        token = f'{generation:064x}'
        entries[token] = {'id': token, 'review_token': token, 'done': list(STEPS), 'compacted': True,
                          'plan': {'review_generation': generation, 'identity_count': 0}}
    write_journal(job, {'version': VERSION, 'jobs': entries, 'next_generation': MAX_HISTORY - 1})
    token = job.preview()['review_token']
    original = job._step_backups
    def fail(_):
        raise OSError('backup locked')
    monkeypatch.setattr(job, '_step_backups', fail)
    with pytest.raises(OSError):
        job.begin(review_token=token)
    before = job.path.read_bytes()
    with pytest.raises(ConversationError, match='ask_cleanup_pending'):
        job.begin(review_token=job.preview()['review_token'])
    assert job.path.read_bytes() == before
    assert job.status(token)['sequence'] == MAX_HISTORY
    assert 'backups' in job._read()[1]['jobs'][token]['plan']
    monkeypatch.setattr(job, '_step_backups', original)
    assert job.resume(token)['local_copies_complete']
    assert job.begin(review_token=job.preview()['review_token'])['sequence'] == MAX_HISTORY + 1
    assert len(job.list_status()) == MAX_HISTORY


def test_legacy_read_only_then_migration_preserves_original_and_unknown_fields(tmp_path):
    job, _ = fixture(tmp_path)
    old_token = legacy_journal(job)
    before = job.path.read_bytes()
    backup = job.path.with_name(job.path.name + '.v1.migrated')
    assert job.list_status()[0]['sequence'] == 1
    preview = job.preview()
    assert job.path.read_bytes() == before and not backup.exists()
    assert job.begin(review_token=preview['review_token'])['sequence'] == 2
    assert backup.read_bytes() == before
    envelope, data = job._read()
    assert envelope['version'] == data['version'] == VERSION
    assert envelope['future_envelope_field'] == 'keep' and data['future_data_field'] == 42
    assert data['jobs'][old_token]['future_job_field'] == ['keep']
    assert data['jobs'][old_token]['plan']['future_plan_field'] == {'keep': True}
    assert data['jobs'][old_token]['compacted']
    job.begin(review_token=job.preview()['review_token'])
    assert backup.read_bytes() == before


def test_unfinished_legacy_operation_resumes_and_compacts_only_after_completion(tmp_path, monkeypatch):
    job, _ = fixture(tmp_path)
    token = legacy_journal(job, done=[])
    before = job.path.read_bytes()
    original = job._step_backups
    def fail(_):
        raise OSError('backup locked')
    monkeypatch.setattr(job, '_step_backups', fail)
    with pytest.raises(OSError):
        job.resume(token)
    assert job.path.with_name(job.path.name + '.v1.migrated').read_bytes() == before
    entry = job._read()[1]['jobs'][token]
    assert entry['done'] == ['source', 'replica'] and not entry.get('compacted')
    assert entry['plan']['identifiers'] == [sample()['id']]
    monkeypatch.setattr(job, '_step_backups', original)
    assert job.resume(token)['local_copies_complete']
    assert job._read()[1]['jobs'][token]['compacted']


@pytest.mark.parametrize('failure', ['backup', 'commit'])
def test_legacy_migration_failure_keeps_source_and_journal_retryable(tmp_path, monkeypatch, failure):
    import rynmesh.ask_ryn.cleanup as module
    job, _ = fixture(tmp_path)
    legacy_journal(job)
    token = job.preview()['review_token']
    before, source_before = job.path.read_bytes(), job.source.path.read_bytes()
    with monkeypatch.context() as patch:
        if failure == 'backup':
            patch.setattr(module, 'migration_backup', lambda *args, **kwargs: None)
            error, match = ConversationError, 'ask_cleanup_backup_failed'
        else:
            def fail(*args, **kwargs):
                raise OSError('journal unavailable')
            patch.setattr(module, 'atomic_write_json', fail)
            error, match = OSError, 'journal unavailable'
        with pytest.raises(error, match=match):
            job.begin(review_token=token)
    assert job.path.read_bytes() == before and job.source.path.read_bytes() == source_before
    assert job.begin(review_token=token)['local_copies_complete']
    assert job.path.with_name(job.path.name + '.v1.migrated').read_bytes() == before


@pytest.mark.parametrize('counter', [None, True, -1, 0, records.MAX_COUNTER + 1])
def test_invalid_generation_fails_closed(tmp_path, counter):
    job, _ = fixture(tmp_path)
    token = job.preview()['review_token']
    job.begin(review_token=token)
    _, data = job._read()
    data['next_generation'] = counter
    write_journal(job, data)
    before = job.path.read_bytes()
    with pytest.raises(ConversationError, match='ask_cleanup_unreadable'):
        job.preview()
    assert job.path.read_bytes() == before


def test_future_journal_is_not_overwritten(tmp_path):
    job, _ = fixture(tmp_path)
    write_journal(job, {'version': 'ryn.conversation-cleanup.v99', 'jobs': {}})
    before = job.path.read_bytes()
    with pytest.raises(ConversationError, match='ask_cleanup_version_unsupported'):
        job.preview()
    assert job.path.read_bytes() == before
