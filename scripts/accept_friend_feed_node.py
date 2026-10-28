"""Run one isolated loopback node for the friend-feed browser/TCP acceptance.

Start three copies with distinct homes and ports. --seed creates one synthetic
saved text document; pairing, following and publishing use the actual APIs/UI.
No model, public registry, or discovery is enabled. Stop nodes before --reuse.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import quote

from accept_local_search import configure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--seed', action='store_true')
    parser.add_argument('--reuse', action='store_true')
    args = parser.parse_args()
    home = args.home.resolve()
    if not home.name.startswith('rynmesh-feed-acceptance-') or home.exists() != args.reuse:
        raise SystemExit('Choose a new dedicated rynmesh-feed-acceptance-* directory, or explicitly --reuse it.')
    configure(home, args.port)
    import uvicorn

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore
    app = create_app(RynmeshStore(home=home, network_dir=home / 'network', node_name=args.name))
    if args.seed and not args.reuse:
        saved = app.state.friends.content.imports.save('A short synthetic article for friend updates. 朋友分享验收正文。'.encode(),
            filename='friend-update.txt', mime='text/plain', source={'title': 'Weekend reading note', 'source_url': 'https://example.test/feed-note'})
        identifier = 'import:' + saved['import_id']
        app.state.consumption_store.record({'item_id': identifier, 'title': 'Weekend reading note', 'content_kind': 'document',
            'link': 'rynmesh://content/' + quote(identifier, safe=''), 'source_title': 'Synthetic notebook'}, 'bookmark')
    app.state.first_run.store.dismiss()
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
