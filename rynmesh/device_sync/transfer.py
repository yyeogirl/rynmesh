"""Authenticated source-to-source batches and current-policy acknowledgements.

The lock order is pairing -> source -> replica. Source commits and receipts
hold policy stable, but no file lock is held while calling a peer. Each batch
contains one scope so a partial failure in another source cannot acknowledge it.
"""
from __future__ import annotations

import json

from ..crypto import canonical_json
from ..file_transactions import file_transaction
from ..services import peer_box
from . import pair_crypto as crypto
from . import records
from .conversation_bridge import ConversationBridge
from .pairing import policy
from .reading_bridge import ReadingBridge
from .records import SyncError, fingerprint
from .store import MAX_BATCH, MAX_BATCH_BYTES

PATH = '/api/peer/device-sync/batch'
MAX_WIRE_BYTES = 18 * 1024 * 1024
DATA = 'ryn.device-data.v1'
ACK = 'ryn.device-data-ack.v1'
CHANNEL = b'rynmesh-device-data-v1:'


class DeviceTransfer:
    def __init__(self, *, pairing, replica, reading, conversations, post_json):
        self.pairing, self.replica = pairing, replica
        self.reading, self.conversations, self.post_json = reading, conversations, post_json
        self.turns = {}

    def _pair(self, pair_id):
        node = self.pairing()
        return node._pair(node.store.snapshot(), pair_id)

    def _guard(self, row, scopes):
        if row['status'] != 'active':
            raise SyncError('sync_device_not_active')
        return self.pairing().authorized(row['id'], remote_actor=row['remote']['actor'], scopes=scopes,
            sender_revision=row['remote_policy']['revision'], receiver_revision=row['local_policy']['revision'])

    @staticmethod
    def _channel(row, *, reply=False):
        # Both the fresh pairing identity and its shared capability bind data.
        return CHANNEL + row['id'].encode() + crypto.decoded(row['secret'], size=32) + (b':ack' if reply else b':batch')

    @staticmethod
    def _epoch(row):
        return fingerprint([policy(row['local_policy']), policy(row['remote_policy'])])

    def _initialize(self, row, scope):
        node = self.pairing()
        epoch = self._epoch(row)
        if row.get('transfer', {}).get('epoch') != epoch:
            # Receipts use the pair ID, never merely the remote actor. Forget
            # before recording the new epoch; interrupted reset safely repeats.
            self.replica.forget_device(row['id'])
            for previous in node.store.snapshot()['pairs'].values():
                if previous['status'] == 'revoked':
                    self.replica.forget_device(previous['id'])
            def reset(data):
                current = node._pair(data, row['id'])
                current['transfer'] = {'epoch': epoch, 'confirmed': {}, 'received': {}, 'errors': {}}
            node.store.mutate(reset)
        if scope == 'conversations':
            source = self.conversations()
            source.enable_sync()
            return ConversationBridge(source, self.replica)
        source = self.reading()
        # This bridge is used only inside the current pairing-policy guard.
        # Combine approved opt-in checks with the source read for the operation.
        return ReadingBridge(source, self.replica, ensure_enabled=True)

    @staticmethod
    def _pending(bridge, pair_id, scope):
        return bridge.pending(pair_id) if scope == 'conversations' else bridge.pending(pair_id, [scope])

    @staticmethod
    def _receive(bridge, rows, scope):
        return bridge.receive(rows) if scope == 'conversations' else bridge.receive(rows, scopes=[scope])

    @staticmethod
    def _acknowledge(bridge, pair_id, receipts, scope):
        return bridge.acknowledge(pair_id, receipts) if scope == 'conversations' else bridge.acknowledge(pair_id, receipts, scopes=[scope])

    def _seal(self, row, payload, *, reply=False):
        node = self.pairing()
        box = crypto.seal(payload, private_key=node.identity_private, messaging_key=node.messaging_key,
                          sender=row['local'], receiver=row['remote'], channel=self._channel(row, reply=reply))
        wire = {'pair_id': row['id'], 'box': box}
        if len(canonical_json(wire)) > MAX_WIRE_BYTES:
            raise SyncError('sync_batch_limit')
        return wire

    def _open(self, wire, *, reply=False):
        if not isinstance(wire, dict) or set(wire) != {'pair_id', 'box'} or len(canonical_json(wire)) > MAX_WIRE_BYTES:
            raise SyncError('sync_batch_invalid')
        row = self._pair(wire['pair_id'])
        if row['status'] != 'active':
            raise SyncError('sync_device_not_active')
        node = self.pairing()
        proof, sender = crypto.open_wire(wire['box'], messaging_key=node.messaging_key,
            allow_loopback=node.allow_loopback, channel=self._channel(row, reply=reply), max_bytes=MAX_WIRE_BYTES)
        value = proof.payload
        if (not node._same_identity(sender, row['remote']) or value.get('receiver') != row['local']['peer_id']
                or value.get('pair_id') != row['id']):
            raise SyncError('sync_batch_invalid')
        return row, value

    @staticmethod
    def _receipts(rows, scope):
        if not isinstance(rows, list) or len(rows) > MAX_BATCH or len(canonical_json(rows)) > MAX_BATCH_BYTES:
            raise SyncError('sync_batch_limit')
        receipts, seen = [], set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'scope', 'id', 'record'} or row['scope'] != scope:
                raise SyncError('sync_scope_denied')
            records.validate(scope, row['id'], row['record'])
            if row['id'] in seen:
                raise SyncError('sync_batch_invalid')
            seen.add(row['id'])
            receipts.append({'scope': scope, 'id': row['id'], 'revision': fingerprint(row['record'])})
        return receipts

    def prepare(self, pair_id, scope):
        records.scope_id(scope)
        row = self._pair(pair_id)
        with self._guard(row, [scope]) as current:
            bridge = self._initialize(current, scope)
            batch = self._pending(bridge, pair_id, scope)
            payload = {'kind': DATA, 'sender': current['local'], 'receiver': current['remote']['peer_id'],
                'pair_id': pair_id, 'scope': scope, 'sender_revision': current['local_policy']['revision'],
                'receiver_revision': current['remote_policy']['revision'], 'records': batch['records']}
            return self._seal(current, payload)

    def receive(self, wire):
        row, value = self._open(wire)
        if set(value) != {'kind', 'sender', 'receiver', 'pair_id', 'scope', 'sender_revision', 'receiver_revision', 'records'} or value['kind'] != DATA:
            raise SyncError('sync_batch_invalid')
        scope = records.scope_id(value['scope'])
        self._receipts(value['records'], scope)
        node = self.pairing()
        with node.authorized(row['id'], remote_actor=row['remote']['actor'], scopes=[scope],
                             sender_revision=value['sender_revision'], receiver_revision=value['receiver_revision']) as current:
            bridge = self._initialize(current, scope)
            receipts = self._receive(bridge, value['records'], scope)
            self._record(current, scope, received=True)
            return self._seal(current, {'kind': ACK, 'sender': current['local'], 'receiver': current['remote']['peer_id'],
                'pair_id': row['id'], 'scope': scope, 'sender_revision': current['local_policy']['revision'],
                'receiver_revision': current['remote_policy']['revision'], 'request_hash': fingerprint(wire),
                'receipts': receipts}, reply=True)

    def _record(self, row, scope, *, received=False, error=''):
        node = self.pairing()
        def update(data):
            current = node._pair(data, row['id'])
            if current['status'] != 'active' or self._epoch(current) != self._epoch(row):
                return
            state = current.setdefault('transfer', {'epoch': self._epoch(row), 'confirmed': {}, 'received': {}, 'errors': {}})
            if error:
                state.setdefault('errors', {})[scope] = error
            elif not received:
                state.setdefault('errors', {}).pop(scope, None)
            if not error:
                state['received' if received else 'confirmed'][scope] = int(node.clock())
        node.store.mutate(update)

    def acknowledge(self, wire, response):
        # Validate the exact sent records as well as the peer's signed response.
        # The outgoing box cannot be opened with _open (its sender is local).
        row, value = self._open(response, reply=True)
        if wire.get('pair_id') != row['id'] or value.get('request_hash') != fingerprint(wire):
            raise SyncError('sync_receipt_invalid')
        return self._accept_receipt(row, value, self._outgoing(row, wire))

    def _outgoing(self, row, wire):
        node = self.pairing()
        box = wire['box']
        plain = peer_box.open_sealed(node.messaging_key, row['remote']['messaging_pub'], box['nonce'], box['ciphertext'], info=self._channel(row))
        proof = crypto.verify(json.loads(plain))
        sent = proof.payload
        if (proof.public_key != row['local']['peer_id'] or sent.get('sender') != row['local']
                or sent.get('receiver') != row['remote']['peer_id'] or sent.get('kind') != DATA
                or sent.get('pair_id') != row['id']):
            raise SyncError('sync_batch_invalid')
        return sent

    def _accept_receipt(self, row, value, sent):
        if set(value) != {'kind', 'sender', 'receiver', 'pair_id', 'scope', 'sender_revision', 'receiver_revision', 'request_hash', 'receipts'} or value['kind'] != ACK:
            raise SyncError('sync_receipt_invalid')
        scope = records.scope_id(value['scope'])
        if (scope != sent['scope'] or value['sender_revision'] != sent['receiver_revision']
                or value['receiver_revision'] != sent['sender_revision']
                or canonical_json(value['receipts']) != canonical_json(self._receipts(sent['records'], scope))):
            raise SyncError('sync_receipt_invalid')
        node = self.pairing()
        with node.authorized(row['id'], remote_actor=row['remote']['actor'], scopes=[scope],
                             sender_revision=value['sender_revision'], receiver_revision=value['receiver_revision']) as current:
            bridge = self._initialize(current, scope)
            result = self._acknowledge(bridge, row['id'], value['receipts'], scope)
            self._record(current, scope)
            return result

    def send(self, pair_id, scope):
        wire = self.prepare(pair_id, scope)
        row = self._pair(pair_id)
        try:
            # Recheck permission immediately before starting network IO. An
            # already in-flight body cannot be recalled by local cancellation.
            sent = self._outgoing(row, wire)
            with self.pairing().authorized(pair_id, remote_actor=row['remote']['actor'], scopes=[scope],
                    sender_revision=sent['receiver_revision'], receiver_revision=sent['sender_revision']):
                pass
            response = self.post_json(row['remote']['endpoint'], PATH, wire)
            return self.acknowledge(wire, response)
        except Exception:
            with file_transaction(self.pairing().store.lock):
                self._record(row, scope, error='sync_transfer_unconfirmed')
            raise

    def run_once(self, pair_id):
        row = self._pair(pair_id)
        scopes = self.pairing().public(row)['effective_scopes']
        if not scopes:
            return False
        index = self.turns.get(pair_id, 0) % len(scopes)
        self.turns[pair_id] = index + 1
        return bool(self.send(pair_id, scopes[index])['acknowledged'])

    def status(self, pair_id):
        row = self._pair(pair_id)
        public = self.pairing().public(row)
        base = {'pending': None, 'last_success_at': None, 'error_code': '', 'conflicts': 0}
        if public['status'] != 'active':
            return {**base, 'state': 'unpaired'}
        if public['paused'] or public['remote_paused']:
            return {**base, 'state': 'paused'}
        scopes = public['effective_scopes']
        if not scopes:
            return {**base, 'state': 'no_scope'}
        try:
            with self._guard(row, scopes) as current:
                pending, conflicts = 0, 0
                for scope in scopes:
                    bridge = self._initialize(current, scope)
                    state = bridge.status(pair_id) if scope == 'conversations' else bridge.status(pair_id, [scope])
                    pending += state['pending']
                    conflicts += state['conflicts']
                    current = self._pair(pair_id)  # _initialize may have reset the epoch.
                saved = current.get('transfer', {})
                stamps = saved.get('confirmed', {})
                last = min(stamps[scope] for scope in scopes) if all(scope in stamps for scope in scopes) else None
                error = next((saved.get('errors', {}).get(scope) for scope in scopes if saved.get('errors', {}).get(scope)), '')
                state = 'conflict' if conflicts else 'waiting' if error else 'pending' if pending or last is None else 'confirmed'
                return {'state': state, 'pending': pending, 'last_success_at': last, 'error_code': error, 'conflicts': conflicts}
        except Exception:
            return {**base, 'state': 'failed', 'error_code': 'sync_storage_unavailable'}
