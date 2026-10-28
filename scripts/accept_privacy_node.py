"""Isolated, synthetic node and explicit browser fixture for cleanup acceptance.

The browser fixture only creates an unreadable legacy synthetic copy after a
button click. It is not a production route or evidence of actual old-user data.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from accept_conversation_sync_node import seed
from accept_local_search import configure

FIXTURE = '''<!doctype html><html lang="en"><meta charset="utf-8"><title>Privacy acceptance fixture</title>
<h1>Synthetic browser recovery fixture</h1>
<p>This isolated test page creates one intentionally unreadable old browser conversation. No real user data is used.</p>
<button id="seed">Create synthetic older browser copy</button><p id="status" role="status"></p>
<a href="/settings">Open Ryn settings</a>
<script>
document.getElementById('seed').onclick = () => {
  const request = indexedDB.open('ryn-private-ai-chat');
  request.onupgradeneeded = () => {
    for (const name of ['keys', 'conversations']) if (!request.result.objectStoreNames.contains(name)) request.result.createObjectStore(name, {keyPath:'id'});
  };
  request.onerror = () => { document.getElementById('status').textContent = 'Fixture creation failed'; };
  request.onsuccess = () => {
    const db = request.result;
    const tx = db.transaction('conversations', 'readwrite');
    tx.objectStore('conversations').put({id:'privacy-browser-fixture', serviceKey:'fixture::old-model', updatedAt:'2026-09-11T00:00:00Z', iv:'synthetic-invalid-iv', ciphertext:'synthetic-unreadable-ciphertext'});
    tx.oncomplete = () => { db.close(); document.getElementById('status').textContent = 'One synthetic unreadable browser copy created'; };
    tx.onerror = () => { db.close(); document.getElementById('status').textContent = 'Fixture creation failed'; };
  };
};
</script></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18878)
    parser.add_argument('--reuse', action='store_true')
    args = parser.parse_args()
    home = args.home.resolve()
    if not home.name.startswith('rynmesh-privacy-acceptance-') or home.exists() != args.reuse:
        raise SystemExit('Use a new rynmesh-privacy-acceptance-* directory, or explicitly --reuse it.')
    configure(home, args.port)
    import uvicorn
    from starlette.responses import HTMLResponse
    from starlette.routing import Route

    from rynmesh.atomic_io import atomic_write_bytes
    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    app = create_app(RynmeshStore(home=home, network_dir=home / 'network', node_name='Privacy acceptance'))
    if not args.reuse:
        seed(app, home)
        source = app.state.ask_ryn.conversations
        source.save_draft('Synthetic unsent cleanup draft', expected_revision=0)
        atomic_write_bytes(source.path.with_name('history.json.migrated'), source.path.read_bytes())
        app.state.device_sync.transfer.replica.reconcile_source(source.sync_export(), scopes=['conversations'])
        app.state.local_search.index.rebuild(force=True)
    app.state.first_run.store.dismiss()
    async def fixture(_):
        return HTMLResponse(FIXTURE)
    app.router.routes.insert(0, Route('/acceptance/browser-copies', fixture))
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
