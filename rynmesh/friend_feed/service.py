"""Use existing friendship authentication, encrypted transport and content copies.

Subscribing is a receiver preference. Only a publisher's current audience grants
access, and every protected read checks it again. No public registry is used.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import time
import uuid
from copy import deepcopy

from ..crypto import canonical_json
from ..friends.service import MAX_SHARED_CONTENT_BYTES, MAX_SHARED_RESPONSE_BYTES
from ..services import peer_box
from .store import MAX_PUBLICATIONS, MAX_SUBSCRIPTIONS, FeedError, identity, revision

PATH = '/api/peer/friend-feed'
WIRE_VERSION = 'ryn.friend-feed-wire.v1'
WIRE_CHANNEL = b'rynmesh-friend-feed-wire-v1'
PEER_ERRORS = frozenset({'feed_publication_unavailable', 'feed_publication_changed', 'feed_content_changed',
                         'feed_results_changed', 'feed_cursor_invalid', 'feed_friend_inactive'})


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


class FriendFeed:
    def __init__(self, *, store, friends, content, clock=time.time):
        self.store, self.friends, self.content, self.clock = store, friends, content, clock
        self.poll_index = 0

    def active(self, rid, peer_id=None):
        row = self.friends().store.relationship(identity(rid))
        if not row or row.get('status') != 'active' or peer_id is not None and row['peer_id'] != peer_id:
            raise FeedError('feed_friend_inactive')
        return row

    def audience(self, value):
        if not isinstance(value, dict) or set(value) != {'mode', 'relationship_ids'}:
            raise FeedError('feed_audience_invalid')
        mode, ids = value['mode'], value['relationship_ids']
        if mode not in {'selected', 'all_friends'} or not isinstance(ids, list) or len(ids) > MAX_SUBSCRIPTIONS:
            raise FeedError('feed_audience_invalid')
        if mode == 'selected' and not ids or mode == 'all_friends' and ids:
            raise FeedError('feed_audience_invalid')
        for rid in ids:
            self.active(rid)
        return {'mode': mode, 'relationship_ids': sorted(set(ids))}

    def _change(self, identifier, expected_revision, operation_id, request, apply):
        identity(identifier)
        identity(operation_id)
        revision(expected_revision)
        fingerprint = digest(request)

        def change(data):
            if identifier in data.get('erased_publications', {}):
                raise FeedError('feed_publication_erased')
            previous = data['publications'].get(identifier)
            if previous and previous.get('operation_id') == operation_id:
                if previous.get('operation_digest') != fingerprint:
                    raise FeedError('feed_operation_conflict')
                return self.owner_row(previous)
            if (previous or {}).get('revision', 0) != expected_revision:
                raise FeedError('feed_revision_conflict')
            if previous is None and len(data['publications']) >= MAX_PUBLICATIONS:
                raise FeedError('feed_capacity_exhausted')
            row = deepcopy(previous) if previous else {'id': identifier, 'draft': None, 'published': None, 'stopped': False}
            row['revision'] = expected_revision + 1
            apply(row)
            row.update(operation_id=operation_id, operation_digest=fingerprint)
            data['publications'][identifier] = row
            return self.owner_row(row)

        return self.store.mutate(change)

    def save_draft(self, identifier, *, reference, audience, expected_revision, operation_id):
        audience = self.audience(audience)
        if not isinstance(reference, dict) or set(reference) != {'item_id'}:
            raise FeedError('feed_reference_invalid')
        prepared = self.content().prepare(reference)
        resource = self.content().resolve(prepared['library_id'])
        body = bytes((resource or {}).get('data', b''))
        if not 0 < len(body) <= MAX_SHARED_CONTENT_BYTES:
            raise FeedError('feed_content_unavailable')
        card = self.friends()._clean_card({**prepared, 'sha256': hashlib.sha256(body).hexdigest(),
            'size_bytes': len(body), 'filename': resource['filename'], 'mime': resource['mime'], 'fetch_available': True})
        draft = {'card': card, 'audience': audience}
        return self._change(identifier, expected_revision, operation_id, {'action': 'draft', 'draft': draft},
                            lambda row: row.update(draft=draft))

    def publish(self, identifier, *, expected_revision, operation_id, confirm_all_friends=False):
        if type(confirm_all_friends) is not bool:
            raise FeedError('feed_confirmation_required')

        def apply(row):
            draft = row.get('draft')
            if not draft:
                raise FeedError('feed_draft_required')
            audience = self.audience(draft['audience'])
            if audience['mode'] == 'all_friends' and not confirm_all_friends:
                raise FeedError('feed_future_friends_confirmation_required')
            self._body(draft['card'])  # Confirm the reviewed snapshot still exists.
            now = self.clock()
            row['published'] = {'id': identifier, 'revision': row['revision'], 'card': deepcopy(draft['card']),
                'audience': audience, 'published_at': (row.get('published') or {}).get('published_at', now),
                'updated_at': now}
            row['stopped'] = False

        return self._change(identifier, expected_revision, operation_id,
                            {'action': 'publish', 'confirm_all_friends': confirm_all_friends}, apply)

    def stop(self, identifier, *, expected_revision, operation_id):
        def apply(row):
            if not row.get('published'):
                raise FeedError('feed_publication_unavailable')
            row['stopped'] = True
        return self._change(identifier, expected_revision, operation_id, {'action': 'stop'}, apply)

    @staticmethod
    def owner_row(row):
        return deepcopy({key: row[key] for key in ('id', 'revision', 'draft', 'published', 'stopped')})

    def publications(self):
        result = []
        active = [row for row in self.friends().list_friends() if row['status'] == 'active']
        for row in self.store.read()['publications'].values():
            public = self.owner_row(row)
            audience = (public['published'] or {}).get('audience', {})
            public['current_audience'] = [friend for friend in active if not row['stopped'] and row['published'] and
                (audience.get('mode') == 'all_friends' or friend['relationship_id'] in audience.get('relationship_ids', []))]
            result.append(public)
        return result

    @staticmethod
    def permitted(row, relationship):
        publication = row.get('published')
        if not publication or row['stopped']:
            return False
        audience = publication['audience']
        return audience['mode'] == 'all_friends' or relationship['relationship_id'] in audience['relationship_ids']

    def authorize(self, identifier, relationship, expected_revision=None):
        relationship = self.active(relationship['relationship_id'], relationship['peer_id'])
        row = self.store.read()['publications'].get(identity(identifier))
        if not row or not self.permitted(row, relationship):
            raise FeedError('feed_publication_unavailable')
        publication = row['published']
        if expected_revision is not None and publication['revision'] != revision(expected_revision):
            raise FeedError('feed_publication_changed')
        return publication

    @staticmethod
    def peer_row(publication):
        # Local library identifiers and audience membership stay with the owner.
        return {key: publication[key] for key in ('id', 'revision', 'published_at', 'updated_at')} | {
            'card': {key: value for key, value in publication['card'].items() if key != 'library_id'}}

    def page(self, relationship, *, cursor=''):
        relationship = self.active(relationship['relationship_id'], relationship['peer_id'])
        rows = [row['published'] for row in self.store.read()['publications'].values() if self.permitted(row, relationship)]
        rows.sort(key=lambda row: (-row['published_at'], row['id']))
        catalogue = {row['id']: row['revision'] for row in rows}
        generation, offset = digest(catalogue), 0
        if cursor:
            try:
                prior, offset_text = cursor.split(':')
                offset = int(offset_text)
                if prior != generation:
                    raise FeedError('feed_results_changed')
                if offset < 0 or offset > len(rows):
                    raise ValueError
            except FeedError:
                raise
            except (ValueError, AttributeError):
                raise FeedError('feed_cursor_invalid') from None
        return {'rows': [self.peer_row(row) for row in rows[offset:offset + 20]], 'catalogue': catalogue,
                'generation': generation, 'next_cursor': f'{generation}:{offset + 20}' if offset + 20 < len(rows) else ''}

    def _body(self, card):
        resource = self.content().resolve(card['library_id'])
        body = bytes((resource or {}).get('data', b''))
        if len(body) != card['size_bytes'] or hashlib.sha256(body).hexdigest() != card['sha256']:
            raise FeedError('feed_content_changed')
        return body

    def respond(self, wire, relationship):
        friends = self.friends()
        relationship = self.active(relationship['relationship_id'], relationship['peer_id'])
        try:
            plain = peer_box.open_sealed(friends.messaging_private, relationship['messaging_pub'],
                                         wire['nonce'], wire['ciphertext'], info=WIRE_CHANNEL)
            if len(plain) > 4096:
                raise ValueError
            request = json.loads(plain)
            identity(request['request_id'])
            if (request.get('version') != WIRE_VERSION or request.get('from') != relationship['peer_id'] or
                    request.get('to') != friends.peer_id or request.get('relationship_id') != relationship['relationship_id']):
                raise ValueError
        except Exception:
            raise FeedError('feed_request_rejected') from None
        try:
            if request.get('action') == 'list':
                result = self.page(relationship, cursor=request.get('cursor', ''))
            elif request.get('action') == 'fetch':
                publication = self.authorize(request.get('id'), relationship, request.get('revision'))
                body = self._body(publication['card'])
                # Resolve can take time; recheck both friendship and publication after it.
                self.authorize(publication['id'], relationship, publication['revision'])
                result = {'publication': self.peer_row(publication), 'data_base64': base64.b64encode(body).decode()}
            else:
                raise FeedError('feed_request_rejected')
        except FeedError as exc:
            if str(exc) not in PEER_ERRORS:
                raise
            # An authenticated recipient may learn the bounded failure reason,
            # encrypted with the same request binding. Never include exception text.
            result = {'error_code': str(exc)}
        response = {'version': WIRE_VERSION, 'request_id': request['request_id'],
                    'relationship_id': relationship['relationship_id'], 'result': result}
        nonce, ciphertext = peer_box.seal(friends.messaging_private, relationship['messaging_pub'],
                                        canonical_json(response), info=WIRE_CHANNEL)
        return {'nonce': nonce, 'ciphertext': ciphertext}

    def request(self, peer_id, action, **arguments):
        friends = self.friends()
        relationship, secret = friends._relationship(peer_id)
        request_id = uuid.uuid4().hex
        request = {'version': WIRE_VERSION, 'request_id': request_id, 'relationship_id': relationship['relationship_id'],
                   'from': friends.peer_id, 'to': peer_id, 'action': action, **arguments}
        nonce, ciphertext = peer_box.seal(friends.messaging_private, relationship['messaging_pub'],
                                        canonical_json(request), info=WIRE_CHANNEL)
        wire = friends._request_wire(relationship, secret, PATH, {'nonce': nonce, 'ciphertext': ciphertext},
                                     max_response_bytes=MAX_SHARED_RESPONSE_BYTES)
        try:
            plain = peer_box.open_sealed(friends.messaging_private, relationship['messaging_pub'],
                                        wire['nonce'], wire['ciphertext'], info=WIRE_CHANNEL)
            if len(plain) > MAX_SHARED_RESPONSE_BYTES:
                raise ValueError
            response = json.loads(plain)
            if (response.get('version') != WIRE_VERSION or response.get('request_id') != request_id or
                    response.get('relationship_id') != relationship['relationship_id'] or not isinstance(response.get('result'), dict)):
                raise ValueError
        except Exception:
            raise FeedError('feed_response_invalid') from None
        self.active(relationship['relationship_id'], peer_id)
        if response['result'].get('error_code') in PEER_ERRORS:
            raise FeedError(response['result']['error_code'])
        return response['result']

    def subscribe(self, relationship_id, *, enabled, expected_revision):
        identity(relationship_id)
        revision(expected_revision)
        if type(enabled) is not bool:
            raise FeedError('feed_subscription_invalid')

        def change(data):
            prior = data['subscriptions'].get(relationship_id)
            relationship = self.active(relationship_id) if enabled or not prior else None
            if prior and prior['revision'] == expected_revision + 1 and prior['enabled'] == enabled:
                return prior
            if (prior or {}).get('revision', 0) != expected_revision:
                raise FeedError('feed_revision_conflict')
            if not prior and len(data['subscriptions']) >= MAX_SUBSCRIPTIONS:
                raise FeedError('feed_capacity_exhausted')
            row = {**(prior or {}), 'relationship_id': relationship_id,
                   'peer_id': (prior or {}).get('peer_id') or relationship['peer_id'],
                   'enabled': enabled, 'revision': expected_revision + 1}
            data['subscriptions'][relationship_id] = row
            if not enabled:
                data['inbox'].pop(relationship_id, None)
            return row
        return self.store.mutate(change)

    def subscriptions(self):
        return [deepcopy(row) for row in self.store.read()['subscriptions'].values()]

    def refresh(self, relationship_id, *, cursor=''):
        relationship = self.active(relationship_id)

        def begin(data):
            row = data['subscriptions'].get(relationship_id)
            if not row or not row['enabled']:
                raise FeedError('feed_subscription_required')
            row['refresh_sequence'] = row.get('refresh_sequence', 0) + 1
            return row
        ticket = self.store.mutate(begin)

        def current(data):
            row = data['subscriptions'].get(relationship_id)
            if (not row or not row['enabled'] or row['revision'] != ticket['revision'] or
                    row.get('refresh_sequence') != ticket['refresh_sequence']):
                raise FeedError('feed_refresh_superseded')
            return row

        try:
            page = self.request(relationship['peer_id'], 'list', cursor=cursor)
            catalogue = page.get('catalogue')
            if not isinstance(catalogue, dict) or len(catalogue) > MAX_PUBLICATIONS or page.get('generation') != digest(catalogue):
                raise FeedError('feed_response_invalid')
            for identifier, rev in catalogue.items():
                identity(identifier)
                if revision(rev) < 1:
                    raise FeedError('feed_response_invalid')
            rows = page.get('rows')
            if not isinstance(rows, list) or len(rows) > 20 or not isinstance(page.get('next_cursor'), str) or len(page['next_cursor']) > 100:
                raise FeedError('feed_response_invalid')
            clean = {}
            for row in rows:
                identifier = identity(row.get('id'))
                if identifier in clean or revision(row.get('revision')) != catalogue.get(identifier):
                    raise FeedError('feed_response_invalid')
                for field in ('published_at', 'updated_at'):
                    if type(row.get(field)) not in (int, float) or not math.isfinite(row[field]) or row[field] < 0:
                        raise FeedError('feed_response_invalid')
                card = self.friends()._clean_card({**row['card'], 'library_id': 'peer-feed'})
                if not card['fetch_available']:
                    raise FeedError('feed_response_invalid')
                card.pop('library_id')
                clean[identifier] = {key: row[key] for key in ('id', 'revision', 'published_at', 'updated_at')} | {'card': card}
            self.active(relationship_id, relationship['peer_id'])

            def finish(data):
                subscription = current(data)
                prior = data['inbox'].get(relationship_id, {})
                if cursor and prior.get('generation') != page['generation']:
                    raise FeedError('feed_results_changed')
                # The complete authorized revision catalogue invalidates old cached
                # snippets even when the affected item is beyond the first page.
                records = {key: row for key, row in prior.get('records', {}).items()
                           if catalogue.get(key) == row['revision']}
                records.update(clean)
                next_cursor = page['next_cursor']
                if not cursor and prior.get('generation') == page['generation']:
                    next_cursor = prior.get('next_cursor', next_cursor)
                value = {'records': records, 'generation': page['generation'],
                         'next_cursor': next_cursor, 'checked_at': self.clock()}
                data['inbox'][relationship_id] = {**prior, **value}
                subscription['last_error'] = ''
                return value
            return self.store.mutate(finish)
        except Exception as exc:
            code = str(exc) if isinstance(exc, FeedError) else 'feed_friend_unreachable'

            def failure(data):
                row = data['subscriptions'].get(relationship_id)
                if row and row['revision'] == ticket['revision'] and row.get('refresh_sequence') == ticket['refresh_sequence']:
                    row['last_error'] = code
            self.store.mutate(failure)
            raise FeedError(code) from None

    def timeline(self):
        data = self.store.read()
        result = []
        for rid, subscription in data['subscriptions'].items():
            if not subscription['enabled']:
                continue
            try:
                friend = self.active(rid, subscription['peer_id'])
            except FeedError:
                continue
            cached = data['inbox'].get(rid, {})
            rows = [{**row, 'read': subscription.get('read', {}).get(row['id']) == row['revision']}
                    for row in cached.get('records', {}).values()]
            rows.sort(key=lambda row: (-row['published_at'], row['id']))
            result.append({'relationship_id': rid, 'peer_id': friend['peer_id'], 'node_name': friend['node_name'],
                'subscription_revision': subscription['revision'], 'rows': rows, 'checked_at': cached.get('checked_at'),
                'next_cursor': cached.get('next_cursor', ''), 'error_code': subscription.get('last_error', '')})
        return result

    def mark_read(self, relationship_id, identifier, *, expected_revision):
        self.active(relationship_id)
        identity(identifier)
        revision(expected_revision)

        def change(data):
            subscription = data['subscriptions'].get(relationship_id)
            row = data['inbox'].get(relationship_id, {}).get('records', {}).get(identifier)
            if not subscription or not subscription['enabled'] or not row or row['revision'] != expected_revision:
                raise FeedError('feed_publication_changed')
            subscription.setdefault('read', {})[identifier] = expected_revision
            return {'id': identifier, 'revision': expected_revision, 'read': True}
        return self.store.mutate(change)

    def fetch(self, relationship_id, identifier, *, expected_revision):
        friend = self.active(relationship_id)
        identity(identifier)
        revision(expected_revision)
        generation = self.content().imports.generation()
        result = self.request(friend['peer_id'], 'fetch', id=identifier, revision=expected_revision)
        try:
            publication = result['publication']
            if publication['id'] != identifier or publication['revision'] != expected_revision:
                raise ValueError
            card = self.friends()._clean_card({**publication['card'], 'library_id': 'peer-feed'})
            body = base64.b64decode(result['data_base64'], validate=True)
            if (not 0 < len(body) <= MAX_SHARED_CONTENT_BYTES or len(body) != card['size_bytes'] or
                    hashlib.sha256(body).hexdigest() != card['sha256']):
                raise ValueError
        except Exception:
            raise FeedError('feed_content_verification_failed') from None
        # A user-confirmed import is an independent saved copy; unsubscribe or
        # remote revocation cannot erase it. Use existing generation/cleanup checks.
        copy_id = digest([friend['peer_id'], relationship_id, identifier, expected_revision])[:32]
        return self.content().import_card({'peer_id': friend['peer_id'], 'card_id': copy_id, 'card': card,
            'data': body, 'filename': card['filename'], 'mime': card['mime'], 'generation': generation})

    def run_once(self):
        rows = [row for row in self.subscriptions() if row['enabled']]
        if not rows:
            return False
        row = rows[self.poll_index % len(rows)]
        self.poll_index += 1
        self.refresh(row['relationship_id'])
        return True
