from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_reading_cleanup import ITEM, fixture

from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.services.reading_cleanup_routes import install_reading_cleanup

PREFIX = '/api/local/privacy/reading'


def test_owner_guard_reinstallation_bounded_body_and_actual_cleanup(tmp_path):
    cleanup = fixture(tmp_path)
    app = FastAPI()

    def owner(request):
        if request.headers.get('x-owner') != 'yes':
            raise HTTPException(403)

    args = dict(store=SimpleNamespace(home=tmp_path), home=tmp_path,
        workers=BackgroundWorkerRegistry(), local_control=owner, factory=lambda: cleanup)
    install_reading_cleanup(app, **args)
    count = len(app.routes)
    install_reading_cleanup(app, **args)
    assert len(app.routes) == count
    client = TestClient(app)
    assert client.post(PREFIX + '/job', content='x' * 9000).status_code == 403
    client.headers['x-owner'] = 'yes'
    assert client.post(PREFIX + '/job', content='x' * 8193).status_code == 413
    assert client.post(PREFIX + '/job', json={}).status_code == 400
    preview = client.get(PREFIX + '/preview').json()
    result = client.post(PREFIX + '/job', json={'review_token': preview['review_token']})
    assert result.status_code == 200, result.text
    value = result.json()
    assert value['local_copies_complete'] and not value['remote_confirmed']
    assert client.get(PREFIX + '/job').json()['job'] == value
    assert client.post(PREFIX + '/job/' + value['id'] + '/resume').json() == value

    def deny(_):
        raise HTTPException(403)

    install_reading_cleanup(app, **{**args, 'local_control': deny})
    for method, suffix in [('get', '/preview'), ('get', '/job'), ('post', '/job'),
                           ('post', '/job/' + value['id'] + '/resume'), ('post', '/job/' + value['id'] + '/cancel'),
                           ('get', '/job/' + value['id'] + '/backups'), ('post', '/job/' + value['id'] + '/backups')]:
        assert getattr(client, method)(PREFIX + suffix).status_code == 403


def test_stale_review_and_storage_errors_do_not_disclose_private_paths(tmp_path, monkeypatch):
    cleanup = fixture(tmp_path)
    app = FastAPI()
    install_reading_cleanup(app, store=SimpleNamespace(home=tmp_path), home=tmp_path,
        workers=BackgroundWorkerRegistry(), local_control=lambda _: None, factory=lambda: cleanup)
    client = TestClient(app)
    preview = client.get(PREFIX + '/preview').json()
    cleanup.source.record({**ITEM, 'item_id': 'new'}, 'bookmark')
    result = client.post(PREFIX + '/job', json={'review_token': preview['review_token']})
    assert result.status_code == 409 and result.json()['detail'] == 'reading_privacy_review_changed'
    assert len(cleanup.source.list()) == 2
    monkeypatch.setattr(cleanup, 'preview', lambda: (_ for _ in ()).throw(OSError('private-path-and-body-marker')))
    result = client.get(PREFIX + '/preview')
    assert result.status_code == 503 and result.json()['detail'] == 'reading_cleanup_unavailable'
    assert 'private-path-and-body-marker' not in result.text


def test_complete_node_installs_real_reading_cleanup_dependencies(tmp_path, monkeypatch):
    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv('RYNMESH_HOME', str(tmp_path / 'node'))
    monkeypatch.setenv('RYNMESH_AUTO_REGISTER', '0')
    monkeypatch.setenv('RYNMESH_ALLOW_REMOTE_CONTROL', '1')
    app = create_app(RynmeshStore())
    app.state.consumption_store.record(ITEM, 'bookmark')
    client = TestClient(app)
    preview = client.get(PREFIX + '/preview')
    assert preview.status_code == 200, preview.text
    result = client.post(PREFIX + '/job', json={'review_token': preview.json()['review_token']})
    assert result.status_code == 200, result.text
    assert result.json()['local_copies_complete']
    assert app.state.consumption_store.list() == []
