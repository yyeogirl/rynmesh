"""Explicit offline copies: real encrypted stores, lifecycle and HTTP boundaries."""
from __future__ import annotations

import multiprocessing
import struct
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from rynmesh.atomic_io import atomic_write_json, read_json
from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.offline_reading import service as operations
from rynmesh.offline_reading import store as storage
from rynmesh.offline_reading.fetch import OfflineError, fetch_resource, image_mime
from rynmesh.offline_reading.routes import install_offline_reading
from rynmesh.offline_reading.service import OfflineReading, OfflineSources
from rynmesh.offline_reading.store import VERSION, OfflineStore, key_for
from rynmesh.services import peer_box
from rynmesh.services.consumption import ConsumptionStore
from rynmesh.services.library_imports import LibraryImportStore
from rynmesh.services.reader import extract_readable

URL = 'https://example.com/article'
BODY = 'Offline-only marker 正文，在断网和重启后仍应可读。 This is the complete saved article.'


def png(width=8, height=8):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\x00' + b'\x80\x40\x20' * width) * height)) + chunk(b'IEND', b''))


def fixture(tmp_path, *, images=True):
    key = peer_box.load_or_create_messaging_key(tmp_path / 'messaging.x25519')
    consumption = ConsumptionStore(tmp_path / 'consumption.json')
    imports = LibraryImportStore(tmp_path / 'library-imports')
    item = {'item_id': 'article:one', 'title': 'Saved article', 'source_title': 'Example',
            'content_kind': 'article', 'link': URL}
    consumption.record(item, 'bookmark')
    consumption.record(item, 'progress', progress=0.45)
    network = {'body': BODY, 'calls': [], 'images': images, 'fail': set()}
    def fetch(url, *, max_bytes, check):
        check()
        network['calls'].append(url)
        if url in network['fail']:
            raise OfflineError('offline_source_unreachable')
        if url == URL:
            image = '<img src="/picture.png" alt="Article illustration">' if network['images'] else ''
            data = f'<html><title>Downloaded title</title><body><article><p>{network["body"]}</p>{image}</article></body></html>'.encode()
            mime = 'text/html'
        else:
            data, mime = png(), 'image/png'
        assert len(data) <= max_bytes
        return {'data': data, 'mime': mime, 'url': url}
    sources = OfflineSources(consumption=lambda: consumption, imports=lambda: imports, native=lambda: None, fetch=fetch)
    service = OfflineReading(store=OfflineStore(tmp_path, messaging_key=key), sources=sources)
    return SimpleNamespace(service=service, sources=sources, key=key, home=tmp_path,
                           consumption=consumption, imports=imports, item=item, network=network)


def row(f):
    return f.service.status()['records'][0]


def download(f):
    f.service.request(f.item['item_id'])
    assert f.service.run_once()
    return f.service.read(f.item['item_id'])


def test_bookmark_is_not_download_and_committed_copy_survives_restart_without_network(tmp_path):
    f = fixture(tmp_path)
    with pytest.raises(OfflineError, match='offline_not_downloaded'):
        f.service.read(f.item['item_id'])
    first = f.service.request(f.item['item_id'])
    assert f.service.request(f.item['item_id']) == first
    assert row(f)['state'] == 'queued'
    with pytest.raises(OfflineError, match='offline_not_downloaded'):
        f.service.read(f.item['item_id'])
    assert f.service.run_once()
    body = f.service.read(f.item['item_id'])
    assert body['text'] == BODY and body['title'] == 'Downloaded title'
    assert body['url'] == URL and body['downloaded_at'] > 0 and body['partial'] is False
    assert row(f)['verified_bytes'] == len(BODY.encode()) + len(png())
    assert len(list(f.service.store.downloads.glob('*.json'))) == 1
    before = list(f.network['calls'])
    f.network['fail'] = {URL, 'https://example.com/picture.png'}
    restarted = OfflineReading(store=OfflineStore(tmp_path, messaging_key=f.key), sources=f.sources)
    restarted.recover()
    assert restarted.read(f.item['item_id']) == body
    assert restarted.image(f.item['item_id'], 0) == (png(), 'image/png')
    assert f.network['calls'] == before
    for path in f.service.store.root.rglob('*.json'):
        assert BODY.encode() not in path.read_bytes() and b'example.com' not in path.read_bytes()


