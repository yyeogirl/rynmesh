"""Portable exports use real stores, verification and HTTP ownership checks."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import threading
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_ask_history import sample
from test_friend_feed import publish, selected, setup
from test_offline_reading import BODY, download, fixture, png

from rynmesh.atomic_io import atomic_write_json
from rynmesh.privacy_export import archive as package
from rynmesh.privacy_export.archive import SCOPES, ExportBuilder, ExportError
from rynmesh.privacy_export.routes import ArchiveResponse, install_privacy_export
from rynmesh.privacy_export.sources import ProductDataSources

PREFIX = '/api/local/privacy/export'


def unpack(artifact):
    try:
        with zipfile.ZipFile(artifact.file) as zipped:
            assert zipped.testzip() is None
            return {name: zipped.read(name) for name in zipped.namelist()}
    finally:
        artifact.close()


def verify_manifest(files):
    manifest = json.loads(files['manifest.json'])
    assert set(files) == {'manifest.json'} | {row['path'] for row in manifest['files']}
    for row in manifest['files']:
        assert row['bytes'] == len(files[row['path']])
        assert row['sha256'] == hashlib.sha256(files[row['path']]).hexdigest()
    assert manifest['total_uncompressed_bytes'] == sum(row['bytes'] for row in manifest['files'])
    return manifest


def test_complete_node_exports_every_selected_scope_without_model_or_network(tmp_path, monkeypatch):
    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv('RYNMESH_HOME', str(tmp_path / 'node'))
    monkeypatch.setenv('RYNMESH_AUTO_REGISTER', '0')
    monkeypatch.setenv('RYNMESH_ALLOW_REMOTE_CONTROL', '1')
    app = create_app(RynmeshStore())
    app.state.ask_ryn.conversations.save(sample(), expected_revision=0)
    app.state.ask_ryn.conversations.save_draft('Unsent draft', expected_revision=0)
    imported = app.state.friends.content.imports.save(b'Saved local document', filename='article.txt', mime='text/plain')
    client = TestClient(app)
    scopes = client.get(PREFIX + '/scopes').json()['scopes']
    response = client.post(PREFIX + '/archive', json={'scopes': [row['id'] for row in scopes]})
    assert response.status_code == 200, response.text if response.status_code != 200 else ''
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['content-type'] == 'application/zip'
    with zipfile.ZipFile(io.BytesIO(response.content)) as zipped:
        files = {name: zipped.read(name) for name in zipped.namelist()}
    manifest = verify_manifest(files)
    assert manifest['selected_scopes'] == list(SCOPES)
    assert all(row['status'] == 'included' for row in manifest['scopes'])
    assert json.loads(files['conversations/history.json'])['draft']['text'] == 'Unsent draft'
    assert json.loads(files['saved_documents/index.json'])['documents'][0]['import_id'] == imported['import_id']
    assert files['saved_documents/0/original.txt'] == b'Saved local document'
    assert not app.state.privacy_export.builder.running.locked()
    assert app.state.ask_ryn.conversations.get(sample()['id'])


def test_real_friend_feed_and_documents_keep_audiences_and_exclude_unknown_credentials(tmp_path):
    _, nodes, feeds, contents, reference, _ = setup(tmp_path)
    author, bob, _, _ = nodes
    feed = feeds[0]
    publication = publish(feed, reference, selected(author, bob))
    author.send_content_card(bob.peer_id, contents[0].prepare(reference))
    marker = 'DO_NOT_EXPORT_UNKNOWN_CREDENTIAL'
    with author.store._guard():
        data = author.store._read(author.store.state_path, author.store._empty())
        data['unknown_credentials'] = marker
        for row in data['relationships'].values():
            row['unknown_credentials'] = marker
        for row in data['cards'].values():
            row['card']['unknown_credentials'] = marker
        author.store._write(author.store.state_path, data)
    def extend(data):
        data['unknown_credentials'] = marker
        data['publications'][publication['id']]['draft']['card']['unknown_credentials'] = marker
    feed.store.mutate(extend)
    app = SimpleNamespace(state=SimpleNamespace(friends=SimpleNamespace(service=author, content=contents[0]),
                                                friend_feed=SimpleNamespace(service=feed)))
    files = unpack(ExportBuilder(ProductDataSources(app)).build(['friends', 'friend_feed', 'saved_documents']))
    verify_manifest(files)
    assert marker.encode() not in b''.join(files.values())
    exported = json.loads(files['friend_feed/updates.json'])['publications'][0]
    assert exported['published']['audience'] == selected(author, bob)
    friends = json.loads(files['friends/relationships-and-cards.json'])
    assert len(friends['relationships']) == 2 and len(friends['cards']) == 1
    assert friends['relationships'][0]['permissions']
    assert 'secrets' not in friends and 'invites' not in friends


def test_verified_offline_body_images_and_metadata_export_without_refetch(tmp_path):
    f = fixture(tmp_path)
    download(f)
    calls = list(f.network['calls'])
    app = SimpleNamespace(state=SimpleNamespace(offline_reading=SimpleNamespace(service=f.service),
                                                consumption_store=f.consumption))
    files = unpack(ExportBuilder(ProductDataSources(app)).build(['reading', 'offline_reading']))
    verify_manifest(files)
    assert json.loads(files['offline_reading/0/article.json'])['text'] == BODY
    assert files['offline_reading/0/image-0.png'] == png()
    assert json.loads(files['reading/history.json'])['records'][0]['progress'] == .45
    assert f.network['calls'] == calls
    assert not json.loads(files['offline_reading/index.json'])['unfinished_checkpoints_included']


def test_corrupt_verified_image_fails_export_and_releases_temporary_storage(tmp_path, monkeypatch):
    f = fixture(tmp_path)
    download(f)
    job_id = f.service.status()['records'][0]['current']['job_id']
    bundle = f.service.store.bundle(job_id)
    bundle['images'][0]['data'] = 'Y29ycnVwdA=='
    atomic_write_json(f.service.store._path(job_id), f.service.store._encode(bundle))
    created = []
    factory = package.tempfile.SpooledTemporaryFile
    def remember(*args, **kwargs):
        file = factory(*args, **kwargs)
        created.append(file)
        return file
    monkeypatch.setattr(package.tempfile, 'SpooledTemporaryFile', remember)
    app = SimpleNamespace(state=SimpleNamespace(offline_reading=SimpleNamespace(service=f.service)))
    builder = ExportBuilder(ProductDataSources(app))
    with pytest.raises(ExportError) as raised:
        builder.build(['offline_reading'])
    assert raised.value.scope == 'offline_reading'
    assert created[0].closed and not builder.running.locked()


@pytest.mark.parametrize('entry', ['../secrets.json', '/absolute.json', 'a/../../b', 'a\\b', ''])
def test_archive_never_accepts_unsafe_entry_names(entry):
    builder = ExportBuilder(lambda _: [(entry, b'text')])
    with pytest.raises(ExportError, match='privacy_export_invalid_entry'):
        builder.build(['reading'])
    assert not builder.running.locked()


def test_capacity_failure_returns_no_archive_and_next_request_can_retry(monkeypatch):
    monkeypatch.setattr(package, 'MAX_TOTAL', 4)
    builder = ExportBuilder(lambda _: [('data.txt', b'12345')])
    with pytest.raises(ExportError, match='privacy_export_capacity_exhausted'):
        builder.build(['reading'])
    builder.sources = lambda _: [('data.txt', b'1234')]
    artifact = builder.build(['reading'])
    with pytest.raises(ExportError, match='privacy_export_busy'):
        builder.build(['reading'])
    artifact.close()
    artifact.close()
    assert not builder.running.locked()


def test_owner_guard_reinstallation_validation_and_private_source_errors():
    app = FastAPI()
    calls = []
    def owner(request):
        if request.headers.get('x-owner') != 'yes':
            raise HTTPException(403)
    def source(scope):
        calls.append(scope)
        raise OSError('private-path secret-token')
    install_privacy_export(app, local_control=owner, sources=source)
    count = len(app.routes)
    install_privacy_export(app, local_control=owner, sources=source)
    assert len(app.routes) == count
    client = TestClient(app)
    assert client.get(PREFIX + '/scopes').status_code == 403
    assert client.post(PREFIX + '/archive', json={'scopes': ['reading']}).status_code == 403
    assert not calls
    client.headers['x-owner'] = 'yes'
    for body in ({}, {'scopes': []}, {'scopes': ['unknown']}, {'scopes': ['reading', 'reading']},
                 {'scopes': ['reading'], 'secrets': True}):
        assert client.post(PREFIX + '/archive', json=body).status_code == 400
    assert client.post(PREFIX + '/archive', content=b'x' * 8193).status_code == 413
    assert not calls
    response = client.post(PREFIX + '/archive', json={'scopes': ['reading']})
    assert response.status_code == 503
    assert response.json() == {'detail': {'code': 'privacy_export_source_unavailable', 'scope': 'reading'}}
    assert 'secret' not in response.text and not app.state.privacy_export.builder.running.locked()


def test_failed_response_send_closes_archive_and_busy_lock():
    builder = ExportBuilder(lambda _: [('history.json', {'data': 'private'})])
    artifact = builder.build(['reading'])
    async def fail_send(_):
        raise RuntimeError('connection closed')
    async def receive():
        return {'type': 'http.disconnect'}
    async def run():
        with pytest.raises(RuntimeError, match='connection closed'):
            await ArchiveResponse(artifact)({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, fail_send)
    asyncio.run(run())
    assert artifact.file.closed and not builder.running.locked()


def test_cancelled_build_closes_archive_after_worker_finishes():
    app = FastAPI()
    started, release = threading.Event(), threading.Event()
    def source(_):
        started.set()
        assert release.wait(5)
        yield 'history.json', {'data': 'private'}
    state = install_privacy_export(app, local_control=lambda _: None, sources=source)
    endpoint = next(row.endpoint for row in app.routes if getattr(row, 'path', '') == PREFIX + '/archive')
    async def stream():
        yield b'{"scopes":["reading"]}'
    request = SimpleNamespace(stream=stream)
    async def run():
        task = asyncio.create_task(endpoint(request))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        for _ in range(100):
            if not state.builder.running.locked():
                break
            await asyncio.sleep(.01)
        assert not state.builder.running.locked()
    asyncio.run(run())
