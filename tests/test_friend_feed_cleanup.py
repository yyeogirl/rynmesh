import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_friend_feed import publish, selected, setup

from rynmesh.atomic_io import atomic_write_json
from rynmesh.crypto import canonical_json
from rynmesh.friend_feed import store as store_module
from rynmesh.friend_feed.cleanup import FeedCleanup
from rynmesh.friend_feed.store import CHANNEL, PREVIOUS_VERSION, VERSION, FeedError, FeedStore
from rynmesh.services import peer_box


def erase(feed):
    cleanup = FeedCleanup(feed.store)
    token = cleanup.preview()['review_token']
    return token, cleanup.begin(review_token=token)


def test_clear_stops_publications_drops_drafts_and_preserves_saved_copies_and_friends(tmp_path):
    _, nodes, feeds, contents, reference, _ = setup(tmp_path)
    author, bob, *_ = nodes
    a, b, *_ = feeds
    row = publish(a, reference, selected(author, bob))
    private_copy = b.fetch(bob.store.relationship_for_peer(author.peer_id)['relationship_id'], row['id'], expected_revision=row['revision'])
    saved = contents[1].resolve(private_copy['library_id'])['data']
    draft_id = uuid.uuid4().hex
    a.save_draft(draft_id, reference=reference, audience=selected(author, bob), expected_revision=0, operation_id=uuid.uuid4().hex)
    before_relationship = author.store.relationship_for_peer(bob.peer_id)
    token, result = erase(a)
    assert result['done'] == ['source', 'backups'] and result['local_copies_complete']
    assert not result['remote_confirmed']
    assert a.publications() == [] and b.request(author.peer_id, 'list')['rows'] == []
    with pytest.raises(FeedError, match='publication_unavailable'):
        b.request(author.peer_id, 'fetch', id=row['id'], revision=row['revision'])
    for identifier in (row['id'], draft_id):
        with pytest.raises(FeedError, match='publication_erased'):
            a.save_draft(identifier, reference=reference, audience=selected(author, bob), expected_revision=0, operation_id=uuid.uuid4().hex)
    assert contents[1].resolve(private_copy['library_id'])['data'] == saved
    assert author.store.relationship_for_peer(bob.peer_id) == before_relationship
    assert b'Private feed marker' not in canonical_json(a.store.read())
    fresh = publish(a, reference, selected(author, bob))
    source_bytes = a.store.path.read_bytes()
    assert FeedCleanup(FeedStore(author.home, messaging_key=author.messaging_private)).begin(review_token=token) == result
    assert a.store.path.read_bytes() == source_bytes
    assert a.publications()[0]['id'] == fresh['id']


def test_refresh_in_flight_and_old_subscribe_cannot_restore_erased_timeline(tmp_path):
    _, nodes, feeds, _, reference, _ = setup(tmp_path)
    author, bob, *_ = nodes
    a, b, *_ = feeds
    row = publish(a, reference, selected(author, bob))
    rid = bob.store.relationship_for_peer(author.peer_id)['relationship_id']
    b.subscribe(rid, enabled=True, expected_revision=0)
    b.refresh(rid)
    b.mark_read(rid, row['id'], expected_revision=row['revision'])
    entered, release = threading.Event(), threading.Event()
    original = b.request

    def pause(*args, **kwargs):
        value = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return value

    b.request = pause
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(b.refresh, rid)
        assert entered.wait(3)
        try:
            erase(b)
            assert b.timeline() == [] and b.store.read()['inbox'] == {}
            assert b.subscriptions()[0] == {'relationship_id': rid, 'peer_id': author.peer_id, 'enabled': False, 'revision': 2}
            with pytest.raises(FeedError, match='revision_conflict'):
                b.subscribe(rid, enabled=True, expected_revision=0)
            b.subscribe(rid, enabled=True, expected_revision=2)
        finally:
            release.set()
        with pytest.raises(FeedError, match='refresh_superseded'):
            future.result(5)
    assert b.timeline()[0]['rows'] == []
    b.request = original
    b.refresh(rid)
    assert b.timeline()[0]['rows'][0]['read'] is False