def test_missing_image_is_partial_and_update_failure_keeps_previous_body(tmp_path):
    f = fixture(tmp_path)
    f.network['fail'].add('https://example.com/picture.png')
    old = download(f)
    assert old['partial'] and old['images'][0]['state'] == 'missing'
    with pytest.raises(OfflineError, match='offline_image_unavailable'):
        f.service.image(f.item['item_id'], 0)
    f.service.request(f.item['item_id'], update=True)
    f.network['fail'].add(URL)
    with pytest.raises(OfflineError, match='offline_source_unreachable'):
        f.service.run_once()
    assert row(f)['state'] == 'failed'
    assert f.service.read(f.item['item_id']) == old
    f.network['fail'].clear()
    f.network['body'] = BODY + ' A verified newer version.'
    f.service.retry(f.item['item_id'])
    assert f.service.run_once()
    new = f.service.read(f.item['item_id'])
    assert new['text'] != old['text'] and not new['partial']
    assert new['downloaded_at'] >= old['downloaded_at']
    assert len(list(f.service.store.downloads.glob('*.json'))) == 1
    with pytest.raises(OfflineError, match='offline_copy_changed'):
        f.service.image(f.item['item_id'], 0, job_id=old['job_id'])


@pytest.mark.parametrize('action', ['cancel', 'clear', 'restart'])
def test_inflight_download_is_fenced_and_restart_reuses_verified_body(tmp_path, action):
    f = fixture(tmp_path)
    started, release = threading.Event(), threading.Event()
    original = f.sources.fetch
    def blocked(url, **options):
        if url.endswith('picture.png'):
            started.set()
            assert release.wait(10)
        return original(url, **options)
    f.sources.fetch = blocked
    f.service.request(f.item['item_id'])
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(f.service.run_once)
        try:
            assert started.wait(10)
            assert row(f)['state'] == 'downloading'
            with pytest.raises(OfflineError, match='offline_not_downloaded'):
                f.service.read(f.item['item_id'])
            if action == 'cancel':
                assert f.service.cancel(f.item['item_id'])['state'] == 'cancel_requested'
            elif action == 'clear':
                preview = f.service.clear_preview()
                assert preview['pending'] == 1 and preview['copies'] == 0 and preview['bytes'] > 0
                f.service.clear(review_token=preview['review_token'])
            else:
                replacement = OfflineReading(store=OfflineStore(tmp_path, messaging_key=f.key), sources=f.sources)
                replacement.recover()
                assert row(f)['state'] == 'queued'
        finally:
            release.set()
        assert work.result(timeout=10) is False
    assert row(f)['current'] is None
    if action == 'clear':
        assert row(f)['state'] == 'cleared' and f.service.store.used() == 0
    else:
        if action == 'cancel':
            assert row(f)['state'] == 'cancelled'
            f.service.retry(f.item['item_id'])
        else:
            f.service = replacement
        assert f.service.run_once()
        assert f.service.read(f.item['item_id'])['text'] == BODY
        assert f.network['calls'].count(URL) == 1  # Durable body checkpoint, not byte-range claims.


