"""Serve an isolated real node for manual two-browser device pairing checks.

Start twice with distinct homes and ports. Optional seeds are synthetic only;
discovery is disabled and no model runs. This is loopback evidence, not cross-NAT or
packaged desktop acceptance. --reuse preserves identity for restart checks.
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from accept_local_search import configure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--reuse', action='store_true')
    parser.add_argument('--seed', action='store_true', help='Seed synthetic saved/progress/history sources on a new node.')
    parser.add_argument('--position', type=float, help='Change the synthetic article position before this reused node starts networking.')
    parser.add_argument('--body', action='store_true', help='Independently seed synthetic reader text on this node; text is not transferred by sync.')
    args = parser.parse_args()
    home = args.home.resolve()
    if not home.name.startswith('rynmesh-device-pairing-acceptance-') or home.exists() != args.reuse:
        raise SystemExit('Use a new rynmesh-device-pairing-acceptance-* directory, or explicitly --reuse it.')
    if args.position is not None and (not args.reuse or not 0 <= args.position <= 1):
        raise SystemExit('--position requires --reuse and a value between 0 and 1.')
    configure(home, args.port)
    os.environ.update(RYNMESH_DEVICE_ENDPOINT=f'http://127.0.0.1:{args.port}', RYNMESH_DEVICE_ALLOW_LOOPBACK='1')
    import uvicorn

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    app = create_app(RynmeshStore(home=home, network_dir=home / 'network', node_name=args.name))
    if args.seed:
        if args.reuse:
            raise SystemExit('--seed requires a new node home.')
        item = {'item_id': 'device-transfer-article', 'title': 'Device transfer reading sample',
                'source_title': 'Synthetic acceptance source', 'link': 'https://example.test/device-transfer', 'content_kind': 'article'}
        app.state.consumption_store.record(item, 'bookmark')
        app.state.consumption_store.record(item, 'progress', progress=.65)
        stamp = '2026-09-11T00:00:00Z'
        app.state.ask_ryn.conversations.save({'id': 'device-transfer-conversation', 'title': 'Conversation from the other computer',
            'serviceKey': 'acceptance-provider::original-model', 'providerPeerId': 'acceptance-provider',
            'serviceName': 'Original acceptance model', 'networkId': 'rynmesh-main', 'createdAt': stamp, 'updatedAt': stamp,
            'messages': [{'id': 'original-answer', 'role': 'assistant', 'status': 'complete', 'createdAt': stamp,
                          'content': 'This history arrived through encrypted device transfer. 原服务绑定保留，没有调用模型。'}]}, expected_revision=0)
    app.state.first_run.store.dismiss()
    if args.body:
        paragraphs = [f'Section {number}. Device reading checkpoint. ' +
            'This synthetic text checks continuing the same article on another computer. ' * 18 for number in range(1, 21)]
        app.state.reader_cache.put('https://example.test/device-transfer', {
            'url': 'https://example.test/device-transfer', 'title': 'Device transfer reading sample',
            'byline': 'Synthetic acceptance source', 'lead_image': '', 'truncated': False,
            'blocks': [{'tag': 'p', 'text': paragraph} for paragraph in paragraphs],
            'word_count': sum(len(paragraph.split()) for paragraph in paragraphs)}, now=time.time())
    if args.position is not None:
        rows = app.state.consumption_store.list()
        original = next((row for row in rows if row['item_id'] == 'device-transfer-article'), None)
        if original is None:
            raise SystemExit('The reused node has no synthetic transfer article.')
        app.state.consumption_store.record(original['item'], 'progress', progress=args.position)
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
