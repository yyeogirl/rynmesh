"""Pairing through installed owner/peer HTTP handlers and the shared worker."""
import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.device_sync.routes import install_device_sync

PREFIX = '/api/local/device-sync'
OWNER = {'x-test-owner': 'yes'}


class Node:
    def __init__(self, home, endpoint='', post=None, *, transfer=False):
        self.app, self.workers = FastAPI(), BackgroundWorkerRegistry()
        self.store = SimpleNamespace(home=home, private_key_bytes=Ed25519PrivateKey.generate().private_bytes_raw(), node_name=home.name)
        self.key, self.endpoint, self.post = X25519PrivateKey.generate(), endpoint, post
        self.transfer_enabled = transfer
        if transfer:
            from rynmesh.ask_ryn.store import ConversationStore
            from rynmesh.services.consumption import ConsumptionStore
            self.reader = ConsumptionStore(home / 'consumption.json')
            self.history = ConversationStore(home / 'ask', self.key)
        self.install()
        self.client = TestClient(self.app)

    @staticmethod
    def owner(request):
        if request.headers.get('x-test-owner') != 'yes':
            raise HTTPException(403, detail='owner_required')

    def install(self):
        return install_device_sync(self.app, store=self.store, home=self.store.home / 'wrong-home',
            workers=self.workers, messaging_key=self.key, endpoint=self.endpoint, allow_loopback=True,
            local_control=self.owner, post_json=self.post,
            reading=(lambda: self.reader) if self.transfer_enabled else None,
            conversations=(lambda: self.history) if self.transfer_enabled else None)

    @property
    def service(self):
        return self.app.state.device_sync.service

    def tick(self):
        return self.workers.specs()[0].run_once()

    def request(self, method, path='', **kwargs):
        result = self.client.request(method, PREFIX + path, headers=OWNER, **kwargs)
        assert result.status_code == 200, result.text
        return result.json()


def test_owner_handlers_and_worker_complete_pair_pause_remove(tmp_path):
    nodes = {}

    def post(endpoint, path, wire):
        response = nodes[endpoint].client.post(path, json=wire)
        assert response.status_code == 200, response.text
        return response.json()

    a = Node(tmp_path / 'A', 'http://127.0.0.1:18901', post)
    b = Node(tmp_path / 'B', 'http://127.0.0.1:18902', post)
    nodes.update({a.endpoint: a, b.endpoint: b})
    uri = a.request('POST', '/invites', json={'scopes': ['bookmarks']})['uri']
    preview = b.request('POST', '/invites/inspect', json={'uri': uri})
    assert preview['device']['name'] == 'A'
    joining = b.request('POST', '/join', json={'uri': uri, 'scopes': ['bookmarks']})
    pair_id = joining['id']
    assert a.request('GET')['devices'] == []  # Local consent committed; IO is deferred.
    b.tick()
    pending = a.request('GET')['devices'][0]
    assert pending['status'] == 'awaiting_owner'
    a.request('POST', f'/devices/{pair_id}/approve', json={'review_token': pair_id, 'scopes': ['bookmarks']})
    b.tick()
    assert a.request('GET')['devices'][0]['status'] == b.request('GET')['devices'][0]['status'] == 'active'
    assert a.request('GET')['data_transfer_available'] is False  # Pairing never implies data convergence.
    a.request('PUT', f'/devices/{pair_id}/policy', json={'expected_revision': 1, 'scopes': ['bookmarks'], 'paused': True})
    a.tick()
    assert b.request('GET')['devices'][0]['remote_paused']
    removed = a.request('POST', f'/devices/{pair_id}/remove', json={'expected_revision': 2})
    assert removed['removal_pending']
    a.tick()
    assert b.request('GET')['devices'][0]['status'] == 'revoked'
    assert not a.request('GET')['devices'][0]['removal_pending']
    assert not (a.store.home / 'wrong-home').exists()


