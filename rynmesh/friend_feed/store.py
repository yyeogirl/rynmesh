"""Encrypted local feed state; drafts and published snapshots are separate."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path

from cryptography.exceptions import InvalidTag

from ..atomic_io import atomic_write_json, migration_backup, read_json
from ..crypto import canonical_json
from ..file_transactions import file_transaction
from ..services import peer_box

PREVIOUS_VERSION = 'ryn.friend-feed.v1'
VERSION = 'ryn.friend-feed.v2'
CHANNEL = b'rynmesh-friend-feed-storage-v1'
MAX_PLAINTEXT = 12 * 1024 * 1024
MAX_FILE = 17 * 1024 * 1024
MAX_PUBLICATIONS = 512
MAX_SUBSCRIPTIONS = 512
MAX_ERASED_PUBLICATIONS = 10000


class FeedError(ValueError):
    pass


def identity(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise FeedError('feed_identity_invalid')
    return value


def revision(value):
    if type(value) is not int or value < 0:
        raise FeedError('feed_revision_invalid')
    return value


class FeedStore:
    def __init__(self, home, *, messaging_key):
        self.path = Path(home) / 'friend-feed' / 'state.json'
        self.lock = self.path.parent / '.state.lock'
        self.key = messaging_key
        self.pub = peer_box.public_key_b64(messaging_key)

    def _read(self):
        if not self.path.exists():
            return {}, {'version': VERSION, 'publications': {}, 'subscriptions': {}, 'inbox': {}, 'erased_publications': {}}
        try:
            envelope = read_json(self.path, max_bytes=MAX_FILE)
            if not isinstance(envelope, dict) or envelope.get('version') not in {PREVIOUS_VERSION, VERSION}:
                raise FeedError('feed_version_unsupported')
            plain = peer_box.open_sealed(self.key, self.pub, envelope['nonce'], envelope['ciphertext'], info=CHANNEL)
            if len(plain) > MAX_PLAINTEXT:
                raise ValueError
            data = json.loads(plain)
            if not isinstance(data, dict) or data.get('version') != envelope['version']:
                raise FeedError('feed_version_unsupported')
            if any(not isinstance(data.get(key), dict) for key in ('publications', 'subscriptions', 'inbox')):
                raise ValueError
            if len(data['publications']) > MAX_PUBLICATIONS or len(data['subscriptions']) > MAX_SUBSCRIPTIONS:
                raise ValueError
            if data['version'] == VERSION:
                erased = data.get('erased_publications')
                if not isinstance(erased, dict) or len(erased) > MAX_ERASED_PUBLICATIONS:
                    raise ValueError
                for identifier, counter in erased.items():
                    identity(identifier)
                    if revision(counter) < 1 or identifier in data['publications']:
                        raise ValueError
                if 'cleanup' in data:
                    from .cleanup import validate_cleanup

                    validate_cleanup(data['cleanup'])
            elif 'cleanup' in data or 'erased_publications' in data:
                raise FeedError('feed_version_unsupported')
            return envelope, data
        except FeedError:
            raise
        except (OSError, ValueError, TypeError, KeyError, InvalidTag):
            raise FeedError('feed_store_unavailable') from None

    def read(self):
        with file_transaction(self.lock):
            return self._read()[1]

    def mutate(self, operation):
        with file_transaction(self.lock):
            envelope, data = self._read()
            before = canonical_json(data)
            result = operation(data)
            plain = canonical_json(data)
            if len(plain) > MAX_PLAINTEXT:
                raise FeedError('feed_capacity_exhausted')
            if plain != before:
                if envelope.get('version') == PREVIOUS_VERSION and data['version'] == VERSION:
                    if migration_backup(self.path, suffix='.v1.migrated', max_bytes=MAX_FILE) is None:
                        raise FeedError('feed_backup_failed')
                nonce, ciphertext = peer_box.seal(self.key, self.pub, plain, info=CHANNEL)
                atomic_write_json(self.path, {**envelope, 'version': data['version'], 'nonce': nonce, 'ciphertext': ciphertext}, max_bytes=MAX_FILE)
            return deepcopy(result)
