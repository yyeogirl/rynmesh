"""Two-owner device pairing with restartable, authenticated confirmation.

No source history is accessed here. Only an active pairing's mutually selected
policy can authorize the later transfer adapter. Network calls never hold a
local transaction, including during crossed requests and response-loss retries.
"""
from __future__ import annotations

import hmac
import os
import time
import uuid
from contextlib import contextmanager

from ..crypto import canonical_json, public_key_from_private, sign_payload
from ..file_transactions import file_transaction
from ..services import peer_box
from . import pair_crypto as crypto
from .pair_store import PairingStore
from .records import MAX_COUNTER, SyncError, actor_id, fingerprint

JOIN = 'ryn.device-join.v1'
CONFIRM = 'ryn.device-confirm.v1'
RESPONSE = 'ryn.device-pair-response.v1'
POLICY = 'ryn.device-policy.v1'
POLICY_CHANNEL = crypto.CHANNEL + b':policy'
REVOKE = 'ryn.device-revoke.v1'
REVOKE_CHANNEL = crypto.CHANNEL + b':revoke'


def policy(value):
    if not isinstance(value, dict) or set(value) != {'revision', 'scopes', 'paused'}:
        raise SyncError('sync_policy_invalid')
    if type(value['revision']) is not int or not 1 <= value['revision'] <= MAX_COUNTER or type(value['paused']) is not bool:
        raise SyncError('sync_policy_invalid')
    if crypto.selected(value['scopes']) != value['scopes']:
        raise SyncError('sync_policy_invalid')
    return dict(value)