def test_reinstallation_replaces_guard_service_and_worker_without_duplicate_routes(tmp_path):
    node = Node(tmp_path / 'A', 'http://127.0.0.1:18901')
    first, count = node.service, len(node.app.routes)
    second = node.install()
    assert first is not second
    assert len(node.app.routes) == count
    assert [spec.name for spec in node.workers.specs()] == ['device-sync.pairing']
    second.readiness = lambda: {'pairing_available': False, 'reason': 'new-state'}
    assert node.request('GET')['reason'] == 'new-state'
    node.app.state.device_sync.local_control = lambda _: (_ for _ in ()).throw(HTTPException(403, 'replaced-guard'))
    assert node.client.get(PREFIX, headers=OWNER).json()['detail'] == 'replaced-guard'


def test_missing_endpoint_does_not_block_node_or_local_revocation(tmp_path):
    node = Node(tmp_path / 'A')
    assert node.request('GET')['reason'] == 'sync_endpoint_unavailable'
    assert node.client.post(PREFIX + '/invites', headers=OWNER, json={'scopes': []}).status_code == 503
    assert not node.service.store.path.exists()
    node.tick()


@pytest.mark.parametrize('method,path', [
    ('GET', ''), ('POST', '/invites'), ('POST', '/invites/inspect'), ('DELETE', '/invites/' + 'a' * 32),
    ('POST', '/join'), ('POST', '/devices/' + 'a' * 64 + '/approve'),
    ('PUT', '/devices/' + 'a' * 64 + '/policy'), ('POST', '/devices/' + 'a' * 64 + '/remove'),
    ('POST', '/devices/' + 'a' * 64 + '/retry'),
    ('GET', '/reading/conflicts'), ('POST', '/reading/resolve'),
])
def test_every_owner_route_checks_guard_before_request_body(tmp_path, method, path):
    node = Node(tmp_path / 'A')
    result = node.client.request(method, PREFIX + path, content=b'x' * 65537)
    assert result.status_code == 403
    assert not node.service.store.path.exists()


def test_peer_routes_bound_input_rate_and_error_details(tmp_path):
    node = Node(tmp_path / 'A')
    path = '/api/peer/device-sync/join'
    assert node.client.post(path, content=b'x' * 65537).status_code == 413
    assert node.client.post(path, json=[]).status_code == 400
    assert node.client.post(path, json={'secret': 'must-not-leak'}).json() == {'detail': 'sync_request_rejected'}
    assert node.client.post('/api/peer/device-sync/unexpected', json={}).status_code == 404
    for _ in range(57):
        assert node.client.post(path, json={}).status_code == 403
    result = node.client.post(path, json={})
    assert result.status_code == 429 and result.headers['retry-after'] == '60'
    assert len(node.app.state.device_sync.attempts) == 1


def test_slow_pairing_io_does_not_block_http_event_loop(tmp_path):
    node = Node(tmp_path / 'A')
    entered, release = threading.Event(), threading.Event()

    def slow(_wire):
        entered.set()
        assert release.wait(5)
        return {'done': True}

    node.service.receive_join = slow

    @node.app.get('/health-test')
    async def health():
        return {'ok': True}

    async def scenario():
        transport = httpx.ASGITransport(app=node.app)
        async with httpx.AsyncClient(transport=transport, base_url='http://node') as client:
            pending = asyncio.create_task(client.post('/api/peer/device-sync/join', json={}))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                response = await asyncio.wait_for(client.get('/health-test'), timeout=1)
                assert response.json() == {'ok': True}
            finally:
                release.set()
            assert (await pending).status_code == 200

    asyncio.run(scenario())


def test_worker_sanitizes_network_error(tmp_path):
    def offline(*_):
        raise OSError('private invite and address')
    a = Node(tmp_path / 'A', 'http://127.0.0.1:18901')
    b = Node(tmp_path / 'B', 'http://127.0.0.1:18902', offline)
    uri = a.request('POST', '/invites', json={'scopes': []})['uri']
    b.request('POST', '/join', json={'uri': uri, 'scopes': []})
    with pytest.raises(RuntimeError, match='^sync_pairing_retry_unavailable$'):
        b.tick()
    assert b.request('GET')['devices'][0]['status'] == 'awaiting_inviter'


