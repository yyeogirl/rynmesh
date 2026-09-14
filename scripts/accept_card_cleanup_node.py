"""Serve an isolated, labelled synthetic card-cleanup fixture using installed code."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18994)
    args = parser.parse_args()
    home = args.home.resolve()
    if not home.name.startswith('card-cleanup-'):
        raise ValueError('Use a dedicated card-cleanup-* directory')
    marker = home / '.card-cleanup-acceptance.json'
    if home.exists() and not marker.exists():
        raise ValueError('Existing directories without the acceptance marker are not reused')
    for key in list(os.environ):
        if key.startswith('RYNMESH_'):
            del os.environ[key]
    os.environ.update(RYNMESH_HOME=str(home), RYNMESH_LLM_HOME=str(home / 'llm'),
        RYNMESH_NETWORK_DIR=str(home / 'network'), RYNMESH_REGISTRY_DIR=str(home / 'network/registry'),
        RYNMESH_AUTO_REGISTER='0', RYNMESH_DISABLE_DISCOVERY='1', RYNMESH_DEFAULT_DISCOVERY='0',
        RYNMESH_MODEL_PROVIDER='none', RYNMESH_PEER_ENDPOINT=f'http://127.0.0.1:{args.port}',
        RYNMESH_FRIEND_ENDPOINT=f'http://127.0.0.1:{args.port}', RYNMESH_FRIEND_ALLOW_LOOPBACK='1')
    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore
    app = create_app(RynmeshStore(home=home, network_dir=home / 'network', node_name='Synthetic card cleanup acceptance'))
    service = app.state.friends.service
    if not marker.exists():
        card = {'card_id': 'a' * 32, 'card': service._clean_card({'title': 'Synthetic current card to clear'}),
            'dir': 'in', 'from': 'synthetic-sender', 'to': service.peer_id, 'relationship_id': 'synthetic-revoked',
            'created_at': '2026-09-14T00:00:00+00:00', 'fetch_state': 'metadata_only'}
        service.store.put_card(card)
        (service.store.root / 'content-cards.jsonl').write_text(json.dumps({**card, 'card_id': 'b' * 32,
            'card': service._clean_card({'title': 'Synthetic legacy card to clear'})}) + '\n', encoding='utf-8')
        backup = service.store.root / 'state.json.migrated'
        backup.write_bytes(service.store.state_path.read_bytes())
        saved = app.state.friends.content.imports.save(b'Independent saved copy must survive card cleanup.',
            filename='keep.txt', mime='text/plain')
        app.state.first_run.store.dismiss()
        marker.write_text(json.dumps({'kind': 'ryn.card-cleanup.acceptance.v1', 'import_id': saved['import_id']}) + '\n')
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
