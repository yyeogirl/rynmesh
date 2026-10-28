"""Actual deletion faults, durable receipts and independently created downloads."""
from pathlib import Path

import pytest
from test_offline_reading import BODY, download, fixture, row

from rynmesh.atomic_io import atomic_write_json
from rynmesh.offline_reading import store as storage
from rynmesh.offline_reading.fetch import OfflineError
from rynmesh.offline_reading.service import OfflineReading
from rynmesh.offline_reading.store import OfflineStore


def block_copy(monkeypatch, path):
    unlink = Path.unlink
    def fail(candidate, *args, **kwargs):
        if candidate == path:
            raise OSError('copy locked')
        return unlink(candidate, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail)


def test_file_failure_restart_and_retry_preserve_new_download_and_replay_receipt(tmp_path, monkeypatch):
    f = fixture(tmp_path, images=False)
    old = download(f)
    old_path = f.service.store._path(old['job_id'])
    preview = f.service.clear_preview()
    before = f.consumption.path.read_bytes()
    with monkeypatch.context() as patch:
        block_copy(patch, old_path)
        with pytest.raises(OSError, match='copy locked'):
            f.service.clear(review_token=preview['review_token'])
        assert row(f)['state'] == 'cleared'
        assert f.service.status()['cleanup']['done'] is False
        assert old_path.exists()
        restarted = OfflineReading(store=OfflineStore(tmp_path, messaging_key=f.key), sources=f.sources)
        restarted.recover()
        f.service = restarted
        with pytest.raises(OfflineError, match='offline_cleanup_pending'):
            f.service.clear_preview()
        newer = download(f)
        assert newer['job_id'] != old['job_id']
        assert old_path.exists()  # Other download GC cannot bypass this review.
    new_path = f.service.store._path(newer['job_id'])
    new_bytes = new_path.read_bytes()
    result = f.service.clear(review_token=preview['review_token'])
    assert result['freed_bytes'] == preview['bytes']
    assert f.service.status()['cleanup']['done']
    assert not old_path.exists() and new_path.read_bytes() == new_bytes
    assert f.service.read(f.item['item_id'])['text'] == BODY
    assert f.service.clear(review_token=preview['review_token']) == result
    assert f.consumption.path.read_bytes() == before


def test_commit_failure_does_not_remove_copy_and_original_review_can_retry(tmp_path, monkeypatch):
    f = fixture(tmp_path, images=False)
    old = download(f)
    preview = f.service.clear_preview()
    before = f.service.store.path.read_bytes()
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError('metadata unavailable')
        patch.setattr(storage, 'atomic_write_json', fail)
        with pytest.raises(OSError):
            f.service.clear(review_token=preview['review_token'])
    assert f.service.store.path.read_bytes() == before
    assert f.service.store._path(old['job_id']).exists()
    assert f.service.clear(review_token=preview['review_token'])['freed_bytes'] > 0


def test_loss_of_completion_commit_retries_after_file_is_already_removed(tmp_path, monkeypatch):
    f = fixture(tmp_path, images=False)
    old = download(f)
    preview = f.service.clear_preview()
    write, writes = storage.atomic_write_json, []
    def fail_second(*args, **kwargs):
        writes.append(1)
        if len(writes) == 2:
            raise OSError('completion unavailable')
        return write(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'atomic_write_json', fail_second)
        with pytest.raises(OSError):
            f.service.clear(review_token=preview['review_token'])
    assert not f.service.store._path(old['job_id']).exists()
    assert not f.service.status()['cleanup']['done']
    result = f.service.clear(review_token=preview['review_token'])
    assert result['freed_bytes'] == preview['bytes']
    assert f.service.status()['cleanup']['done']


def test_changed_remaining_copy_needs_new_review_and_future_copy_cannot_be_approved(tmp_path, monkeypatch):
    f = fixture(tmp_path, images=False)
    old = download(f)
    path = f.service.store._path(old['job_id'])
    preview = f.service.clear_preview()
    with monkeypatch.context() as patch:
        block_copy(patch, path)
        with pytest.raises(OSError):
            f.service.clear(review_token=preview['review_token'])
    path.write_bytes(b'changed managed copy')
    with pytest.raises(OfflineError, match='offline_cleanup_copy_changed'):
        f.service.clear(review_token=preview['review_token'])
    approval = f.service.clear_remaining_preview()
    path.write_bytes(b'another changed copy')
    with pytest.raises(OfflineError, match='offline_clear_review_changed'):
        f.service.clear_remaining(review_token=approval['review_token'])
    atomic_write_json(path, {'version': 'ryn.offline-reading.v999'})
    with pytest.raises(OfflineError, match='offline_version_unsupported'):
        f.service.clear_remaining_preview()
    path.write_bytes(b'current damaged known copy')
    approval = f.service.clear_remaining_preview()
    assert approval['files'] == 1 and approval['bytes'] == path.stat().st_size
    result = f.service.clear_remaining(review_token=approval['review_token'])
    assert not path.exists() and result['freed_bytes'] == approval['bytes']
    assert f.service.clear_remaining(review_token=approval['review_token']) == result