def test_clear_requires_current_review_and_preserves_bookmark_progress_and_independent_copy(tmp_path):
    f = fixture(tmp_path, images=False)
    own = f.imports.save(BODY.encode(), filename='owned.txt', mime='text/plain', source={'title': 'Owned'})
    original_import = f.imports.body(own['import_id'])
    download(f)
    before = f.consumption.path.read_bytes()
    preview = f.service.clear_preview(f.item['item_id'])
    f.service.request(f.item['item_id'], update=True)
    with pytest.raises(OfflineError, match='offline_clear_review_changed'):
        f.service.clear(item_id=f.item['item_id'], review_token=preview['review_token'])
    preview = f.service.clear_preview(f.item['item_id'])
    result = f.service.clear(item_id=f.item['item_id'], review_token=preview['review_token'])
    assert result['copies'] == 1 and result['freed_bytes'] == preview['bytes'] > 0
    assert f.service.store.used() == 0 and row(f)['state'] == 'cleared'
    assert f.consumption.path.read_bytes() == before
    assert f.imports.body(own['import_id']) == original_import
    with pytest.raises(OfflineError, match='offline_not_downloaded'):
        f.service.read(f.item['item_id'])


def test_unbookmark_does_not_remove_download_and_owned_import_never_fetches_remote(tmp_path):
    f = fixture(tmp_path)
    saved = f.imports.save(BODY.encode(), filename='friend.txt', mime='text/plain', source={'title': 'From friend'})
    item_id = 'import:' + saved['import_id']
    f.item = {'item_id': item_id, 'title': 'Independent copy', 'content_kind': 'article',
              'link': 'rynmesh://content/' + quote(item_id, safe='')}
    f.consumption.record(f.item, 'bookmark')
    assert download(f)['source_mode'] == 'independent_local_copy'
    f.consumption.record(f.item, 'unbookmark')
    f.service.request(item_id, update=True)
    assert f.service.run_once()
    assert f.service.read(item_id)['text'] == BODY
    assert f.network['calls'] == []


@pytest.mark.parametrize(('limit', 'code'), [('MAX_ITEM', 'offline_item_limit'), ('MAX_TOTAL', 'offline_storage_limit'), ('disk', 'offline_disk_full')])
def test_storage_limits_never_evict_old_copy(tmp_path, monkeypatch, limit, code):
    f = fixture(tmp_path, images=False)
    old = download(f)
    f.service.request(f.item['item_id'], update=True)
    if limit == 'disk':
        monkeypatch.setattr(storage.shutil, 'disk_usage', lambda path: SimpleNamespace(free=0))
    elif limit == 'MAX_ITEM':
        monkeypatch.setattr(storage, limit, row(f)['current']['size_bytes'] + 512)
        f.network['body'] = BODY + ' larger new version' * 200
    else:
        monkeypatch.setattr(storage, limit, 1)
    with pytest.raises(OfflineError, match=code):
        f.service.run_once()
    assert row(f)['state'] == 'failed' and row(f)['error_code'] == code
    assert f.service.read(f.item['item_id']) == old


def test_corrupt_checkpoint_rebuilds_but_corrupt_committed_copy_and_future_version_refuse(tmp_path):
    f = fixture(tmp_path, images=False)
    f.service.request(f.item['item_id'])
    job = f.service.store.read()['records'][key_for(f.item['item_id'])]['job']['id']
    path = f.service.store._path(job)
    atomic_write_json(path, {'version': VERSION, 'nonce': '', 'ciphertext': 'corrupted'})
    assert f.service.run_once()
    envelope, bundle = f.service.store._read(path, storage.MAX_ITEM)
    bundle['text'] += ' tampered'
    atomic_write_json(path, f.service.store._encode(bundle, envelope))
    with pytest.raises(OfflineError, match='offline_verification_failed'):
        f.service.read(f.item['item_id'])
    f.service.request(f.item['item_id'], update=True)
    new_job = f.service.store.read()['records'][key_for(f.item['item_id'])]['job']['id']
    future_path = f.service.store._path(new_job)
    future = {'version': 'ryn.offline-reading.v999', 'future': True}
    atomic_write_json(future_path, future)
    with pytest.raises(OfflineError, match='offline_version_unsupported'):
        f.service.run_once()
    assert row(f)['error_code'] == 'offline_version_unsupported'
    assert read_json(future_path) == future