def test_installed_worker_moves_real_sources_through_encrypted_http_batches(tmp_path):
    from test_ask_history import sample
    from test_device_sync_reading import ITEM
    nodes, paths = {}, []

    def post(endpoint, path, wire):
        paths.append(path)
        result = nodes[endpoint].client.post(path, json=wire)
        assert result.status_code == 200, result.text
        return result.json()

    a = Node(tmp_path / 'A', 'http://127.0.0.1:18901', post, transfer=True)
    b = Node(tmp_path / 'B', 'http://127.0.0.1:18902', post, transfer=True)
    nodes.update({a.endpoint: a, b.endpoint: b})
    scopes = ['bookmarks', 'reading', 'conversations']
    a.reader.record(ITEM, 'bookmark')
    a.reader.record(ITEM, 'progress', progress=.7)
    conversation = sample()
    conversation['messages'][0]['content'] = 'Large history fragment. ' * 6000
    a.history.save(conversation, expected_revision=0)
    uri = a.request('POST', '/invites', json={'scopes': scopes})['uri']
    pair_id = b.request('POST', '/join', json={'uri': uri, 'scopes': scopes})['id']
    b.tick()
    assert not b.reader.path.exists() and not b.history.path.exists()
    a.request('POST', f'/devices/{pair_id}/approve', json={'review_token': pair_id, 'scopes': scopes})
    b.tick()
    for _ in range(3):
        a.tick()
    status = a.request('GET')
    assert status['data_transfer_available']
    assert status['devices'][0]['sync']['state'] == 'confirmed'
    assert b.reader.sync_read('reading', ITEM['item_id'])['value']['progress'] == .7
    assert b.history.get(conversation['id'])['messages'][0]['content'] == conversation['messages'][0]['content']
    assert paths.count('/api/peer/device-sync/batch') == 3
    a.reader.record(ITEM, 'unbookmark')
    assert a.request('GET')['devices'][0]['sync']['pending'] == 1
    assert b.client.post('/api/peer/device-sync/batch', json={}).status_code == 403


def test_owner_reading_choice_uses_stored_candidate_and_rejects_stale_revision(tmp_path):
    from test_device_sync_reading import ITEM

    from rynmesh.device_sync import records
    node = Node(tmp_path / 'A', transfer=True)
    node.reader.enable_sync(node.service.store.actor, ['reading'])
    for actor, position in (('b' * 64, .2), ('c' * 64, .8)):
        record = records.write('reading', ITEM['item_id'], records.empty(), actor,
            {'item': ITEM, 'progress': position, 'completed': False, 'content_version': ''})
        node.reader.sync_receive([{'scope': 'reading', 'id': ITEM['item_id'], 'record': record}], scopes=['reading'])
    result = node.request('GET', '/reading/conflicts')
    assert result['local_actor'] == node.service.store.actor
    issue = result['conflicts'][0]
    choice = next(row for row in issue['candidates'] if row['value']['progress'] == .2)
    payload = {'id': issue['id'], 'scope': 'reading', 'expected_revision': issue['revision'],
               'choice_id': choice['choice_id'], 'progress': .99}
    assert node.request('POST', '/reading/resolve', json=payload)['value']['progress'] == .2
    assert node.request('POST', '/reading/resolve', json=payload)['value']['progress'] == .2
    node.reader.record(ITEM, 'progress', progress=.4)
    response = node.client.post(PREFIX + '/reading/resolve', headers=OWNER, json=payload)
    assert response.status_code == 409 and response.json()['detail'] == 'sync_revision_conflict'
    assert node.request('GET', '/reading/conflicts')['conflicts'] == []
