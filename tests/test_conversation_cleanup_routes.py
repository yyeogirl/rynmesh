from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_conversation_cleanup import fixture

from rynmesh.ask_ryn.cleanup_routes import install_conversation_cleanup
from rynmesh.background_workers import BackgroundWorkerRegistry

PREFIX = '/api/local/privacy/conversations'


def test_owner_review_cleanup_status_and_reinstallation_use_current_state(tmp_path):
    cleanup, _ = fixture(tmp_path)
    app = FastAPI()
    def owner(request):
        if request.headers.get('x-owner') != 'yes':
            raise HTTPException(403)
    arguments = dict(store=SimpleNamespace(home=tmp_path), home=tmp_path,
        workers=BackgroundWorkerRegistry(), local_control=owner, factory=lambda: cleanup)
    install_conversation_cleanup(app, **arguments)
    original = len(app.routes)
    install_conversation_cleanup(app, **arguments)
    assert len(app.routes) == original
    client = TestClient(app)
    assert client.get(PREFIX + '/preview').status_code == 403
    client.headers['x-owner'] = 'yes'
    preview = client.get(PREFIX + '/preview').json()
    result = client.post(PREFIX + '/jobs', json={'review_token': preview['review_token']})
    assert result.status_code == 200, result.text
    value = result.json()
    assert value['local_copies_complete'] and not value['remote_confirmed']
    assert value['browser_cleanup_required']
    assert client.get(PREFIX + '/jobs').json()['jobs'] == [value]
    assert client.post(PREFIX + '/jobs/' + value['id'] + '/resume').json() == value
    def deny(_):
        raise HTTPException(403)
    install_conversation_cleanup(app, **{**arguments, 'local_control': deny})
    for method, suffix in [('get', '/jobs'), ('get', '/jobs/' + value['id']), ('post', '/jobs/' + value['id'] + '/resume'),
                            ('post', '/jobs/' + value['id'] + '/cancel'), ('get', '/jobs/' + value['id'] + '/backups')]:
        assert getattr(client, method)(PREFIX + suffix).status_code == 403


def test_malformed_oversize_and_changed_review_cannot_trigger_cleanup(tmp_path):
    cleanup, _ = fixture(tmp_path)
    app = FastAPI()
    install_conversation_cleanup(app, store=SimpleNamespace(home=tmp_path), home=tmp_path,
        workers=BackgroundWorkerRegistry(), local_control=lambda _: None, factory=lambda: cleanup)
    client = TestClient(app)
    preview = client.get(PREFIX + '/preview').json()
    for value in ({}, [], {'review_token': preview['review_token'], 'erase_all': True}):
        assert client.post(PREFIX + '/jobs', json=value).status_code == 400
    assert client.post(PREFIX + '/jobs', content='x' * 8193).status_code == 413
    cleanup.source.save_draft('new work', expected_revision=0)
    result = client.post(PREFIX + '/jobs', json={'review_token': preview['review_token']})
    assert result.status_code == 409 and result.json()['detail'] == 'ask_privacy_review_changed'
    assert cleanup.source.draft()['text'] == 'new work'


def test_complete_node_installs_real_cleanup_dependencies(tmp_path, monkeypatch):
    from test_ask_history import sample

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv('RYNMESH_HOME', str(tmp_path / 'node'))
    monkeypatch.setenv('RYNMESH_AUTO_REGISTER', '0')
    monkeypatch.setenv('RYNMESH_ALLOW_REMOTE_CONTROL', '1')
    app = create_app(RynmeshStore())
    app.state.ask_ryn.conversations.save(sample(), expected_revision=0)
    client = TestClient(app)
    preview = client.get(PREFIX + '/preview')
    assert preview.status_code == 200, preview.text
    result = client.post(PREFIX + '/jobs', json={'review_token': preview.json()['review_token']})
    assert result.status_code == 200, result.text
    assert result.json()['local_copies_complete']
    assert client.get('/api/local/ask/conversations').json()['conversations'] == []
