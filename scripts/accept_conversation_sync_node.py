"""Isolated loopback UI for source-created conversation conflicts and recovery.

Two encrypted source stores create real causal branches through their APIs.
This seeds state explicitly for browser checks; it does not claim device pairing,
network delivery, packaged desktop or public-network acceptance.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from accept_local_search import configure


def seed(app, home):
    from rynmesh.ask_ryn.store import ConversationStore
    from rynmesh.services.peer_box import load_or_create_messaging_key

    left = app.state.ask_ryn.conversations
    other = home / 'synthetic-device-b'
    right = ConversationStore(other, load_or_create_messaging_key(other / 'messaging.x25519'))
    left.enable_sync()
    right.enable_sync()
    stamp = '2026-09-11T00:00:00Z'
    for identifier, title in (('sync-live-demo', 'Garden plans on two computers'),
                              ('sync-deleted-demo', 'A reply arrived after deletion')):
        original = {'id': identifier, 'title': title, 'serviceKey': 'original-provider::garden-model',
                    'providerPeerId': 'original-provider', 'serviceName': 'Garden model', 'networkId': 'rynmesh-main',
                    'createdAt': stamp, 'updatedAt': stamp,
                    'messages': [{'id': 'shared-question', 'role': 'user', 'status': 'complete', 'createdAt': stamp,
                                  'content': 'How should we plan a small garden? 这是两台电脑共享的问题。'}]}
        left.save(original, expected_revision=0)
    right.sync_receive(left.sync_export())
    for identifier in ('sync-live-demo', 'sync-deleted-demo'):
        for history, device in ((left, 'A'), (right, 'B')):
            current = history.get(identifier)
            if identifier == 'sync-deleted-demo' and device == 'A':
                history.remove(identifier, expected_revision=current['revision'])
                continue
            message = {'id': 'answer-' + device, 'role': 'assistant', 'status': 'complete', 'createdAt': stamp,
                       'content': f'Device {device} kept this separate answer. 电脑 {device} 的独立回答。\nThe original provider binding remains unchanged.'}
            history.save({**current, 'messages': [*current['messages'], message]}, expected_revision=current['revision'])
    left.sync_receive(right.sync_export())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18872)
    parser.add_argument('--reuse', action='store_true')
    args = parser.parse_args()
    home = args.home.resolve()
    if not home.name.startswith('rynmesh-conversation-sync-acceptance-') or home.exists() != args.reuse:
        raise SystemExit('Use a new rynmesh-conversation-sync-acceptance-* directory, or explicitly --reuse it.')
    configure(home, args.port)
    import uvicorn

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    app = create_app(RynmeshStore(home=home, network_dir=home / 'network', node_name='Conversation sync acceptance'))
    if not args.reuse:
        seed(app, home)
    app.state.first_run.store.dismiss()
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