def test_equal_size_file_change_invalidates_original_review_before_any_erasure(tmp_path):
    f = fixture(tmp_path, images=False)
    old = download(f)
    path = f.service.store._path(old['job_id'])
    path.write_bytes(b'first broken version')
    preview = f.service.clear_preview()
    before = f.service.store.path.read_bytes()
    path.write_bytes(b'other broken version')
    with pytest.raises(OfflineError, match='offline_clear_review_changed'):
        f.service.clear(review_token=preview['review_token'])
    assert f.service.store.path.read_bytes() == before and path.exists()


def test_reviewed_cleanup_leaves_unreviewed_orphan_and_preserves_unknown_metadata(tmp_path):
    f = fixture(tmp_path, images=False)
    download(f)
    f.service.store.mutate(lambda data: data.update(extension={'keep': True}))
    preview = f.service.clear_preview()
    orphan = f.service.store._path('f' * 32)
    orphan.write_bytes(b'unreviewed orphan')
    f.service.clear(review_token=preview['review_token'])
    assert orphan.read_bytes() == b'unreviewed orphan'
    assert f.service.store.read()['extension'] == {'keep': True}


def test_receipt_binding_generation_and_future_format_fail_closed(tmp_path):
    f = fixture(tmp_path, images=False)
    download(f)
    preview = f.service.clear_preview()
    f.service.clear(review_token=preview['review_token'])
    with pytest.raises(OfflineError, match='offline_clear_review_changed'):
        f.service.clear(review_token=preview['review_token'], item_id=f.item['item_id'])
    second = f.service.clear_preview()
    f.service.clear(review_token=second['review_token'])
    assert f.service.status()['cleanup']['sequence'] == 2
    assert f.service.clear_preview()['review_token'] != second['review_token']
    data = f.service.store.read()
    data['cleanup']['version'] = 'ryn.offline-cleanup.v999'
    atomic_write_json(f.service.store.path, f.service.store._encode(data))
    before = f.service.store.path.read_bytes()
    with pytest.raises(OfflineError, match='offline_cleanup_version_unsupported'):
        f.service.clear(review_token=second['review_token'])
    assert f.service.store.path.read_bytes() == before


def test_owner_routes_expose_pending_progress_and_review_current_file_before_retry(tmp_path, monkeypatch):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from rynmesh.background_workers import BackgroundWorkerRegistry
    from rynmesh.offline_reading.routes import install_offline_reading

    f = fixture(tmp_path, images=False)
    old = download(f)
    path = f.service.store._path(old['job_id'])
    app = FastAPI()
    def owner(request):
        if request.headers.get('x-owner') != 'yes':
            raise HTTPException(403)
    install_offline_reading(app, home=tmp_path, messaging_key=f.key, consumption=lambda: f.consumption,
        imports=lambda: f.imports, native=lambda: None, local_control=owner,
        workers=BackgroundWorkerRegistry(), fetch=f.sources.fetch)
    client = TestClient(app)
    prefix = '/api/local/offline-reading'
    for action in ('clear-remaining-preview', 'clear-remaining'):
        assert client.post(prefix + '/' + action, json={}).status_code == 403
    client.headers['x-owner'] = 'yes'
    preview = client.post(prefix + '/clear-preview', json={}).json()
    with monkeypatch.context() as patch:
        block_copy(patch, path)
        response = client.post(prefix + '/clear', json={'review_token': preview['review_token']})
    assert response.status_code == 503 and response.json()['detail'] == 'offline_operation_unavailable'
    assert client.get(prefix).json()['cleanup']['done'] is False
    path.write_bytes(b'a reviewed replacement')
    pending = client.post(prefix + '/clear-remaining-preview', json={}).json()
    assert pending['files'] == 1
    response = client.post(prefix + '/clear-remaining', json={'review_token': pending['review_token']})
    assert response.status_code == 200 and not path.exists()
    assert client.get(prefix).json()['cleanup']['done'] is True
