from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from rynmesh.services.library_cleanup_routes import install_library_cleanup_routes
from rynmesh.services.library_imports import LibraryImportStore

PREFIX = '/api/local/privacy/documents'


def test_current_owner_and_store_reinstall_and_body_limits(tmp_path):
    app = FastAPI()
    store = LibraryImportStore(tmp_path / 'copies')
    row = store.save(b'Private synthetic document', filename='article.txt', mime='text/plain')
    def guard(request):
        if request.headers.get('x-owner') != 'yes':
            raise HTTPException(403)
    app.state.friends = SimpleNamespace(local_control=guard, content=SimpleNamespace(imports=store))
    install_library_cleanup_routes(app)
    count = len(app.routes)
    install_library_cleanup_routes(app)
    assert len(app.routes) == count
    client = TestClient(app)
    assert client.post(PREFIX + '/preview', content='x'*9000).status_code == 403
    client.headers['x-owner'] = 'yes'
    assert client.post(PREFIX + '/preview', content='x'*8193).status_code == 413
    assert client.post(PREFIX + '/preview', json={'scope': None, 'extra': 1}).status_code == 400
    assert client.post(PREFIX + '/preview', json={'scope': ['invalid']}).status_code == 409
    review = client.post(PREFIX + '/preview', json={'scope': row['import_id']}).json()
    assert review['documents'] == 1 and review['files'] == 3
    result = client.post(PREFIX + '/job', json={key: review[key] for key in ('scope', 'review_token')})
    assert result.status_code == 200 and result.json()['local_copies_complete']
    assert client.get(PREFIX + '/job').json()['job'] == result.json()
    def deny(_):
        raise HTTPException(403)
    app.state.friends.local_control = deny
    for method, path in [('post', '/preview'), ('get', '/job'), ('post', '/job'),
                         ('post', f'/job/{review["review_token"]}/resume'), ('get', f'/job/{review["review_token"]}/files'),
                         ('post', f'/job/{review["review_token"]}/files')]:
        assert getattr(client, method)(PREFIX + path).status_code == 403


def test_full_node_legacy_mutations_require_review_and_new_routes_are_wired(tmp_path, monkeypatch):
    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore
    monkeypatch.setenv('RYNMESH_HOME', str(tmp_path / 'node'))
    monkeypatch.setenv('RYNMESH_AUTO_REGISTER', '0')
    monkeypatch.setenv('RYNMESH_ALLOW_REMOTE_CONTROL', '1')
    app = create_app(RynmeshStore())
    store = app.state.friends.content.imports
    row = store.save(b'Synthetic document', filename='article.txt', mime='text/plain')
    client = TestClient(app)
    assert client.post('/api/local/friends/documents/clear').status_code == 409
    assert client.delete('/api/local/friends/documents/' + row['import_id']).status_code == 409
    assert store.body(row['import_id'])['text'] == 'Synthetic document'
    review = client.post(PREFIX + '/preview', json={'scope': None}).json()
    result = client.post('/api/local/friends/documents/clear', json={'review_token': review['review_token']})
    assert result.status_code == 200 and result.json()['local_copies_complete']
    store.save(b'Later document', filename='article.txt', mime='text/plain')
    assert client.post('/api/local/friends/documents/clear', json={'review_token': review['review_token']}).json() == result.json()
    assert len(store.list()) == 1
    from rynmesh.services.library_cleanup import LibraryCleanup
    monkeypatch.setattr(LibraryCleanup, 'preview', lambda *a: (_ for _ in ()).throw(OSError('secret private path')))
    failed = client.post(PREFIX + '/preview', json={'scope': None})
    assert failed.status_code == 503 and failed.json()['detail'] == 'library_cleanup_unavailable'
    assert 'secret private path' not in failed.text