def test_publisher_rechecks_authorization_when_cleanup_happens_during_body_read(tmp_path):
    _, nodes, feeds, contents, reference, _ = setup(tmp_path)
    author, bob, *_ = nodes
    a, b, *_ = feeds
    row = publish(a, reference, selected(author, bob))
    original = contents[0].resolve

    def clear_during_read(identifier):
        value = original(identifier)
        erase(a)
        return value

    contents[0].resolve = clear_during_read
    with pytest.raises(FeedError, match='publication_unavailable'):
        b.request(author.peer_id, 'fetch', id=row['id'], revision=row['revision'])


@pytest.mark.parametrize('after_commit', [False, True])
def test_atomic_failure_or_lost_response_preserves_source_and_new_work(tmp_path, monkeypatch, after_commit):
    _, nodes, feeds, _, reference, _ = setup(tmp_path)
    author, bob, *_ = nodes
    a = feeds[0]
    publish(a, reference, selected(author, bob))
    cleanup = FeedCleanup(a.store)
    token = cleanup.preview()['review_token']
    before = a.store.path.read_bytes()
    original = store_module.atomic_write_json

    def fail(*args, **kwargs):
        if after_commit:
            original(*args, **kwargs)
        raise OSError('synthetic storage failure')

    with monkeypatch.context() as patch:
        patch.setattr(store_module, 'atomic_write_json', fail)
        with pytest.raises(OSError):
            cleanup.begin(review_token=token)
    if after_commit:
        fresh = publish(a, reference, selected(author, bob))
    else:
        assert a.store.path.read_bytes() == before
    assert FeedCleanup(FeedStore(author.home, messaging_key=author.messaging_private)).begin(review_token=token)['local_copies_complete']
    assert [row['id'] for row in a.publications()] == ([fresh['id']] if after_commit else [])


def test_file_failure_restart_changed_backup_review_keeps_new_publications(tmp_path, monkeypatch):
    _, nodes, feeds, _, reference, _ = setup(tmp_path)
    author, bob, *_ = nodes
    a = feeds[0]
    publish(a, reference, selected(author, bob))
    cleanup = FeedCleanup(a.store)
    orphan = a.store.path.with_name('.state.json.' + 'a' * 32 + '.tmp')
    orphan.write_bytes(a.store.path.read_bytes())
    token = cleanup.preview()['review_token']
    original = Path.unlink

    def fail(path, *args, **kwargs):
        if path == orphan:
            raise OSError('synthetic busy file')
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail)
        with pytest.raises(OSError):
            cleanup.begin(review_token=token)
    assert cleanup.status()['done'] == ['source']
    fresh = publish(a, reference, selected(author, bob))
    orphan.write_bytes(b'new reviewed backup bytes')
    untouched = orphan.with_name('.state.json.' + 'b' * 32 + '.tmp')
    untouched.write_bytes(b'new unreviewed bytes')
    cleanup = FeedCleanup(FeedStore(author.home, messaging_key=author.messaging_private))
    with pytest.raises(FeedError, match='backup_changed'):
        cleanup.resume(token)
    approval = cleanup.review_backups(token)
    result = cleanup.approve_backups(token, review_token=approval['review_token'])
    assert cleanup.approve_backups(token, review_token=approval['review_token']) == result
    assert not orphan.exists() and untouched.exists()
    assert a.publications()[0]['id'] == fresh['id']


def test_v1_migration_is_reviewed_and_failure_preserves_exact_backup(tmp_path, monkeypatch):
    _, nodes, feeds, _, reference, _ = setup(tmp_path)
    author, bob, *_ = nodes
    a = feeds[0]
    publish(a, reference, selected(author, bob))
    data = a.store.read()
    data.update(version=PREVIOUS_VERSION, extension={'preserve': True})
    data.pop('erased_publications')
    nonce, ciphertext = peer_box.seal(a.store.key, a.store.pub, canonical_json(data), info=CHANNEL)
    atomic_write_json(a.store.path, {'version': PREVIOUS_VERSION, 'nonce': nonce, 'ciphertext': ciphertext, 'extension': 'keep'})
    before = a.store.path.read_bytes()
    cleanup = FeedCleanup(a.store)
    preview = cleanup.preview()
    assert preview['backup_files'] == 1 and a.store.path.read_bytes() == before
    with monkeypatch.context() as patch:
        patch.setattr(store_module, 'migration_backup', lambda *a, **kw: None)
        with pytest.raises(FeedError, match='backup_failed'):
            cleanup.begin(review_token=preview['review_token'])
    assert a.store.path.read_bytes() == before
    with monkeypatch.context() as patch:
        original = Path.unlink
        patch.setattr(Path, 'unlink', lambda p, *a, **kw: (_ for _ in ()).throw(OSError()) if p.name.endswith('.migrated') else original(p, *a, **kw))
        with pytest.raises(OSError):
            cleanup.begin(review_token=preview['review_token'])
    assert a.store.path.with_name('state.json.v1.migrated').read_bytes() == before
    cleanup.resume(preview['review_token'])
    assert a.store.read()['version'] == VERSION and a.store.read()['extension'] == {'preserve': True}
    assert json.loads(a.store.path.read_bytes())['extension'] == 'keep'


