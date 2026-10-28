"""Synthetic local fixture and HTTP assertions around actual browser cleanup."""
import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from rynmesh.services.library_imports import LibraryImportError, LibraryImportStore

PREFIX = '/api/local/privacy/documents'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--phase', choices=['prepare', 'pending', 'verify'], required=True)
    args = parser.parse_args()
    home = args.home.resolve()
    if home.name != 'rynmesh-feed-acceptance-documents-author':
        raise SystemExit('Use the dedicated synthetic document acceptance home.')
    store = LibraryImportStore(home / 'library-imports')
    case_path = home / '.document-cleanup-case.json'
    with httpx.Client(base_url='http://127.0.0.1:18920', timeout=30) as client:
        def call(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()
        assert Path(call('GET', '/api/local/privacy/status')['storage_root']).resolve() == home
        if args.phase == 'prepare':
            assert not case_path.exists()
            row, = store.list()
            body = call('GET', '/api/local/friends/documents/' + row['import_id'] + '/body')
            case_path.write_text(json.dumps({'original': row, 'original_body_hash': hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()}), encoding='utf-8')
            print('Synthetic document is readable; proceed with browser cleanup while the file is held.')
            return
        case = json.loads(case_path.read_text(encoding='utf-8'))
        row = case['original']
        job = call('GET', PREFIX + '/job')['job']
        if args.phase == 'pending':
            assert job['pending'] == ['files'] and not job['local_copies_complete']
            assert call('GET', '/api/local/friends/documents')['documents'] == []
            denied = client.get('/api/local/friends/documents/' + row['import_id'] + '/body')
            assert denied.status_code == 409 and denied.json()['detail'] == 'library_cleanup_pending'
            try:
                store.save(b'A short synthetic article for friend updates. ' + '\u670b\u53cb\u5206\u4eab\u9a8c\u6536\u6b63\u6587\u3002'.encode(),
                    filename=row['filename'], mime=row['mime'], source=row['source'])
            except LibraryImportError as exc:
                assert exc.code == 'library_cleanup_pending'
            else:
                raise AssertionError('A pending target was recreated')
            later = store.save(b'New independent document during pending cleanup', filename='later.txt', mime='text/plain')
            case.update(job=job, later=later, pending_http_checked=True)
            case_path.write_text(json.dumps(case), encoding='utf-8')
            print('Pending read denied; same copy creation denied; independent later document saved.')
            return
        assert case['pending_http_checked'] and job['id'] == case['job']['id'] and job['local_copies_complete']
        assert not store._directory(row['import_id']).exists()
        later_body = call('GET', '/api/local/friends/documents/' + case['later']['import_id'] + '/body')
        assert later_body['text'] == 'New independent document during pending cleanup'
        recreated = store.save(b'A short synthetic article for friend updates. ' + '\u670b\u53cb\u5206\u4eab\u9a8c\u6536\u6b63\u6587\u3002'.encode(),
            filename=row['filename'], mime=row['mime'], source=row['source'])
        assert recreated['import_id'] == row['import_id']
        assert call('POST', PREFIX + '/job', json={'scope': job['scope'], 'review_token': job['id']}) == job
        body = call('GET', '/api/local/friends/documents/' + recreated['import_id'] + '/body')
        assert hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() == case['original_body_hash']
        assert len(call('GET', '/api/local/consumption')) == 1
        result = {'verified_at': datetime.now(UTC).isoformat(), 'environment': 'Windows independent loopback node',
            'pending_http_read_denied': True, 'pending_same_copy_save_denied': True,
            'new_independent_document_preserved': True, 'original_job_completed': True,
            'same_id_saved_after_completion': True, 'completed_request_replay_kept_recreated_copy': True,
            'recreated_body_hash_matches': True, 'bookmark_retained': True, 'remote_erasure_confirmed': False}
        output = Path(__file__).resolve().parents[1] / 'docs/acceptance/friends-development/document-cleanup-http.json'
        output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(result))


if __name__ == '__main__':
    main()