class PairingService:
    def __init__(self, home, *, identity_private, messaging_key, name, endpoint, post_json, clock=time.time, allow_loopback=False):
        self.identity_private, self.messaging_key = identity_private, messaging_key
        self.post_json, self.clock, self.allow_loopback = post_json, clock, allow_loopback
        self.store = PairingStore(home, messaging_key)
        self.identity = {'peer_id': public_key_from_private(identity_private),
            'messaging_pub': peer_box.public_key_b64(messaging_key), 'actor': self.store.actor,
            'name': name, 'endpoint': endpoint}

    def readiness(self):
        try:
            crypto.identity(self.identity, allow_loopback=self.allow_loopback)
            return {'pairing_available': True, 'reason': None}
        except SyncError:
            return {'pairing_available': False, 'reason': 'sync_endpoint_unavailable'}

    def _new_identity(self):
        if not self.readiness()['pairing_available']:
            raise SyncError('sync_endpoint_unavailable')
        return crypto.identity(self.identity, allow_loopback=self.allow_loopback)

    @staticmethod
    def device(identity):
        return {key: identity[key] for key in ('name', 'peer_id', 'actor', 'endpoint')}

    def public(self, row):
        local = row.get('local_policy', {'revision': 0, 'scopes': row['requested_scopes'], 'paused': False})
        remote = row.get('remote_policy', {'revision': 0, 'scopes': [], 'paused': False})
        status = row['status']
        if status not in {'active', 'revoked', 'rejected'} and row['expires'] <= self.clock():
            status = 'expired'
        effective = sorted(set(local['scopes']) & set(remote['scopes'])) if status == 'active' and not local['paused'] and not remote['paused'] else []
        return {'id': row['id'], 'role': row['role'], 'status': status, 'device': self.device(row['remote']),
                'review_token': row['id'], 'verification_code': '-'.join(row['id'][index:index + 4] for index in range(0, 24, 4)),
                'expires': row['expires'], 'scopes': local['scopes'], 'remote_scopes': remote['scopes'],
                'paused': local['paused'], 'remote_paused': remote['paused'], 'revision': local['revision'], 'effective_scopes': effective,
                'removal_pending': bool(row.get('revoke_wire'))}

    def list(self):
        return [self.public(row) for row in self.store.snapshot()['pairs'].values()]

    def list_invites(self):
        results = []
        for row in self.store.snapshot()['invites'].values():
            value = row['proof']['payload']
            status = row['status']
            if status == 'open' and value['expires'] <= self.clock():
                status = 'expired'
            results.append({'id': value['id'], 'scopes': value['scopes'], 'created': value['created'],
                            'expires': value['expires'], 'status': status, 'pair_id': row.get('pair_id')})
        return results

    def get(self, pair_id):
        return self.public(self._pair(self.store.snapshot(), pair_id))

    def _pair(self, data, pair_id):
        actor_id(pair_id)
        row = data['pairs'].get(pair_id)
        if row is None:
            raise SyncError('sync_pairing_not_found')
        if not self._same_identity(row['local'], self.identity):
            raise SyncError('sync_device_identity_changed')
        return row

    @staticmethod
    def _same_peer(left, right):
        return left['actor'] == right['actor'] or left['peer_id'] == right['peer_id']

    @staticmethod
    def _same_identity(left, right):
        return all(left[key] == right[key] for key in ('actor', 'peer_id', 'messaging_pub'))

    def _available(self, data, remote, *, except_id=None):
        if self._same_peer(self.identity, remote):
            raise SyncError('sync_cannot_pair_self')
        if any(row['id'] != except_id and row['status'] == 'active' and self._same_peer(row['remote'], remote) for row in data['pairs'].values()):
            raise SyncError('sync_device_already_paired')

    def create_invite(self, scopes, *, ttl_seconds=900):
        identity = self._new_identity()
        scopes = crypto.selected(scopes)
        if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 3600:
            raise SyncError('sync_invite_lifetime_invalid')
        now, secret = int(self.clock()), os.urandom(32)
        payload = {'kind': crypto.INVITE, 'id': uuid.uuid4().hex, 'inviter': identity, 'scopes': scopes,
                   'created': now, 'expires': now + ttl_seconds, 'secret_hash': crypto.secret_hash(secret)}
        proof = sign_payload(payload, private_key_bytes=self.identity_private).to_dict()
        self.store.mutate(lambda data: data['invites'].__setitem__(payload['id'], {'proof': proof, 'status': 'open'}))
        uri = 'rynmesh://device-pair/' + crypto.encoded(canonical_json({'proof': proof, 'secret': crypto.encoded(secret)}))
        return {'uri': uri, 'invite': self.inspect_invite(uri)}

    def inspect_invite(self, uri):
        proof, _ = crypto.invitation(uri, now=self.clock(), allow_loopback=self.allow_loopback)
        payload = proof.payload
        return {'id': payload['id'], 'device': self.device(payload['inviter']), 'scopes': payload['scopes'],
                'created': payload['created'], 'expires': payload['expires']}

    def _seal(self, payload, receiver, *, channel=crypto.CHANNEL):
        sender = crypto.identity(payload['sender'], allow_loopback=self.allow_loopback)
        return crypto.seal(payload, private_key=self.identity_private, messaging_key=self.messaging_key,
                           sender=sender, receiver=receiver, channel=channel)

    def _open(self, wire, *, channel=crypto.CHANNEL):
        proof, sender = crypto.open_wire(wire, messaging_key=self.messaging_key, allow_loopback=self.allow_loopback, channel=channel)
        if proof.payload.get('receiver') != self.identity['peer_id'] or self._same_peer(sender, self.identity):
            raise SyncError('sync_pairing_invalid')
        return proof, sender

    def _reply(self, wire, receiver, **fields):
        row = self.store.snapshot()['pairs'].get(fields.get('pair_id'))
        sender = row['local'] if row else self._new_identity()
        return self._seal({'kind': RESPONSE, 'sender': sender, 'receiver': receiver['peer_id'],
                           'request_hash': fingerprint(wire), **fields}, receiver)

    def _response(self, wire, response, remote):
        proof, sender = self._open(response)
        value = proof.payload
        if not self._same_identity(sender, remote) or value.get('kind') != RESPONSE or value.get('request_hash') != fingerprint(wire):
            raise SyncError('sync_pairing_response_invalid')
        return value

    def start_join(self, uri, scopes):
        scopes = crypto.selected(scopes)
        invitation, secret = crypto.invitation(uri, now=self.clock(), allow_loopback=self.allow_loopback, allow_expired=True)
        invite = invitation.payload
        if not set(scopes) <= set(invite['scopes']):
            raise SyncError('sync_scope_denied')

        def prepare(data):
            for previous in data['pairs'].values():
                if previous['role'] == 'joiner' and previous['invite_id'] == invite['id'] and previous['remote'] == invite['inviter']:
                    if previous['requested_scopes'] != scopes:
                        raise SyncError('sync_pairing_intent_conflict')
                    return previous
            if invite['expires'] <= self.clock():
                raise SyncError('sync_invite_expired')
            self._available(data, invite['inviter'])
            identity = self._new_identity()
            proposal = {'kind': JOIN, 'sender': identity, 'receiver': invite['inviter']['peer_id'],
                        'invite': invitation.to_dict(), 'secret': crypto.encoded(secret), 'scopes': scopes, 'request_id': uuid.uuid4().hex}
            proof = sign_payload(proposal, private_key_bytes=self.identity_private).to_dict()
            pair_id = fingerprint({'invite': invitation.to_dict(), 'join': proof})
            row = {'id': pair_id, 'role': 'joiner', 'local': identity, 'remote': invite['inviter'], 'invite_id': invite['id'],
                   'requested_scopes': scopes, 'expires': invite['expires'], 'status': 'awaiting_inviter',
                   'join_wire': self._seal(proposal, invite['inviter'])}
            data['pairs'][pair_id] = row
            return row
        return self.public(self.store.mutate(prepare))

    def join(self, uri, scopes):
        return self.retry(self.start_join(uri, scopes)['id'])

    def receive_join(self, wire):
        proof, sender = self._open(wire)
        value = proof.payload
        if set(value) != {'kind', 'sender', 'receiver', 'invite', 'secret', 'scopes', 'request_id'} or value['kind'] != JOIN:
            raise SyncError('sync_pairing_invalid')
        crypto.identifier(value['request_id'])
        # Reuse the strict invite parser, including domain and secret checks.
        uri = 'rynmesh://device-pair/' + crypto.encoded(canonical_json({'proof': value['invite'], 'secret': value['secret']}))
        invitation, _ = crypto.invitation(uri, now=self.clock(), allow_loopback=self.allow_loopback, allow_expired=True)
        invite = invitation.payload
        scopes = crypto.selected(value['scopes'])
        if not self._same_identity(invite['inviter'], self.identity) or not set(scopes) <= set(invite['scopes']):
            raise SyncError('sync_pairing_invalid')
        pair_id = fingerprint({'invite': invitation.to_dict(), 'join': proof.to_dict()})

        def receive(data):
            saved = data['invites'].get(invite['id'])
            if saved is None or saved['proof'] != invitation.to_dict():
                raise SyncError('sync_invite_invalid')
            if saved.get('pair_id') and saved['pair_id'] != pair_id:
                return {'pair_id': pair_id, 'state': 'rejected', 'reason': 'sync_invite_used'}
            row = data['pairs'].get(pair_id)
            if saved['status'] == 'cancelled' or (row and row['status'] in {'rejected', 'revoked'}):
                return {'pair_id': pair_id, 'state': 'rejected', 'reason': 'sync_pairing_cancelled'}
            if invite['expires'] <= self.clock() and (not row or row['status'] != 'active'):
                return {'pair_id': pair_id, 'state': 'expired', 'reason': 'sync_invite_expired'}
            if row is None:
                self._available(data, sender)
                row = {'id': pair_id, 'role': 'inviter', 'local': invite['inviter'], 'remote': sender, 'invite_id': invite['id'],
                       'requested_scopes': scopes, 'expires': invite['expires'], 'status': 'awaiting_owner'}
                data['pairs'][pair_id] = row
                saved.update(status='claimed', pair_id=pair_id)
            return {'pair_id': pair_id, 'state': 'offer' if row.get('offer') else 'pending',
                    **({'offer': row['offer']} if row.get('offer') else {})}
        return self._reply(wire, sender, **self.store.mutate(receive))

    def approve(self, pair_id, *, review_token, scopes):
        scopes = crypto.selected(scopes)

        def approve(data):
            row = self._pair(data, pair_id)
            if row['role'] != 'inviter' or review_token != pair_id or not set(scopes) <= set(row['requested_scopes']):
                raise SyncError('sync_pairing_review_changed')
            if row['status'] in {'awaiting_peer', 'active'}:
                if row['offer']['scopes'] != scopes:
                    raise SyncError('sync_pairing_review_changed')
                return row
            if row['status'] != 'awaiting_owner' or row['expires'] <= self.clock():
                raise SyncError('sync_pairing_not_pending')
            self._available(data, row['remote'], except_id=pair_id)
            offer = {'id': pair_id, 'inviter': row['local'], 'joiner': row['remote'], 'scopes': scopes, 'secret': crypto.encoded(os.urandom(32))}
            row.update(status='awaiting_peer', offer=offer, approval_hash=fingerprint(offer), secret=offer['secret'],
                       local_policy={'revision': 1, 'scopes': scopes, 'paused': False},
                       remote_policy={'revision': 1, 'scopes': scopes, 'paused': False})
            return row
        return self.public(self.store.mutate(approve))

    def _accept_offer(self, pair_id, response):
        def accept(data):
            row = self._pair(data, pair_id)
            if row['status'] in {'revoked', 'rejected', 'active'}:
                return row
            if response.get('pair_id') != pair_id:
                raise SyncError('sync_pairing_response_invalid')
            if response.get('state') in {'expired', 'rejected'}:
                row.update(status='rejected', reason=response.get('reason', 'sync_pairing_cancelled'))
                return row
            if response.get('state') == 'pending':
                return row
            offer = response.get('offer')
            if (response.get('state') != 'offer' or not isinstance(offer, dict) or set(offer) != {'id', 'inviter', 'joiner', 'scopes', 'secret'}
                or offer['id'] != pair_id or offer['inviter'] != row['remote'] or offer['joiner'] != row['local']
                or not set(crypto.selected(offer['scopes'])) <= set(row['requested_scopes'])):
                raise SyncError('sync_pairing_response_invalid')
            secret = crypto.decoded(offer['secret'], size=32)
            if row.get('approval_hash') and row['approval_hash'] != fingerprint(offer):
                raise SyncError('sync_pairing_response_invalid')
            row.update(status='awaiting_ack', secret=offer['secret'], approval_hash=fingerprint(offer),
                       local_policy={'revision': 1, 'scopes': offer['scopes'], 'paused': False},
                       remote_policy={'revision': 1, 'scopes': offer['scopes'], 'paused': False})
            row['confirm_wire'] = self._seal({'kind': CONFIRM, 'sender': row['local'], 'receiver': row['remote']['peer_id'],
                'pair_id': pair_id, 'approval_hash': row['approval_hash'], 'confirmation': crypto.confirmation(secret, pair_id)}, row['remote'])
            return row
        return self.store.mutate(accept)

    def receive_confirm(self, wire):
        proof, sender = self._open(wire)
        value = proof.payload
        if set(value) != {'kind', 'sender', 'receiver', 'pair_id', 'approval_hash', 'confirmation'} or value['kind'] != CONFIRM:
            raise SyncError('sync_pairing_invalid')

        def confirm(data):
            row = self._pair(data, value['pair_id'])
            if row['role'] != 'inviter' or not self._same_identity(row['remote'], sender):
                raise SyncError('sync_pairing_invalid')
            if row['status'] in {'revoked', 'rejected'}:
                return {'pair_id': row['id'], 'state': 'rejected'}
            if row['status'] not in {'awaiting_peer', 'active'}:
                raise SyncError('sync_pairing_not_pending')
            if row['status'] != 'active' and row['expires'] <= self.clock():
                return {'pair_id': row['id'], 'state': 'expired'}
            if value['approval_hash'] != row['approval_hash'] or not hmac.compare_digest(str(value['confirmation']), crypto.confirmation(crypto.decoded(row['secret'], size=32), row['id'])):
                raise SyncError('sync_pairing_invalid')
            self._available(data, sender, except_id=row['id'])
            row['status'] = 'active'
            return {'pair_id': row['id'], 'state': 'active', 'approval_hash': row['approval_hash']}
        return self._reply(wire, sender, **self.store.mutate(confirm))

    def retry(self, pair_id):
        row = self._pair(self.store.snapshot(), pair_id)
        if row['role'] != 'joiner' or row['status'] not in {'awaiting_inviter', 'awaiting_ack'}:
            return self.public(row)
        if row['status'] == 'awaiting_inviter':
            wire = row['join_wire']
            result = self.post_json(row['remote']['endpoint'], '/api/peer/device-sync/join', wire)
            row = self._accept_offer(pair_id, self._response(wire, result, row['remote']))
        if row['status'] == 'awaiting_ack':
            wire = row['confirm_wire']
            result = self.post_json(row['remote']['endpoint'], '/api/peer/device-sync/confirm', wire)
            response = self._response(wire, result, row['remote'])

            def acknowledged(data):
                current = self._pair(data, pair_id)
                if current['status'] in {'revoked', 'rejected', 'active'}:
                    return current
                if response.get('pair_id') != pair_id:
                    raise SyncError('sync_pairing_response_invalid')
                if response.get('state') in {'expired', 'rejected'}:
                    current['status'] = 'rejected'
                elif response.get('state') == 'active' and response.get('approval_hash') == current['approval_hash']:
                    current['status'] = 'active'
                else:
                    raise SyncError('sync_pairing_response_invalid')
                return current
            row = self.store.mutate(acknowledged)
        return self.public(row)

    def cancel_invite(self, invite_id):
        crypto.identifier(invite_id)

        def cancel(data):
            invite = data['invites'].get(invite_id)
            if invite is None:
                raise SyncError('sync_invite_invalid')
            pair = data['pairs'].get(invite.get('pair_id'))
            if pair and pair['status'] == 'active':
                raise SyncError('sync_device_already_paired')
            invite['status'] = 'cancelled'
            if pair:
                pair['status'] = 'rejected'
                pair.pop('secret', None)
                pair.pop('offer', None)
            return {'cancelled': True}
        return self.store.mutate(cancel)

    def configure(self, pair_id, *, expected_revision, scopes, paused):
        scopes = crypto.selected(scopes)
        if type(paused) is not bool:
            raise SyncError('sync_policy_invalid')

        def configure(data):
            row = self._pair(data, pair_id)
            if row['status'] != 'active':
                raise SyncError('sync_device_not_active')
            prior = policy(row['local_policy'])
            if prior['scopes'] == scopes and prior['paused'] == paused:
                return row
            if type(expected_revision) is not int or prior['revision'] != expected_revision:
                raise SyncError('sync_revision_conflict')
            row['local_policy'] = policy({'revision': prior['revision'] + 1, 'scopes': scopes, 'paused': paused})
            return row
        return self.public(self.store.mutate(configure))

    def receive_policy(self, wire):
        proof, sender = self._open(wire, channel=POLICY_CHANNEL)
        value = proof.payload
        if set(value) != {'kind', 'sender', 'receiver', 'pair_id', 'policy'} or value['kind'] != POLICY:
            raise SyncError('sync_policy_invalid')
        incoming = policy(value['policy'])

        def accept(data):
            row = self._pair(data, value['pair_id'])
            if row['status'] != 'active' or not self._same_identity(row['remote'], sender):
                raise SyncError('sync_device_not_active')
            previous = policy(row['remote_policy'])
            if incoming['revision'] == previous['revision'] and incoming != previous:
                raise SyncError('sync_policy_revision_conflict')
            if incoming['revision'] > previous['revision']:
                row['remote_policy'] = incoming
            return {'pair_id': row['id'], 'state': 'policy', 'policy': row['local_policy']}
        return self._reply(wire, sender, **self.store.mutate(accept))

    def exchange_policy(self, pair_id):
        row = self._pair(self.store.snapshot(), pair_id)
        if row['status'] != 'active':
            raise SyncError('sync_device_not_active')
        wire = self._seal({'kind': POLICY, 'sender': row['local'], 'receiver': row['remote']['peer_id'],
                           'pair_id': pair_id, 'policy': row['local_policy']}, row['remote'], channel=POLICY_CHANNEL)
        response = self._response(wire, self.post_json(row['remote']['endpoint'], '/api/peer/device-sync/policy', wire), row['remote'])
        if response.get('pair_id') != pair_id or response.get('state') != 'policy':
            raise SyncError('sync_pairing_response_invalid')
        incoming = policy(response.get('policy'))

        def accept(data):
            current = self._pair(data, pair_id)
            if current['status'] != 'active':
                raise SyncError('sync_device_not_active')
            prior = policy(current['remote_policy'])
            if incoming['revision'] == prior['revision'] and incoming != prior:
                raise SyncError('sync_policy_revision_conflict')
            if incoming['revision'] > prior['revision']:
                current['remote_policy'] = incoming
            return current
        return self.public(self.store.mutate(accept))

    def authorize(self, pair_id, *, remote_actor, scopes, sender_revision, receiver_revision):
        requested = crypto.selected(scopes)
        row = self._pair(self.store.snapshot(), pair_id)
        if row['status'] != 'active' or row['remote']['actor'] != remote_actor:
            raise SyncError('sync_device_not_active')
        local, remote = policy(row['local_policy']), policy(row['remote_policy'])
        if local['paused'] or remote['paused']:
            raise SyncError('sync_paused')
        if type(receiver_revision) is not int or type(sender_revision) is not int or receiver_revision != local['revision'] or sender_revision != remote['revision']:
            raise SyncError('sync_policy_changed')
        if not set(requested) <= set(local['scopes']) & set(remote['scopes']):
            raise SyncError('sync_scope_denied')
        return row

    @contextmanager
    def authorized(self, pair_id, **kwargs):
        """Keep policy/revocation stable through a source commit; no network IO."""
        with file_transaction(self.store.lock):
            yield self.authorize(pair_id, **kwargs)

    def revoke(self, pair_id, *, expected_revision):
        def revoke(data):
            row = self._pair(data, pair_id)
            if row['status'] == 'revoked':
                return row
            if type(expected_revision) is not int or row.get('local_policy', {}).get('revision', 0) != expected_revision:
                raise SyncError('sync_revision_conflict')
            row['revoke_wire'] = self._seal({'kind': REVOKE, 'sender': row['local'],
                'receiver': row['remote']['peer_id'], 'pair_id': pair_id}, row['remote'], channel=REVOKE_CHANNEL)
            row['status'] = 'revoked'
            for field in ('secret', 'offer', 'confirm_wire', 'join_wire'):
                row.pop(field, None)
            return row
        return self.public(self.store.mutate(revoke))

    def receive_revoke(self, wire):
        proof, sender = self._open(wire, channel=REVOKE_CHANNEL)
        value = proof.payload
        if set(value) != {'kind', 'sender', 'receiver', 'pair_id'} or value['kind'] != REVOKE:
            raise SyncError('sync_pairing_invalid')

        def receive(data):
            row = self._pair(data, value['pair_id'])
            if not self._same_identity(row['remote'], sender):
                raise SyncError('sync_pairing_invalid')
            row['status'] = 'revoked'
            for field in ('secret', 'offer', 'confirm_wire', 'join_wire'):
                row.pop(field, None)
            return {'pair_id': row['id'], 'state': 'revoked'}
        return self._reply(wire, sender, **self.store.mutate(receive))

    def retry_removal(self, pair_id):
        row = self._pair(self.store.snapshot(), pair_id)
        wire = row.get('revoke_wire')
        if row['status'] != 'revoked' or wire is None:
            return self.public(row)
        result = self.post_json(row['remote']['endpoint'], '/api/peer/device-sync/revoke', wire)
        response = self._response(wire, result, row['remote'])
        if response.get('pair_id') != pair_id or response.get('state') != 'revoked':
            raise SyncError('sync_pairing_response_invalid')

        def acknowledged(data):
            current = self._pair(data, pair_id)
            if current.get('revoke_wire') == wire:
                current.pop('revoke_wire')
            return current
        return self.public(self.store.mutate(acknowledged))