def test_metadata_unknown_fields_preserved_and_future_metadata_not_overwritten(tmp_path):
    f = fixture(tmp_path, images=False)
    download(f)
    envelope, state = f.service.store._state()
    envelope['future_envelope'] = {'retain': True}
    state['future_root'] = [1, 2]
    state['records'][key_for(f.item['item_id'])]['future_record'] = 'keep'
    atomic_write_json(f.service.store.path, f.service.store._encode(state, envelope))
    f.service.request(f.item['item_id'], update=True)
    envelope, state = f.service.store._state()
    assert envelope['future_envelope'] == {'retain': True}
    assert state['future_root'] == [1, 2]
    assert state['records'][key_for(f.item['item_id'])]['future_record'] == 'keep'
    atomic_write_json(f.service.store.path, {'version': 'ryn.offline-reading.v999'})
    before = f.service.store.path.read_bytes()
    with pytest.raises(OfflineError, match='offline_version_unsupported'):
        f.service.recover()
    assert f.service.store.path.read_bytes() == before


@pytest.mark.parametrize(('body', 'mime', 'expected'), [
    (b'<html><h1>Sign in</h1><form>Password</form></html>', 'text/html', 'offline_no_readable_body'),
    (b'', 'text/plain', 'offline_no_readable_body'),
    (b'%PDF-unsupported-for-web-fetch', 'application/pdf', 'offline_source_format_unsupported'),
])
def test_unsupported_or_empty_page_does_not_become_success(tmp_path, body, mime, expected):
    f = fixture(tmp_path)
    f.sources.fetch = lambda url, **kwargs: {'data': body, 'mime': mime, 'url': url}
    f.service.request(f.item['item_id'])
    with pytest.raises(OfflineError, match=expected):
        f.service.run_once()
    assert row(f)['error_code'] == expected and row(f)['current'] is None
    assert f.service.store.used() == 0


def test_password_gate_does_not_replace_a_readable_offline_copy(tmp_path):
    f = fixture(tmp_path, images=False)
    original = download(f)
    gate = b'''<html><title>Sign in</title><body><main>
        <p>Please sign in with your account to continue reading this article.</p>
        <form action="/session"><input type="password" name="password"></form>
        </main></body></html>'''
    f.sources.fetch = lambda url, **kwargs: {'data': gate, 'mime': 'text/html', 'url': url}
    f.service.request(f.item['item_id'], update=True)
    with pytest.raises(OfflineError, match='offline_source_access_required'):
        f.service.run_once()
    assert row(f)['error_code'] == 'offline_source_access_required'
    assert f.service.read(f.item['item_id']) == original


def test_article_with_separate_sign_in_form_can_be_saved(tmp_path):
    f = fixture(tmp_path, images=False)
    page = f'''<html><body><aside><form><input type="password"></form></aside>
        <article><p>{BODY}</p></article></body></html>'''.encode()
    f.sources.fetch = lambda url, **kwargs: {'data': page, 'mime': 'text/html', 'url': url}
    assert download(f)['text'] == BODY


def test_owner_routes_limits_dynamic_reinstallation_and_version_bound_images(tmp_path):
    f = fixture(tmp_path)
    app, workers = FastAPI(), BackgroundWorkerRegistry()
    def guard(request):
        if request.headers.get('X-Test-Owner') != 'yes':
            raise HTTPException(403, detail='owner_required')
    def install(home):
        return install_offline_reading(app, home=home, messaging_key=f.key, consumption=lambda: f.consumption,
            imports=lambda: f.imports, native=lambda: None, local_control=guard, workers=workers, fetch=f.sources.fetch)
    f.service = install(tmp_path)
    client = TestClient(app)
    base, headers = '/api/local/offline-reading', {'X-Test-Owner': 'yes'}
    assert client.get(base).status_code == 403
    assert client.post(base + '/resolve', json={'item_id': f.item['item_id']}).status_code == 403
    assert client.post(base + '/resolve', json={'item_id': f.item['item_id']}, headers=headers).json() is None
    assert client.post(base + '/download', json={'item_id': f.item['item_id']}).status_code == 403
    assert client.post(base + '/download', content='x' * 8193, headers=headers).status_code == 413
    assert client.post(base + '/download', json={'item_id': f.item['item_id']}, headers=headers).status_code == 200
    assert f.service.run_once()
    record = row(f)
    resolved = client.post(base + '/resolve', json={'item_id': f.item['item_id']}, headers=headers).json()
    assert resolved['key'] == record['key'] and resolved['body']['text'] == BODY
    image_path = base + '/copies/' + record['key'] + '/' + record['current']['job_id'] + '/images/0'
    assert client.get(image_path).status_code == 403
    response = client.get(image_path, headers=headers)
    assert response.content == png() and response.headers['content-type'] == 'image/png'
    assert response.headers['cache-control'] == 'no-store'
    assert client.post(base + '/body', json={'item_id': f.item['item_id']}, headers=headers).json()['text'] == BODY
    install(tmp_path / 'replacement')
    assert client.get(base, headers=headers).json()['records'] == []
    assert client.get(image_path, headers=headers).status_code == 404
    assert len([route for route in app.routes if route.name == 'offline_reading_status']) == 1
    assert len(workers.specs()) == 1 and workers.specs()[0].run_once() is False


