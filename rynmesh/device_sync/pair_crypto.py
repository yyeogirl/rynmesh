"""Signed device identities and domain-separated encrypted pairing messages."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re

from ..crypto import SignedPayload, canonical_json, sign_payload, verify_signed_payload
from ..friends.crypto import validate_endpoint
from ..services import peer_box
from .records import SyncError, fingerprint, scope_id

INVITE = 'ryn.device-invite.v1'
CHANNEL = b'rynmesh-device-pairing-v1'
MAX_WIRE_BYTES = 64 * 1024
_ID = re.compile(r'^[a-f0-9]{32}$')


def identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise SyncError('sync_pairing_invalid')
    return value


def encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def decoded(value, *, size=None):
    try:
        if not isinstance(value, str) or len(value) > MAX_WIRE_BYTES:
            raise ValueError
        result = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
        if encoded(result) != value or (size is not None and len(result) != size):
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise SyncError('sync_pairing_invalid') from None


def selected(value):
    if not isinstance(value, list) or len(value) > 3 or len({scope_id(item) for item in value}) != len(value):
        raise SyncError('sync_scope_invalid')
    return sorted(value)


def identity(value, *, allow_loopback=False):
    try:
        if not isinstance(value, dict) or set(value) != {'peer_id', 'messaging_pub', 'actor', 'name', 'endpoint'}:
            raise ValueError
        for key in ('peer_id', 'messaging_pub'):
            raw = base64.b64decode(value[key], validate=True)
            if len(raw) != 32 or base64.b64encode(raw).decode() != value[key]:
                raise ValueError
        if value['actor'] != fingerprint(value['messaging_pub']):
            raise ValueError
        if not isinstance(value['name'], str) or not 0 < len(value['name'].strip()) <= 128 or any(ord(char) < 32 for char in value['name']):
            raise ValueError
        if validate_endpoint(value['endpoint'], allow_loopback=allow_loopback) != value['endpoint']:
            raise ValueError
        return dict(value)
    except (ValueError, TypeError, KeyError):
        raise SyncError('sync_pairing_identity_invalid') from None


def verify(value):
    try:
        if not isinstance(value, dict) or set(value) != {'alg', 'public_key', 'signature', 'payload'}:
            raise ValueError
        proof = SignedPayload.from_dict(value)
        verify_signed_payload(proof)
        return proof
    except (ValueError, TypeError, KeyError):
        raise SyncError('sync_pairing_invalid') from None


def secret_hash(value):
    return hashlib.sha256(b'rynmesh-device-invite-secret-v1:' + value).hexdigest()


def confirmation(secret, review):
    return hmac.new(secret, b'rynmesh-device-confirm-v1:' + review.encode(), hashlib.sha256).hexdigest()


def invitation(uri, *, now, allow_loopback=False, allow_expired=False):
    if not isinstance(uri, str) or not uri.startswith('rynmesh://device-pair/') or len(uri) > 32768:
        raise SyncError('sync_invite_invalid')
    try:
        envelope = json.loads(decoded(uri.removeprefix('rynmesh://device-pair/')))
        if not isinstance(envelope, dict) or set(envelope) != {'proof', 'secret'}:
            raise ValueError
        proof = verify(envelope['proof'])
        payload = proof.payload
        if set(payload) != {'kind', 'id', 'inviter', 'scopes', 'created', 'expires', 'secret_hash'} or payload['kind'] != INVITE:
            raise ValueError
        identifier(payload['id'])
        inviter = identity(payload['inviter'], allow_loopback=allow_loopback)
        if proof.public_key != inviter['peer_id'] or selected(payload['scopes']) != payload['scopes']:
            raise ValueError
        if type(payload['created']) is not int or type(payload['expires']) is not int or not 60 <= payload['expires'] - payload['created'] <= 3600:
            raise ValueError
        secret = decoded(envelope['secret'], size=32)
        if not hmac.compare_digest(str(payload['secret_hash']), secret_hash(secret)):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise SyncError('sync_invite_invalid') from None
    if payload['expires'] <= now and not allow_expired:
        raise SyncError('sync_invite_expired')
    return proof, secret


def seal(payload, *, private_key, messaging_key, sender, receiver, channel=CHANNEL):
    proof = sign_payload(payload, private_key_bytes=private_key).to_dict()
    nonce, ciphertext = peer_box.seal(messaging_key, receiver['messaging_pub'], canonical_json(proof), info=channel)
    return {'kind': 'ryn.device-box.v1', 'sender': sender, 'nonce': nonce, 'ciphertext': ciphertext}


def open_wire(wire, *, messaging_key, allow_loopback=False, channel=CHANNEL, max_bytes=MAX_WIRE_BYTES):
    try:
        if not isinstance(wire, dict) or set(wire) != {'kind', 'sender', 'nonce', 'ciphertext'} or wire['kind'] != 'ryn.device-box.v1':
            raise ValueError
        if len(canonical_json(wire)) > max_bytes:
            raise ValueError
        sender = identity(wire['sender'], allow_loopback=allow_loopback)
        plaintext = peer_box.open_sealed(messaging_key, sender['messaging_pub'], wire['nonce'], wire['ciphertext'], info=channel)
        proof = verify(json.loads(plaintext))
        if proof.public_key != sender['peer_id'] or proof.payload.get('sender') != sender:
            raise ValueError
        return proof, sender
    except Exception:
        # Do not expose ciphertext, invitation credentials or peer-supplied errors.
        raise SyncError('sync_pairing_invalid') from None
