import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_friends import Mesh, _node

from rynmesh.atomic_io import atomic_write_json, read_json
from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.crypto import canonical_json
from rynmesh.friend_feed.routes import install_friend_feed
from rynmesh.friend_feed.service import PATH, FriendFeed
from rynmesh.friend_feed.store import FeedError, FeedStore
from rynmesh.friends.content import FriendContent
from rynmesh.services.consumption import ConsumptionStore
from rynmesh.services.library_imports import LibraryImportStore
from rynmesh.services.reader import ReaderCache
from rynmesh.store import RynmeshStore


def setup(tmp_path):
    mesh = Mesh()
    nodes, feeds, contents = [], [], []
    original_post = mesh.post
    wires = []
    def post(endpoint, path, payload, headers=None, **kwargs):
        if path != PATH:
            return original_post(endpoint, path, payload, headers, **kwargs)
        if endpoint not in mesh.online:
            raise OSError('offline with private diagnostic detail')
        node = mesh.nodes[endpoint]
        relationship = node.verify_request(path=path, body=canonical_json(payload), headers=headers)
        wire = feeds[nodes.index(node)].respond(payload, relationship)
        wires.extend([payload, wire])
        return wire
    mesh.post = post
    for i, name in enumerate(['Author', 'Bob', 'Carol', 'Later']):
        node = _node(tmp_path, name, 18400 + i, mesh)
        nodes.append(node)
        imports = LibraryImportStore(node.home / 'library-imports')
        consumption = ConsumptionStore(node.home / 'consumption.json')
        reader = ReaderCache(node.home / 'reader-cache')
        native = RynmeshStore(home=node.home, network_dir=tmp_path / 'network')
        content = FriendContent(store=native, imports=imports, cache=lambda reader=reader: reader,
                                consumption=lambda consumption=consumption: consumption)
        contents.append(content)
        feeds.append(FriendFeed(store=FeedStore(node.home, messaging_key=node.messaging_private),
                               friends=lambda node=node: node, content=lambda content=content: content))
    for node in nodes[1:3]:
        node.join(nodes[0].create_invite()['invite_uri'])
    imported = contents[0].imports.save('Private article body 中文'.encode(), filename='article.txt', mime='text/plain',
                                        source={'title': 'Private feed marker'})
    reference = {'item_id': 'import:' + imported['import_id']}
    return mesh, nodes, feeds, contents, reference, wires


def selected(author, *peers):
    return {'mode': 'selected', 'relationship_ids': [author.store.relationship_for_peer(peer.peer_id)['relationship_id'] for peer in peers]}


def publish(feed, reference, audience, *, identifier=None, expected_revision=0):
    identifier = identifier or uuid.uuid4().hex
    draft = feed.save_draft(identifier, reference=reference, audience=audience,
                           expected_revision=expected_revision, operation_id=uuid.uuid4().hex)
    return feed.publish(identifier, expected_revision=draft['revision'], operation_id=uuid.uuid4().hex,
                        confirm_all_friends=audience['mode'] == 'all_friends')


def test_three_identities_explicit_publish_audience_and_encrypted_transport(tmp_path):
    _, nodes, feeds, _, reference, wires = setup(tmp_path)
    author, bob, carol, _ = nodes
    a, b, c, _ = feeds
    assert b.subscriptions() == [] and b.timeline() == []
    assert b.request(author.peer_id, 'list')['rows'] == []  # Saved alone is not published.
    identifier = uuid.uuid4().hex
    draft = a.save_draft(identifier, reference=reference, audience=selected(author, bob), expected_revision=0, operation_id=uuid.uuid4().hex)
    assert b.request(author.peer_id, 'list')['rows'] == []
    op = uuid.uuid4().hex
    published = a.publish(identifier, expected_revision=draft['revision'], operation_id=op)
    assert published['published']['published_at'] == published['published']['updated_at']
    assert a.publish(identifier, expected_revision=1, operation_id=op) == published
    page = b.request(author.peer_id, 'list')
    assert [row['id'] for row in page['rows']] == [identifier]
    assert 'library_id' not in page['rows'][0]['card'] and 'audience' not in page['rows'][0]
    assert c.request(author.peer_id, 'list')['rows'] == []
    with pytest.raises(FeedError, match='unavailable'):
        c.request(author.peer_id, 'fetch', id=identifier, revision=2)
    assert 'Private feed marker' not in json.dumps(wires)
    assert 'Private article body' not in json.dumps(wires)
    assert a.publications()[0]['current_audience'][0]['peer_id'] == bob.peer_id
    assert carol.peer_id not in json.dumps(a.publications()[0]['current_audience'])