def test_reader_selects_article_images_and_omits_tiny_chrome_and_active_resources():
    article = extract_readable(f'''<html><body><nav><img src="/navigation.png"></nav>
        <article><p>{BODY}</p><img src="/picture.png" alt="Illustration">
        <img src="/tracking.png" width="1"><img src="data:image/png;base64,AAAA">
        <script><img src="/script.png"></script><iframe src="/frame"></iframe>
        <img src="/advert.png" class="advert"></article></body></html>'''.encode(), url=URL)
    assert article['images'] == [{'url': 'https://example.com/picture.png', 'alt': 'Illustration'}]
    assert article['blocks'] == [{'tag': 'p', 'text': BODY}]


@pytest.fixture
def http_origin():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append({'path': self.path, 'headers': dict(self.headers)})
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', '/body')
                self.end_headers()
                return
            if self.path == '/private-redirect':
                self.send_response(302)
                self.send_header('Location', 'http://10.0.0.1/private')
                self.end_headers()
                return
            if self.path == '/loop':
                self.send_response(302)
                self.send_header('Location', '/loop')
                self.end_headers()
                return
            raw = png() if self.path == '/image' else BODY.encode()
            self.send_response(403 if self.path == '/login' else 200)
            self.send_header('Content-Type', 'image/png' if self.path == '/image' else 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(raw) + (100 if self.path == '/incomplete' else 0)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_real_http_fetch_bounds_redirect_authentication_and_complete_transfer(http_origin):
    origin, requests = http_origin
    with pytest.raises(OfflineError, match='offline_source_address_blocked'):
        fetch_resource(origin + '/body', max_bytes=4096)
    assert requests == []
    result = fetch_resource(origin + '/redirect', max_bytes=4096, allow_loopback=True)
    assert result['data'] == BODY.encode() and result['url'] == origin + '/body'
    assert result['mime'] == 'text/plain'
    assert all(not {'Authorization', 'Cookie', 'Referer'} & request['headers'].keys() for request in requests)
    assert image_mime(fetch_resource(origin + '/image', max_bytes=4096, allow_loopback=True)['data']) == 'image/png'
    for path, code in [('/private-redirect', 'offline_source_address_blocked'), ('/login', 'offline_source_access_required'),
                       ('/incomplete', 'offline_transfer_incomplete'), ('/loop', 'offline_redirect_limit')]:
        with pytest.raises(OfflineError, match=code):
            fetch_resource(origin + path, max_bytes=4096, allow_loopback=True)
    with pytest.raises(OfflineError, match='offline_resource_too_large'):
        fetch_resource(origin + '/body', max_bytes=5, allow_loopback=True)


@pytest.mark.parametrize('raw', [b'<svg><script>active</script></svg>', b'<html>pretend image</html>', b'RIFF0000WEBPbroken'])
def test_active_or_invalid_images_refuse(raw):
    with pytest.raises(OfflineError):
        image_mime(raw)


@pytest.mark.parametrize('action', ['timeout', 'cancel'])
def test_slow_http_headers_are_bounded_and_child_is_reaped(action):
    connected, release = threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            connected.set()
            # Keep a socket active without ever completing the header block.
            self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: ')
            try:
                while not release.wait(0.05):
                    self.wfile.write(b'a')
                    self.wfile.flush()
            except OSError:
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    children = {process.pid for process in multiprocessing.active_children()}
    def check():
        if action == 'cancel' and connected.is_set():
            raise OfflineError('offline_cancelled_or_superseded')
    started = time.monotonic()
    try:
        with pytest.raises(OfflineError, match='offline_source_timeout' if action == 'timeout' else 'offline_cancelled_or_superseded'):
            fetch_resource(f'http://127.0.0.1:{server.server_port}/slow', max_bytes=4096,
                           allow_loopback=True, timeout=2, check=check)
        assert connected.is_set()
        assert time.monotonic() - started < 5
        assert {process.pid for process in multiprocessing.active_children()} == children
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)


