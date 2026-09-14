"""Hold real search HTTP responses for browser SEARCH08 acceptance.

Only the transport is delayed; production search and UI are unchanged. The
fixture contains synthetic archived messages, has no model and never infers.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from accept_local_search import configure, create

HOME = Path('D:/code/rynmesh-search-order-acceptance-20260914')
CONTROL = HOME / '.response-control.json'
EVENTS = HOME / 'response-events.jsonl'
PORT = 18980


def control(mode):
    from rynmesh.atomic_io import atomic_write_json
    prior = json.loads(CONTROL.read_text())
    atomic_write_json(CONTROL, {'mode': mode, 'revision': prior['revision'] + 1})
    print(json.dumps({'mode': mode, 'revision': prior['revision'] + 1}))


class DelayTransport:
    def __init__(self, app):
        self.app = app
        self.serial = 0

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['path'] != '/api/local/search/query':
            return await self.app(scope, receive, send)
        self.serial += 1
        identity = self.serial
        body = bytearray()
        mode = json.loads(CONTROL.read_text())
        started = time.monotonic()
        delayed = False

        def event(state, **fields):
            with EVENTS.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'id': identity, 'state': state, 'at': datetime.now(UTC).isoformat(),
                    'elapsed_seconds': round(time.monotonic() - started, 4),
                    'mode': mode['mode'], 'revision': mode['revision'], **fields}) + '\n')

        async def read():
            message = await receive()
            if message['type'] == 'http.request':
                body.extend(message.get('body', b''))
            return message

        async def write(message):
            nonlocal delayed
            if message['type'] == 'http.response.start':
                payload = json.loads(body)
                is_old = payload.get('query') == 'slowcase'
                is_page = bool(payload.get('cursor'))
                delayed = is_old and (mode['mode'] == 'hold-first' and not is_page
                    or mode['mode'] == 'hold-page' and is_page)
                event('held' if delayed else 'ready', status=message['status'], cursor=is_page,
                    query_sha256=hashlib.sha256(payload.get('query', '').encode()).hexdigest())
                if delayed:
                    deadline = time.monotonic() + 180
                    while json.loads(CONTROL.read_text())['revision'] == mode['revision']:
                        if time.monotonic() > deadline:
                            event('safety-release')
                            break
                        await asyncio.sleep(.05)
                    event('released')
            await send(message)
            if message['type'] == 'http.response.body' and not message.get('more_body', False):
                event('sent', delayed=delayed)

        try:
            await self.app(scope, read, write)
        except asyncio.CancelledError:
            event('cancelled', delayed=delayed)
            raise


def serve():
    if HOME.exists():
        raise SystemExit('Fresh fixture requires an unused dedicated directory.')
    HOME.mkdir(parents=True)
    CONTROL.write_text(json.dumps({'mode': 'none', 'revision': 0}))
    configure(HOME, PORT)
    os.environ['RYNMESH_DEFAULT_DISCOVERY'] = '0'
    app = create(HOME)
    stamp = datetime.now(UTC).isoformat()
    common = {'serviceKey': 'acceptance::unavailable', 'serviceName': 'Unavailable fixture model',
        'providerPeerId': 'acceptance', 'networkId': 'acceptance', 'createdAt': stamp, 'updatedAt': stamp}
    for identity, title, marker, count in [('old', 'Earlier search archive', 'slowcase', 24),
            ('new', 'Current search result', 'freshcase', 1)]:
        app.state.ask_ryn.conversations.save({**common, 'id': identity, 'title': title,
            'messages': [{'id': f'message-{i}', 'role': 'user', 'content': f'{marker} synthetic entry {i}',
                'status': 'complete', 'createdAt': stamp} for i in range(count)]}, expected_revision=0)
    app.state.first_run.store.dismiss()
    app.state.local_search.index.rebuild(force=True)
    (HOME / '.search-order-fixture.json').write_text(json.dumps({
        'kind': 'ryn.search-order.v1', 'pid': os.getpid(), 'port': PORT, 'records': 25}))
    import uvicorn
    uvicorn.run(DelayTransport(app), host='127.0.0.1', port=PORT, access_log=False, log_level='warning')


def verify():
    rows = [json.loads(line) for line in EVENTS.read_text().splitlines()]
    events = {}
    for row in rows:
        events.setdefault(row['id'], {})[row['state']] = row
    assert set(events) == set(range(1, 9))
    assert not any(row['state'] in {'safety-release', 'cancelled'} for row in rows)
    for old, new in ((1, 2), (4, 5)):
        assert events[old]['held']['at'] < events[new]['sent']['at'] < events[old]['released']['at']
        assert events[old]['sent']['delayed'] is True
    assert not events[1]['held']['cursor']
    assert events[4]['held']['cursor'] and events[7]['held']['cursor']
    assert events[7]['sent']['delayed'] is True
    assert all(event['status'] == 200 for row in events.values()
        for state, event in row.items() if state in {'ready', 'held'})
    final = events[8]['ready']
    assert final['query_sha256'] == events[2]['ready']['query_sha256']
    assert final['revision'] == 6 and not final['cursor']
    result = {'scope': 'Production node behind a synthetic response-delay transport; actual Chrome interaction',
        'verified_at': datetime.now(UTC).isoformat(), 'events': rows,
        'delayed_seconds': {str(identity): events[identity]['sent']['elapsed_seconds'] for identity in (1, 4, 7)},
        'queries': 8, 'all_responses_200': True, 'old_first_and_page_sent_after_new': True,
        'rapid_input_final_query_count': 1, 'safety_timeout_used': False,
        'browser_checks_recorded_separately': True, 'packaged_desktop': False}
    output = Path(__file__).resolve().parents[1] / 'docs/acceptance/search-development/response-order-http-20260914.json'
    output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'queries': 8, 'delayed_seconds': result['delayed_seconds'], 'verified': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['serve', 'none', 'hold-first', 'hold-page', 'events', 'verify'])
    args = parser.parse_args()
    if args.phase == 'serve':
        serve()
    else:
        assert json.loads((HOME / '.search-order-fixture.json').read_text())['kind'] == 'ryn.search-order.v1'
        if args.phase == 'verify':
            verify()
        elif args.phase == 'events':
            print(EVENTS.read_text() if EVENTS.exists() else 'No search request yet.')
        else:
            control(args.phase)
