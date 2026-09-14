from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_friend_feed import publish, selected, setup

from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.friend_feed.cleanup import FeedCleanup
from rynmesh.friend_feed.routes import install_friend_feed

PREFIX = '/api/local/privacy/friend-feed'


def test_cleanup_routes_use_current_owner_guard_and_bounded_review_body(tmp_path):
    _, nodes, feeds, contents, reference, _ = setup(tmp_path)
    author, bob, *_ = nodes
    publish(feeds[0], reference, selected(author, bob))
    app, workers = FastAPI(), BackgroundWorkerRegistry()

    def owner(request):
        if request.headers.get('x-owner') != 'yes':
            raise HTTPException(403)

    args = dict(home=author.home, messaging_key=author.messaging_private, friends=lambda: author,
                content=lambda: contents[0], local_control=owner, workers=workers)
    install_friend_feed(app, **args)
    count = len(app.routes)
    install_friend_feed(app, **args)
    assert len(app.routes) == count
    assert [spec.name for spec in workers.specs()] == ['friend-feed.refresh']
    client = TestClient(app)
    assert client.post(PREFIX + '/job', content='x' * 9000).status_code == 403
    client.headers['x-owner'] = 'yes'
    assert client.post(PREFIX + '/job', content='x' * 8193).status_code == 413
    for body in ({}, {'review_token': 'x', 'all': True}, []):
        assert client.post(PREFIX + '/job', json=body).status_code == 400
    value = client.get(PREFIX + '/preview').json()
    assert value['active_publications'] == 1
    result = client.post(PREFIX + '/job', json={'review_token': value['review_token']})
    assert result.status_code == 200 and result.json()['local_copies_complete']
    assert not result.json()['remote_confirmed']
    assert client.get('/api/local/friend-feed/publications').json()['publications'] == []
    assert client.get(PREFIX + '/job').json()['job'] == result.json()

    def deny(_):
        raise HTTPException(403)

    install_friend_feed(app, **{**args, 'local_control': deny})
    identifier = result.json()['id']
    for method, path in [('get', '/preview'), ('get', '/job'), ('post', '/job'),
                         ('post', f'/job/{identifier}/resume'), ('get', f'/job/{identifier}/backups'),
                         ('post', f'/job/{identifier}/backups')]:
        assert getattr(client, method)(PREFIX + path).status_code == 403


def test_stale_review_and_storage_failures_are_safe(tmp_path, monkeypatch):
    _, nodes, feeds, contents, reference, _ = setup(tmp_path)
    author, bob, *_ = nodes
    app = FastAPI()
    install_friend_feed(app, home=author.home, messaging_key=author.messaging_private,
        friends=lambda: author, content=lambda: contents[0], local_control=lambda _: None, workers=BackgroundWorkerRegistry())
    client = TestClient(app)
    token = client.get(PREFIX + '/preview').json()['review_token']
    publish(feeds[0], reference, selected(author, bob))
    result = client.post(PREFIX + '/job', json={'review_token': token})
    assert result.status_code == 409 and result.json()['detail'] == 'feed_cleanup_review_changed'
    assert len(feeds[0].publications()) == 1
    monkeypatch.setattr(FeedCleanup, 'preview', lambda _: (_ for _ in ()).throw(OSError('private path marker')))
    result = client.get(PREFIX + '/preview')
    assert result.status_code == 503 and result.json()['detail'] == 'feed_operation_unavailable'
    assert 'private path marker' not in result.text


def test_complete_node_uses_real_feed_cleanup_store(tmp_path, monkeypatch):
    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv('RYNMESH_HOME', str(tmp_path / 'node'))
    monkeypatch.setenv('RYNMESH_AUTO_REGISTER', '0')
    monkeypatch.setenv('RYNMESH_ALLOW_REMOTE_CONTROL', '1')
    app = create_app(RynmeshStore())
    client = TestClient(app)
    preview = client.get(PREFIX + '/preview')
    assert preview.status_code == 200, preview.text
    result = client.post(PREFIX + '/job', json={'review_token': preview.json()['review_token']})
    assert result.status_code == 200, result.text
    assert app.state.friend_feed.service.store.read()['cleanup']['done'] == ['source', 'backups']
