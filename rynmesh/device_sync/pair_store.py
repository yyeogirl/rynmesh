"""Encrypted atomic pairing state; independent of friend and AI permissions."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from ..atomic_io import atomic_write_json, read_json
from ..crypto import canonical_json
from ..file_transactions import file_transaction
from ..services import peer_box
from . import pair_crypto as crypto
from .records import MAX_COUNTER, SyncError, actor_id, fingerprint

VERSION = 'ryn.device-pairings.v1'
CHANNEL = b'rynmesh-device-pairing-storage-v1'
MAX_PLAINTEXT = 8 * 1024 * 1024
MAX_FILE = 12 * 1024 * 1024
MAX_RECORDS = 256
STATUSES = {'awaiting_owner', 'awaiting_inviter', 'awaiting_peer', 'awaiting_ack', 'active', 'rejected', 'revoked'}


class PairingStore:
    def __init__(self, home, messaging_key):
        self.path = Path(home) / 'device-sync' / 'pairings.json'
        self.lock = self.path.parent / '.pairings.lock'
        self.key = messaging_key
        self.pub = peer_box.public_key_b64(messaging_key)
        self.actor = fingerprint(self.pub)

    def _read(self):
        if not self.path.exists():
            return {}, {'version': VERSION, 'actor': self.actor, 'invites': {}, 'pairs': {}}
        try:
            envelope = read_json(self.path, max_bytes=MAX_FILE)
            if not isinstance(envelope, dict) or envelope.get('version') != VERSION:
                raise SyncError('sync_version_unsupported')
            plain = peer_box.open_sealed(self.key, self.pub, envelope['nonce'], envelope['ciphertext'], info=CHANNEL)
            if len(plain) > MAX_PLAINTEXT:
                raise ValueError
            data = json.loads(plain)
            if not isinstance(data, dict) or data.get('version') != VERSION:
                raise SyncError('sync_version_unsupported')
            if data.get('actor') != self.actor:
                raise SyncError('sync_device_identity_changed')
            self._validate(data)
            return envelope, data
        except SyncError:
            raise
        except Exception:
            raise SyncError('sync_pairing_store_unavailable') from None

    def _validate(self, data):
        if data.get('version') != VERSION:
            raise SyncError('sync_version_unsupported')
        if data.get('actor') != self.actor:
            raise SyncError('sync_device_identity_changed')
        for section in ('invites', 'pairs'):
            if not isinstance(data.get(section), dict) or len(data[section]) > MAX_RECORDS:
                raise SyncError('sync_pairing_capacity_exhausted')
        for identifier, pair in data['pairs'].items():
            actor_id(identifier)
            if not isinstance(pair, dict) or pair.get('id') != identifier or pair.get('status') not in STATUSES:
                raise SyncError('sync_pairing_store_unavailable')
            if pair.get('role') not in {'inviter', 'joiner'}:
                raise SyncError('sync_pairing_store_unavailable')
            for side in ('local', 'remote'):
                crypto.identity(pair.get(side), allow_loopback=True)
            if pair['local']['actor'] != self.actor or type(pair.get('expires')) is not int:
                raise SyncError('sync_pairing_store_unavailable')
            crypto.identifier(pair.get('invite_id'))
            if crypto.selected(pair.get('requested_scopes')) != pair['requested_scopes']:
                raise SyncError('sync_pairing_store_unavailable')
            for side in ('local_policy', 'remote_policy'):
                value = pair.get(side)
                if value is None and pair['status'] not in {'awaiting_peer', 'awaiting_ack', 'active'}:
                    continue
                if (not isinstance(value, dict) or set(value) != {'revision', 'scopes', 'paused'}
                        or type(value['revision']) is not int or not 1 <= value['revision'] <= MAX_COUNTER
                        or type(value['paused']) is not bool or crypto.selected(value['scopes']) != value['scopes']):
                    raise SyncError('sync_pairing_store_unavailable')
            if pair['status'] in {'awaiting_peer', 'awaiting_ack', 'active'}:
                crypto.decoded(pair.get('secret'), size=32)
                actor_id(pair.get('approval_hash'))
            if pair['status'] == 'awaiting_owner' and pair['role'] != 'inviter':
                raise SyncError('sync_pairing_store_unavailable')
            if pair['status'] in {'awaiting_inviter', 'awaiting_ack'} and pair['role'] != 'joiner':
                raise SyncError('sync_pairing_store_unavailable')
        for identifier, invite in data['invites'].items():
            crypto.identifier(identifier)
            if not isinstance(invite, dict) or invite.get('status') not in {'open', 'claimed', 'cancelled'}:
                raise SyncError('sync_pairing_store_unavailable')
            proof = crypto.verify(invite.get('proof'))
            payload = proof.payload
            inviter = crypto.identity(payload.get('inviter'), allow_loopback=True)
            if (payload.get('kind') != crypto.INVITE or payload.get('id') != identifier
                    or proof.public_key != inviter['peer_id'] or inviter['actor'] != self.actor
                    or type(payload.get('created')) is not int or type(payload.get('expires')) is not int
                    or not 60 <= payload['expires'] - payload['created'] <= 3600
                    or crypto.selected(payload.get('scopes')) != payload['scopes']):
                raise SyncError('sync_pairing_store_unavailable')
            actor_id(payload.get('secret_hash'))
            if invite.get('pair_id') is not None:
                pair = data['pairs'].get(invite['pair_id'])
                if pair is None or pair['role'] != 'inviter' or pair['invite_id'] != identifier:
                    raise SyncError('sync_pairing_store_unavailable')

    def mutate(self, operation):
        with file_transaction(self.lock):
            envelope, data = self._read()
            before = canonical_json(data)
            result = operation(data)
            self._validate(data)
            plaintext = canonical_json(data)
            if len(plaintext) > MAX_PLAINTEXT:
                raise SyncError('sync_pairing_capacity_exhausted')
            if plaintext != before:
                nonce, ciphertext = peer_box.seal(self.key, self.pub, plaintext, info=CHANNEL)
                atomic_write_json(self.path, {**envelope, 'version': VERSION, 'nonce': nonce, 'ciphertext': ciphertext}, max_bytes=MAX_FILE)
            return deepcopy(result)

    def snapshot(self):
        with file_transaction(self.lock):
            return deepcopy(self._read()[1])