def test_all_friends_requires_confirmation_and_includes_later_friend_only_when_selected(tmp_path):
    _, nodes, feeds, _, reference, _ = setup(tmp_path)
    author, bob, _, later = nodes
    a, _, _, d = feeds
    own = publish(a, reference, selected(author, bob))
    identifier = uuid.uuid4().hex
    a.save_draft(identifier, reference=reference, audience={'mode': 'all_friends', 'relationship_ids': []},
                 expected_revision=0, operation_id=uuid.uuid4().hex)
    with pytest.raises(FeedError, match='confirmation_required'):
        a.publish(identifier, expected_revision=1, operation_id=uuid.uuid4().hex)
    assert a.store.read()['publications'][identifier]['published'] is None
    a.publish(identifier, expected_revision=1, operation_id=uuid.uuid4().hex, confirm_all_friends=True)
    later.join(author.create_invite()['invite_uri'])
    assert [row['id'] for row in d.request(author.peer_id, 'list')['rows']] == [identifier]
    with pytest.raises(FeedError, match='unavailable'):
        d.request(author.peer_id, 'fetch', id=own['id'], revision=own['published']['revision'])


def test_refresh_revision_read_restart_unsubscribe_and_independent_copy(tmp_path):
    mesh, nodes, feeds, contents, reference, wires = setup(tmp_path)
    author, bob, *_ = nodes
    a, b, *_ = feeds
    published = publish(a, reference, selected(author, bob))
    rid = bob.store.relationship_for_peer(author.peer_id)['relationship_id']
    with pytest.raises(FeedError, match='subscription_required'):
        b.refresh(rid)
    b.subscribe(rid, enabled=True, expected_revision=0)
    b.refresh(rid)
    b.refresh(rid)
    assert len(b.timeline()[0]['rows']) == 1
    row = b.timeline()[0]['rows'][0]
    assert row['read'] is False
    copied = b.fetch(rid, row['id'], expected_revision=row['revision'])
    b.mark_read(rid, row['id'], expected_revision=row['revision'])
    restarted = FriendFeed(store=FeedStore(bob.home, messaging_key=bob.messaging_private), friends=lambda: bob, content=lambda: contents[1])
    assert restarted.timeline()[0]['rows'][0]['read'] is True
    draft = a.save_draft(row['id'], reference=reference, audience=selected(author, bob), expected_revision=published['revision'], operation_id=uuid.uuid4().hex)
    assert b.request(author.peer_id, 'list')['rows'][0]['revision'] == row['revision']
    a.publish(row['id'], expected_revision=draft['revision'], operation_id=uuid.uuid4().hex)
    b.refresh(rid)
    assert len(b.timeline()[0]['rows']) == 1 and b.timeline()[0]['rows'][0]['read'] is False
    checked = b.timeline()[0]['checked_at']
    mesh.online.remove(author.endpoint)
    with pytest.raises(FeedError, match='friend_unreachable'):
        b.refresh(rid)
    assert b.timeline()[0]['checked_at'] == checked and b.timeline()[0]['error_code'] == 'feed_friend_unreachable'
    b.subscribe(rid, enabled=False, expected_revision=1)
    assert b.timeline() == [] and b.run_once() is False
    assert contents[1].imports.read_bytes(copied['import_id']) == 'Private article body 中文'.encode()
    # New publications after unsubscribe must not trigger a fetch or remove the saved copy.
    mesh.online.add(author.endpoint)
    publish(a, reference, selected(author, bob))
    before = len(wires)
    assert b.run_once() is False and b.timeline() == []
    assert len(wires) == before
    assert bob.store.relationship_for_peer(author.peer_id)['status'] == 'active'
    assert contents[1].imports.read_bytes(copied['import_id']) == 'Private article body 中文'.encode()


def test_full_catalogue_removes_revoked_second_page_entries_and_cursor_changes(tmp_path):
    _, nodes, feeds, _, reference, _ = setup(tmp_path)
    author, bob, carol, _ = nodes
    a, b, _, _ = feeds
    publications = [publish(a, reference, selected(author, bob)) for _ in range(23)]
    rid = bob.store.relationship_for_peer(author.peer_id)['relationship_id']
    b.subscribe(rid, enabled=True, expected_revision=0)
    page = b.refresh(rid)
    assert len(page['records']) == 20 and page['next_cursor']
    old_cursor = page['next_cursor']
    page = b.refresh(rid, cursor=old_cursor)
    assert len(page['records']) == 23
    assert page['next_cursor'] == ''
    assert b.refresh(rid)['next_cursor'] == ''  # Automatic refresh preserves pagination progress.
    oldest = publications[0]
    publish(a, reference, selected(author, carol), identifier=oldest['id'], expected_revision=oldest['revision'])
    with pytest.raises(FeedError, match='results_changed'):
        b.request(author.peer_id, 'list', cursor=old_cursor)
    b.refresh(rid)
    assert oldest['id'] not in [row['id'] for row in b.timeline()[0]['rows']]
    with pytest.raises(FeedError, match='unavailable'):
        b.request(author.peer_id, 'fetch', id=oldest['id'], revision=2)


