"""Synthetic Windows search fault fixture; browser interaction is separate."""
import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from accept_local_search import configure, create


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--phase', choices=['serve', 'damage', 'repair', 'verify'], required=True)
    args = parser.parse_args()
    home = args.home.resolve()
    if home.name != 'rynmesh-search-isolation-acceptance':
        raise SystemExit('Use the dedicated synthetic search isolation directory.')
    case_path = home / '.isolation-case.json'
    if args.phase == 'serve':
        if home.exists():
            raise SystemExit('Choose a fresh acceptance directory.')
        configure(home, 18920)
        app = create(home)
        imports = app.state.friends.content.imports
        imports.save(b'isolation healthy saved body', filename='Healthy notebook.txt', mime='text/plain')
        broken = imports.save(b'isolation unavailable private body', filename='Damaged copy.txt', mime='text/plain')
        article = {'item_id': 'healthy-reading', 'title': 'Healthy reading article', 'link': 'https://example.test/isolation', 'source_title': 'Synthetic journal'}
        app.state.consumption_store.record(article, 'opened')
        app.state.reader_cache.put(article['link'], {'blocks': [{'text': 'isolation healthy reading body'}]}, now=1)
        app.state.first_run.store.dismiss()
        app.state.local_search.index.rebuild(force=True)
        case_path.write_text(json.dumps({'target': broken['import_id'], 'metadata': broken}), encoding='utf-8')
        import uvicorn
        uvicorn.run(app, host='127.0.0.1', port=18920, access_log=False)
        return
    with httpx.Client(base_url='http://127.0.0.1:18920', timeout=30) as client:
        status = client.get('/api/local/privacy/status')
        status.raise_for_status()
        assert Path(status.json()['storage_root']).resolve() == home
        case = json.loads(case_path.read_text(encoding='utf-8'))
        path = (home / 'library-imports' / case['target'] / 'metadata.json').resolve()
        assert path.parent.parent == home / 'library-imports' and not path.is_symlink()
        if args.phase == 'damage':
            result = client.post('/api/local/search/query', json={'query': 'isolation'}).json()
            assert result['total'] == 3 and not result['partial']
            path.write_bytes(b'invalid synthetic metadata')
            result = client.post('/api/local/search/query', json={'query': 'isolation'}).json()
            assert result['total'] == 2 and result['unavailable_sources'] == ['saved_documents']
            denied = client.get('/api/local/search/open', params={'identifier': 'content:import:' + case['target']})
            assert denied.status_code == 409
            case['damaged_http_verified'] = True
            case_path.write_text(json.dumps(case), encoding='utf-8')
            print('Two healthy results remain; the damaged result cannot open. Inspect Search in the browser.')
        elif args.phase == 'repair':
            from rynmesh.atomic_io import atomic_write_json
            atomic_write_json(path, case['metadata'])
            print('Restored the synthetic metadata. Refresh browser search after the worker updates the index.')
        else:
            assert case['damaged_http_verified']
            result = client.post('/api/local/search/query', json={'query': 'isolation'}).json()
            assert result['total'] == 3 and not result['partial'] and result['unavailable_sources'] == []
            output = {'verified_at': datetime.now(UTC).isoformat(), 'environment': 'Windows loopback HTTP with live workers',
                'initial_results': 3, 'damaged_results': 2, 'healthy_results_retained': True,
                'damaged_result_open_denied': True, 'saved_document_warning_returned': True,
                'fixture_metadata_restoration_recovered_all_results': True, 'warning_cleared': True,
                'remote_requests_for_repair': False, 'packaged_desktop': False}
            destination = Path(__file__).resolve().parents[1] / 'docs/acceptance/search-development/source-isolation-http.json'
            destination.write_text(json.dumps(output, indent=2) + '\n', encoding='utf-8')
            print(json.dumps(output))


if __name__ == '__main__':
    main()
