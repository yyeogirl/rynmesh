"""Runtime HTTP failures must survive encryption and produce actionable codes."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rynmesh.llm_package.adapters import AdapterError, OpenAICompatibleAdapter
from rynmesh.llm_package.manifest import LLMPackageManifest
from rynmesh.llm_package.routes import ProviderService, _expires
from rynmesh.llm_package.task_balance import TaskBalanceLedger
from rynmesh.llm_package.task_protocol import TaskOrderStore, open_task, seal_task
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


@pytest.fixture
def failing_runtime():
    state = {'body': {'detail': 'runtime_busy', 'debug': 'PRIVATE_ERROR_CANARY'}, 'delay': 0, 'calls': 0, 'status': 503}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_json(200, {'data': [{'id': 'error-test-model'}]})

        def do_POST(self):
            state['calls'] += 1
            self.rfile.read(int(self.headers.get('content-length', 0)))
            time.sleep(state['delay'])
            self.send_json(state['status'], state['body'])

        def send_json(self, status, value):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except OSError:
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize('detail,expected', [
    ('runtime_busy', 'runtime_busy'), ('model_not_ready', 'model_not_ready'),
    ('runtime_unavailable', 'runtime_unavailable'), ('model_not_found', 'model_not_found'),
    ('runtime_busy PRIVATE_ERROR_CANARY', 'inference_failed'),
])
def test_runtime_http_failure_keeps_safe_code_through_provider(tmp_path, failing_runtime, detail, expected):
    state, url = failing_runtime
    state['body']['detail'] = detail
    if detail == 'model_not_found':
        state['status'] = 404
    adapter = OpenAICompatibleAdapter(base_url=url, model='error-test-model')
    store = RynmeshStore(home=tmp_path / 'node', network_dir=tmp_path / 'network')
    key = peer_box.load_or_create_messaging_key(store.home / 'messaging.x25519')
    orders = TaskOrderStore(tmp_path / 'orders')
    provider = ProviderService(manifest=LLMPackageManifest(package_id='error-test', mode='openai_compatible',
        public_model_alias='Error test', base_url=url), adapter=adapter, store=store, task_store=orders,
        balance=TaskBalanceLedger(tmp_path / 'balance.json'), messaging_key=key)
    request = seal_task(body={'task_id': 'error-task', 'service_id': 'error-test', 'prompt': 'PRIVATE_INPUT_CANARY',
        'max_tokens': 8, 'max_amount': 0, 'reply_messaging_pub': peer_box.public_key_b64(key)},
        task_id='error-task', kind='llm_request', sender_peer_id=store.peer_id, recipient_peer_id=store.peer_id,
        sender_signing_key=store.private_key_bytes, recipient_messaging_pub=peer_box.public_key_b64(key), expires_at=_expires(60))
    encrypted = provider.handle(request.to_dict())
    _, response = open_task(encrypted, recipient_peer_id=store.peer_id, recipient_messaging_key=key, expected_kind='llm_response')
    assert response['error_code'] == expected
    assert response['state'] == 'failed'
    assert 'PRIVATE_' not in json.dumps(response)
    assert 'PRIVATE_' not in json.dumps(orders.list())
    assert provider.public_status()['capacity']['running'] == 0
    # Re-delivering the same signed task returns its retained failure, even
    # after runtime recovery. A distinct, explicit task can use the freed slot.
    state['status'] = 200
    state['body'] = {'choices': [{'message': {'content': 'Recovered'}}]}
    assert provider.handle(request.to_dict()) == encrypted
    assert state['calls'] == 1
    retry = seal_task(body={'task_id': 'recovery-task', 'service_id': 'error-test', 'prompt': 'test',
        'max_tokens': 8, 'max_amount': 0, 'reply_messaging_pub': peer_box.public_key_b64(key)},
        task_id='recovery-task', kind='llm_request', sender_peer_id=store.peer_id, recipient_peer_id=store.peer_id,
        sender_signing_key=store.private_key_bytes, recipient_messaging_pub=peer_box.public_key_b64(key), expires_at=_expires(60))
    _, recovered = open_task(provider.handle(retry.to_dict()), recipient_peer_id=store.peer_id,
        recipient_messaging_key=key, expected_kind='llm_response')
    assert recovered['state'] == 'succeeded'
    assert recovered['output'] == 'Recovered'
    assert state['calls'] == 2


@pytest.mark.parametrize('body', [[], None, {'detail': {'code': 'runtime_busy'}}, {'detail': 'x' * 17000}])
def test_unknown_runtime_error_shape_is_not_treated_as_busy(failing_runtime, body):
    state, url = failing_runtime
    state['body'] = body
    with pytest.raises(AdapterError) as failure:
        OpenAICompatibleAdapter(base_url=url, model='error-test-model').infer(prompt='test', max_tokens=1, task_id='t', timeout_s=2)
    assert failure.value.code == 'inference_failed'
    assert len(str(failure.value)) < 150


def test_real_socket_timeout_has_a_stable_code(failing_runtime):
    state, url = failing_runtime
    state['delay'] = 0.2
    with pytest.raises(AdapterError) as failure:
        OpenAICompatibleAdapter(base_url=url, model='error-test-model').infer(prompt='test', max_tokens=1, task_id='t', timeout_s=0.03)
    assert failure.value.code == 'inference_timeout'


def test_closed_runtime_port_is_unreachable_not_busy():
    server = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
    url = f'http://127.0.0.1:{server.server_port}'
    server.server_close()
    with pytest.raises(AdapterError) as failure:
        OpenAICompatibleAdapter(base_url=url, model='error-test-model').infer(
            prompt='test', max_tokens=1, task_id='t', timeout_s=10)
    assert failure.value.code == 'runtime_connection_failed'
