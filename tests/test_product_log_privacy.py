"""Private inputs must not escape through shared diagnostics and runtime output."""
import json
import logging
import sys
from pathlib import Path

from test_recommendation_service import _client

from rynmesh.llm_package import runtime_native
from rynmesh.llm_package.errors import LifecycleError
from rynmesh.registry import RegistryError
from rynmesh.registry_resilience import FallbackRegistryChain, bootstrap_peers_from_path


def test_recommendation_audit_records_counts_without_private_preferences(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    topic = 'privatepreferencecanary'
    response = client.patch('/api/local/recommendations/profile', json={
        'direction': topic, 'topics': [topic], 'platforms': ['github'],
    })
    assert response.status_code == 200
    response = client.post('/api/local/digest/steer', json={'text': 'more ' + topic})
    assert response.status_code == 200 and response.json()['interests']
    exported = client.get('/api/local/privacy/export').json()
    assert topic in json.dumps(exported['recommendation_profile'])
    assert topic not in json.dumps(exported['assistant_audit'])
    assert exported['assistant_audit'][0]['details']['interest_count'] == 1


def test_registry_fallback_and_bootstrap_logs_exclude_error_bodies_and_source_secrets(tmp_path, caplog):
    private = 'PRIVATE_REGISTRY_DETAIL_CANARY'

    class Failing:
        def list_peers(self, **kwargs):
            raise RegistryError(private)

        def deposit_mailbox(self, payload):
            raise RegistryError(private)

    class Working:
        def list_peers(self, **kwargs):
            return []

        def deposit_mailbox(self, payload):
            return {'accepted': True}

    caplog.set_level(logging.INFO, logger='rynmesh.registry_resilience')
    chain = FallbackRegistryChain([Failing(), Working()])
    assert chain.list_peers(network_id='test') == []
    assert chain.deposit_mailbox(None) == {'accepted': True}
    source = tmp_path / (private + '.json')
    source.write_text('[{}]', encoding='utf-8')
    assert bootstrap_peers_from_path(source) == []
    assert len(caplog.records) >= 3
    assert private not in caplog.text
    assert str(source) not in caplog.text


def test_native_child_error_output_cannot_copy_private_payloads_to_runtime_log(tmp_path):
    private = 'PRIVATE_NATIVE_PROMPT_AND_KEY_CANARY'
    command = [sys.executable, '-c',
               f'import sys; print({private!r}, flush=True); print({private!r}, file=sys.stderr, flush=True); sys.exit(1)']
    try:
        process = runtime_native._spawn(Path(sys.executable), command, tmp_path, 'privacy-check', 0)
    except LifecycleError as exc:
        assert private not in str(exc)
    else:
        process.wait(timeout=5)
    log = (tmp_path / 'runtime' / 'privacy-check.log').read_text(encoding='utf-8')
    assert private not in log
    assert 'runtime_process' in log
