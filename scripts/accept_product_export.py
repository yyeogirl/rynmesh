"""Verify the isolated privacy acceptance node's ZIP without recording bodies."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password:
        raise SystemExit('Use the isolated loopback acceptance node.')
    with httpx.Client(base_url=args.url, timeout=60) as client:
        options = client.get('/api/local/privacy/export/scopes')
        options.raise_for_status()
        selected = [row['id'] for row in options.json()['scopes']]
        started = time.monotonic()
        response = client.post('/api/local/privacy/export/archive', json={'scopes': selected})
        response.raise_for_status()
        assert response.headers['content-type'] == 'application/zip'
        assert response.headers['cache-control'] == 'no-store'
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert archive.testzip() is None
            manifest = json.loads(archive.read('manifest.json'))
            assert manifest['selected_scopes'] == selected
            assert set(archive.namelist()) == {'manifest.json'} | {row['path'] for row in manifest['files']}
            for row in manifest['files']:
                data = archive.read(row['path'])
                assert len(data) == row['bytes'] and hashlib.sha256(data).hexdigest() == row['sha256']
            conversations = json.loads(archive.read('conversations/history.json'))
            # The acceptance fixture includes one visible conversation, recovery
            # branches and a global draft; exported values stay in memory only.
            assert len(conversations['conversations']) == 1
            assert conversations['sync_conflicts'] and conversations['draft']['text']
        result = {'version': 1, 'scenario': 'synthetic-privacy-node-product-export',
                  'transport': 'loopback HTTP with the production route package',
                  'scopes': selected, 'files': len(manifest['files']), 'zip_bytes': len(response.content),
                  'uncompressed_bytes': manifest['total_uncompressed_bytes'],
                  'elapsed_seconds': round(time.monotonic() - started, 3),
                  'zip_and_all_file_hashes_verified': True, 'conversation_draft_and_recovery_present': True,
                  'limits': ['Synthetic data', 'No packaged desktop or remote-device export acceptance',
                             'Binary download read over HTTP; no claim about the browser download folder']}
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
