"""One bounded Windows DLL-loss/recovery check on the existing isolated model."""
import ctypes
import hashlib
import json
from pathlib import Path

from accept_local_search import configure
from fastapi.testclient import TestClient

HOME = Path('D:/code/rynmesh-local-ai-acceptance-c124bb62d6654226903a51badd14d528')
OUTPUT = Path(__file__).resolve().parents[1] / 'docs/acceptance/local-ai-development/native-dependency-20260914.json'


def main():
    configure(HOME, 18846)
    import os
    os.environ['RYNMESH_LLM_HOME'] = str(HOME / 'llm')
    from rynmesh.llm_package import runtime_native
    from rynmesh.llm_package.manifest import load_manifest
    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    root = (HOME / 'llm').resolve()
    manifest = load_manifest(root / 'packages/local-small/manifest.json')
    assert manifest.runtime == runtime_native.RUNTIME_ID and not runtime_native.state(manifest)['running']
    dll = (root / 'runtime/llama-b10774/ggml-base.dll').resolve()
    held = dll.with_suffix('.dll.acceptance-held')
    assert dll.is_relative_to(root) and held.is_relative_to(root) and not held.exists()
    before = hashlib.sha256(dll.read_bytes()).hexdigest()
    # Suppress the Windows loader dialog in this acceptance process/its children.
    previous_mode = ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    client = TestClient(create_app(RynmeshStore(home=HOME, network_dir=HOME / 'network', node_name='Dependency acceptance')))
    result = {'scope': 'Windows CPU; real pinned llama-server and Qwen model; actual node routes via TestClient, no public network', 'dll': dll.name}
    try:
        dll.rename(held)
        failed = client.post('/api/local/llm/service/actions/start', json={})
        assert failed.status_code == 409, failed.status_code
        detail = failed.json()['detail']
        assert 'local inference runtime dependency is missing' in detail
        status = client.get('/api/local/llm/service/status').json()
        assert not status['ready'] and not status['online'] and not status['publication_enabled']
        result['missing_dependency'] = {'http_status': failed.status_code, 'error': detail,
            'ready': status['ready'], 'online': status['online'], 'publication_enabled': status['publication_enabled'],
            'runtime_log': (root / 'runtime/local-small.log').read_text()}
        # Exercise the product repair action, not manual restoration.
        restored = client.post('/api/local/llm/service/actions/update', json={})
        assert restored.status_code == 200, restored.text
        checked = client.post('/api/local/llm/service/actions/self-test', json={})
        assert checked.status_code == 200, checked.text
        status = client.get('/api/local/llm/service/status').json()
        assert status['ready'] and not status['publication_enabled']
        assert hashlib.sha256(dll.read_bytes()).hexdigest() == before
        result['restored'] = {'http_status': restored.status_code, 'ready': status['ready'],
            'publication_enabled': status['publication_enabled'], 'real_inference_self_test': checked.json()['result'],
            'dll_bytes_unchanged': True, 'runtime_log': (root / 'runtime/local-small.log').read_text()}
    finally:
        if held.exists():
            if dll.exists():
                assert hashlib.sha256(dll.read_bytes()).hexdigest() == before
                held.unlink()
            else:
                held.rename(dll)
        client.post('/api/local/llm/service/actions/stop', json={}).raise_for_status()
        client.close()
        ctypes.windll.kernel32.SetErrorMode(previous_mode)
    result['final_runtime_running'] = runtime_native.state(load_manifest(root / 'packages/local-small/manifest.json'))['running']
    assert result['final_runtime_running'] is False
    OUTPUT.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