def test_stale_review_and_future_cleanup_format_fail_closed(tmp_path):
    _, nodes, feeds, _, reference, _ = setup(tmp_path)
    author, bob, *_ = nodes
    a = feeds[0]
    cleanup = FeedCleanup(a.store)
    token = cleanup.preview()['review_token']
    publish(a, reference, selected(author, bob))
    before = a.store.path.read_bytes()
    with pytest.raises(FeedError, match='review_changed'):
        cleanup.begin(review_token=token)
    assert a.store.path.read_bytes() == before
    erase(a)
    a.store.mutate(lambda data: data['cleanup'].update(version='ryn.friend-feed-cleanup.future'))
    before = a.store.path.read_bytes()
    for action in (cleanup.preview, a.publications, lambda: cleanup.begin(review_token=token)):
        with pytest.raises(FeedError, match='version_unsupported'):
            action()
        assert a.store.path.read_bytes() == before


def test_maximum_erased_identity_set_fits_and_refuses_to_drop_barriers(tmp_path):
    from rynmesh.friend_feed.store import MAX_ERASED_PUBLICATIONS, MAX_PLAINTEXT

    _, nodes, feeds, _, reference, _ = setup(tmp_path)
    author, bob, *_ = nodes
    a = feeds[0]
    erased = {f'{i:032x}': 2**53 - 1 for i in range(MAX_ERASED_PUBLICATIONS)}
    a.store.mutate(lambda data: data.update(erased_publications=erased))
    assert len(canonical_json(a.store.read())) < MAX_PLAINTEXT
    # A cleanup with no new publications keeps every old identity and advances
    # only its operation sequence, so abandoned old requests never become new.
    old_token, _ = erase(a)
    publish(a, reference, selected(author, bob))
    cleanup = FeedCleanup(a.store)
    new_token = cleanup.preview()['review_token']
    before = a.store.path.read_bytes()
    with pytest.raises(FeedError, match='cleanup_limit'):
        cleanup.begin(review_token=new_token)
    assert a.store.path.read_bytes() == before
    assert a.store.read()['erased_publications'] == erased
    assert cleanup.begin(review_token=old_token)['local_copies_complete']


@pytest.mark.parametrize('legacy', [False, True])
def test_poll_bookkeeping_keeps_review_valid_but_read_choices_do_not(tmp_path, legacy):
    _, nodes, feeds, _, reference, _ = setup(tmp_path)
    author, bob, *_ = nodes
    a, b, *_ = feeds
    row = publish(a, reference, selected(author, bob))
    rid = bob.store.relationship_for_peer(author.peer_id)['relationship_id']
    b.subscribe(rid, enabled=True, expected_revision=0)
    b.refresh(rid)
    if legacy:
        data = b.store.read()
        data['version'] = PREVIOUS_VERSION
        data.pop('erased_publications')
        nonce, ciphertext = peer_box.seal(b.store.key, b.store.pub, canonical_json(data), info=CHANNEL)
        atomic_write_json(b.store.path, {'version': PREVIOUS_VERSION, 'nonce': nonce, 'ciphertext': ciphertext})
    cleanup = FeedCleanup(b.store)
    token = cleanup.preview()['review_token']
    before = b.store.path.read_bytes()
    b.refresh(rid)
    b.refresh(rid)
    assert b.store.path.read_bytes() != before
    assert cleanup.preview()['review_token'] == token
    b.mark_read(rid, row['id'], expected_revision=row['revision'])
    with pytest.raises(FeedError, match='review_changed'):
        cleanup.begin(review_token=token)
    token = cleanup.preview()['review_token']
    b.refresh(rid)
    assert cleanup.begin(review_token=token)['local_copies_complete']
    assert b.timeline() == []