def _checkpoint_child(home, ready):
    f = fixture(home)
    original = f.sources.fetch
    def stalled(url, **kwargs):
        if url.endswith('picture.png'):
            ready.set()
            threading.Event().wait(30)
        return original(url, **kwargs)
    f.sources.fetch = stalled
    f.service.run_once()


def test_terminated_process_recovers_durable_checkpoint_without_refetching_body(tmp_path):
    f = fixture(tmp_path)
    f.service.request(f.item['item_id'])
    context = multiprocessing.get_context('spawn')
    ready = context.Event()
    process = context.Process(target=_checkpoint_child, args=(tmp_path, ready))
    process.start()
    try:
        assert ready.wait(15)
        assert row(f)['state'] == 'downloading'
        assert row(f)['verified_bytes'] == len(BODY.encode())
        with pytest.raises(OfflineError, match='offline_not_downloaded'):
            f.service.read(f.item['item_id'])
    finally:
        process.terminate()
        process.join(5)
        assert not process.is_alive()
        process.close()
    f.service = OfflineReading(store=OfflineStore(tmp_path, messaging_key=f.key), sources=f.sources)
    f.service.recover()
    f.network['fail'].add(URL)
    assert f.service.run_once()
    assert f.service.read(f.item['item_id'])['text'] == BODY
    assert f.network['calls'] == ['https://example.com/picture.png']


def test_future_bundle_cannot_lose_its_metadata_on_update_or_clear(tmp_path):
    f = fixture(tmp_path, images=False)
    saved = download(f)
    path = f.service.store._path(saved['job_id'])
    atomic_write_json(path, {'version': 'ryn.offline-reading.v999', 'future': True})
    before = f.service.store.path.read_bytes()
    preview = f.service.clear_preview()
    with pytest.raises(OfflineError, match='offline_version_unsupported'):
        f.service.clear(review_token=preview['review_token'])
    with pytest.raises(OfflineError, match='offline_version_unsupported'):
        f.service.request(f.item['item_id'], update=True)
    assert f.service.store.path.read_bytes() == before
    assert read_json(path) == {'version': 'ryn.offline-reading.v999', 'future': True}
    orphan = f.service.store._path('f' * 32)
    atomic_write_json(orphan, {'version': 'ryn.offline-reading.v999'})
    f.service.store.gc()
    assert orphan.exists() and path.exists()


def test_cleared_record_does_not_exhaust_download_capacity(tmp_path, monkeypatch):
    f = fixture(tmp_path, images=False)
    monkeypatch.setattr(operations, 'MAX_RECORDS', 1)
    download(f)
    f.service.clear(review_token=f.service.clear_preview()['review_token'])
    f.item = {**f.item, 'item_id': 'article:another'}
    f.consumption.record(f.item, 'bookmark')
    assert f.service.request(f.item['item_id'])['state'] == 'queued'
    assert len(f.service.status()['records']) == 1
    assert f.service.run_once()