def test_unsubscribe_while_refresh_in_flight_cannot_restore_entries(tmp_path):
    _, nodes, feeds, _, reference, _ = setup(tmp_path)
    author, bob, *_ = nodes
    a, b, *_ = feeds
    publish(a, reference, selected(author, bob))
    rid = bob.store.relationship_for_peer(author.peer_id)['relationship_id']
    b.subscribe(rid, enabled=True, expected_revision=0)
    entered, release = threading.Event(), threading.Event()
    request = b.request
    def paused(*args, **kwargs):
        value = request(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return value
    b.request = paused
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(b.refresh, rid)
        assert entered.wait(3)
        try:
            b.subscribe(rid, enabled=False, expected_revision=1)
            b.subscribe(rid, enabled=True, expected_revision=2)
        finally:
            release.set()
        with pytest.raises(FeedError, match='superseded'):
            pending.result(5)
    assert b.timeline()[0]['rows'] == []


def test_stop_and_relationship_revocation_rechecked_after_content_resolution(tmp_path):
    _, nodes, feeds, contents, reference, _ = setup(tmp_path)
    author, bob, *_ = nodes
    a, b, *_ = feeds
    published = publish(a, reference, selected(author, bob))
    resolve = contents[0].resolve
    def stop_during_resolve(library_id):
        resource = resolve(library_id)
        a.stop(published['id'], expected_revision=published['revision'], operation_id=uuid.uuid4().hex)
        return resource
    contents[0].resolve = stop_during_resolve
    with pytest.raises(FeedError, match='unavailable'):
        b.request(author.peer_id, 'fetch', id=published['id'], revision=2)
    assert b.request(author.peer_id, 'list')['rows'] == []
    contents[0].resolve = resolve
    another = publish(a, reference, selected(author, bob))
    rid = author.store.relationship_for_peer(bob.peer_id)['relationship_id']
    def revoke_during_resolve(library_id):
        resource = resolve(library_id)
        author.revoke(rid, notify=False)
        return resource
    contents[0].resolve = revoke_during_resolve
    with pytest.raises(FeedError, match='inactive'):
        b.request(author.peer_id, 'fetch', id=another['id'], revision=2)


def test_encrypted_state_failed_commit_unknown_fields_and_future_version(tmp_path, monkeypatch):
    _, nodes, feeds, _, reference, _ = setup(tmp_path)
    author, bob, *_ = nodes
    a, *_ = feeds
    identifier = uuid.uuid4().hex
    a.save_draft(identifier, reference=reference, audience=selected(author, bob), expected_revision=0, operation_id=uuid.uuid4().hex)
    assert b'Private feed marker' not in a.store.path.read_bytes()
    def extensions(data):
        data['extension'] = 'keep'
        data['publications'][identifier]['extension'] = 'also keep'
    a.store.mutate(extensions)
    import rynmesh.friend_feed.store as module
    atomic = module.atomic_write_json
    def fail(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(module, 'atomic_write_json', fail)
    with pytest.raises(OSError):
        a.publish(identifier, expected_revision=1, operation_id=uuid.uuid4().hex)
    assert a.publications()[0]['published'] is None
    monkeypatch.setattr(module, 'atomic_write_json', atomic)
    a.publish(identifier, expected_revision=1, operation_id=uuid.uuid4().hex)
    assert a.store.read()['extension'] == 'keep'
    assert a.store.read()['publications'][identifier]['extension'] == 'also keep'
    assert 'extension' not in a.publications()[0]
    envelope = read_json(a.store.path)
    envelope['version'] = 'ryn.friend-feed.v999'
    atomic_write_json(a.store.path, envelope)
    before = a.store.path.read_bytes()
    with pytest.raises(FeedError, match='version_unsupported'):
        a.stop(identifier, expected_revision=2, operation_id=uuid.uuid4().hex)
    assert a.store.path.read_bytes() == before


def test_owner_routes_authentication_bounded_requests_and_reinstallation(tmp_path):
    _, nodes, _, contents, reference, _ = setup(tmp_path)
    author, bob, *_ = nodes
    app, workers = FastAPI(), BackgroundWorkerRegistry()
    def guard(request):
        if request.headers.get('x-owner-test') != 'yes':
            raise HTTPException(403, detail='owner_required')
    def install():
        return install_friend_feed(app, home=author.home, messaging_key=author.messaging_private,
            friends=lambda: author, content=lambda: contents[0], local_control=guard, workers=workers)
    first = install()
    second = install()
    assert first is not second
    assert len([route for route in app.routes if route.name == 'friend_feed_publications']) == 1
    assert [spec.name for spec in workers.specs()] == ['friend-feed.refresh']
    with TestClient(app) as client:
        assert client.get('/api/local/friend-feed').status_code == 403
        assert client.post(PATH, json={}).status_code == 403
        assert client.post(PATH, content=b'x' * 8193).status_code == 413
        headers = {'x-owner-test': 'yes'}
        identifier = uuid.uuid4().hex
        prefix = '/api/local/friend-feed/publications/' + identifier
        value = {'reference': reference, 'audience': selected(author, bob), 'expected_revision': 0, 'operation_id': uuid.uuid4().hex}
        assert client.post(prefix + '/draft', headers=headers, json=value).json()['published'] is None
        assert len(second.publications()) == 1
        result = client.post(prefix + '/publish', headers=headers, json={'expected_revision': 1, 'operation_id': uuid.uuid4().hex})
        assert result.status_code == 200 and result.json()['published']['revision'] == 2
        assert len(client.get('/api/local/friend-feed/publications', headers=headers).json()['publications']) == 1
