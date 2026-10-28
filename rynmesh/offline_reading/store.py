"""Atomic encrypted download bundles and metadata; only committed copies open."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from copy import deepcopy
from pathlib import Path

from cryptography.exceptions import InvalidTag

from ..atomic_io import atomic_write_json, read_json
from ..crypto import canonical_json
from ..file_transactions import file_transaction
from ..services import peer_box
from .fetch import OfflineError

VERSION = 'ryn.offline-reading.v1'
CHANNEL = b'rynmesh-offline-reading-v1'
MAX_META = 4 * 1024 * 1024
MAX_ITEM = 16 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_RECORDS = 512


def key_for(item_id):
    if not isinstance(item_id, str) or not 1 <= len(item_id.encode()) <= 512:
        raise OfflineError('offline_item_invalid')
    return hashlib.sha256(item_id.encode()).hexdigest()


class OfflineStore:
    def __init__(self, home, *, messaging_key):
        self.root = Path(home).resolve() / 'offline-reading'
        self.path = self.root / 'state.json'
        self.lock = self.root / '.state.lock'
        self.downloads = self.root / 'downloads'
        self.key, self.pub = messaging_key, peer_box.public_key_b64(messaging_key)

    def _path(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch('[a-f0-9]{32}', job_id):
            raise OfflineError('offline_copy_unavailable')
        path = (self.downloads / (job_id + '.json')).resolve()
        if self.downloads.resolve().parent != self.root.resolve() or path.parent != self.downloads.resolve():
            raise OfflineError('offline_copy_unavailable')
        return path

    def _read(self, path, max_bytes):
        try:
            envelope = read_json(path, max_bytes=max_bytes)
            if not isinstance(envelope, dict) or envelope.get('version') != VERSION:
                raise OfflineError('offline_version_unsupported')
            plain = peer_box.open_sealed(self.key, self.pub, envelope['nonce'], envelope['ciphertext'], info=CHANNEL)
            value = json.loads(plain)
            if not isinstance(value, dict) or value.get('version') != VERSION:
                raise OfflineError('offline_version_unsupported')
            return envelope, value
        except OfflineError:
            raise
        except (OSError, ValueError, TypeError, KeyError, InvalidTag):
            raise OfflineError('offline_copy_unavailable') from None

    def _encode(self, value, prior=None):
        nonce, ciphertext = peer_box.seal(self.key, self.pub, canonical_json(value), info=CHANNEL)
        return {**(prior or {}), 'version': VERSION, 'nonce': nonce, 'ciphertext': ciphertext}

    def _state(self):
        if not self.path.exists():
            return {}, {'version': VERSION, 'records': {}}
        envelope, value = self._read(self.path, MAX_META)
        if not isinstance(value.get('records'), dict) or len(value['records']) > MAX_RECORDS:
            raise OfflineError('offline_store_invalid')
        from .cleanup import receipt
        receipt(value)
        return envelope, value

    def read(self):
        with file_transaction(self.lock):
            return self._state()[1]

    def mutate(self, operation):
        with file_transaction(self.lock):
            envelope, data = self._state()
            before = canonical_json(data)
            result = operation(data)
            if canonical_json(data) != before:
                encoded = self._encode(data, envelope)
                if len(canonical_json(encoded)) > MAX_META:
                    raise OfflineError('offline_metadata_limit')
                atomic_write_json(self.path, encoded, max_bytes=MAX_META)
            return deepcopy(result)

    def used(self):
        return sum(path.stat().st_size for path in self.downloads.glob('*.json') if path.is_file())

    def bundle(self, job_id):
        _, value = self._read(self._path(job_id), MAX_ITEM)
        if value.get('job_id') != job_id or not isinstance(value.get('text'), str) or not value['text'].strip():
            raise OfflineError('offline_copy_unavailable')
        if hashlib.sha256(value['text'].encode()).hexdigest() != value.get('body_sha256'):
            raise OfflineError('offline_verification_failed')
        return value

    def checkpoint(self, item_key, job_id, token, bundle):
        with file_transaction(self.lock):
            self.check(item_key, job_id, token)
            path = self._path(job_id)
            try:
                prior = self._read(path, MAX_ITEM)[0] if path.exists() else {}
            except OfflineError as exc:
                if str(exc) == 'offline_version_unsupported':
                    raise
                prior = {}  # A corrupt, uncommitted checkpoint may be rebuilt.
            encoded = self._encode({**bundle, 'version': VERSION, 'job_id': job_id}, prior)
            size = len(canonical_json(encoded)) + 256  # JSON writer whitespace allowance.
            if size > MAX_ITEM:
                raise OfflineError('offline_item_limit')
            # Include the old checkpoint while atomic replacement is in flight.
            if self.used() + size > MAX_TOTAL - MAX_META:
                raise OfflineError('offline_storage_limit')
            self.root.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(self.root).free < size + 65536:
                raise OfflineError('offline_disk_full')
            atomic_write_json(path, encoded, max_bytes=MAX_ITEM)
            self.bundle(job_id)  # Re-open and authenticate the durable checkpoint.

    def check(self, item_key, job_id, token):
        row = self.read()['records'].get(item_key)
        job = (row or {}).get('job') or {}
        if job.get('id') != job_id or job.get('token') != token or row.get('state') == 'cancel_requested':
            raise OfflineError('offline_cancelled_or_superseded')
        return row

    def ensure_supported(self, job_ids):
        """Preflight before dropping metadata references to any future bundle."""
        for job_id in set(job_ids) - {None}:
            try:
                self._read(self._path(job_id), MAX_ITEM)
            except OfflineError as exc:
                if str(exc) == 'offline_version_unsupported':
                    raise
                # Missing/corrupt downloads remain eligible for owner cleanup.

    def gc(self):
        """Only orphan files owned by this package; all targets resolve in root."""
        with file_transaction(self.lock):
            data = self._state()[1]
            retained = {(row.get('current') or {}).get('job_id') for row in data['records'].values()}
            retained.update((row.get('job') or {}).get('id') for row in data['records'].values())
            from .cleanup import receipt
            cleanup = receipt(data)
            if cleanup and not cleanup['done']:
                retained.update(row['job_id'] for row in cleanup['files'])
            for candidate in self.downloads.glob('*.json'):
                if not re.fullmatch('[a-f0-9]{32}', candidate.stem) or candidate.stem in retained:
                    continue
                path = self._path(candidate.stem)
                # Preserve unknown orphan formats; never fail after committing
                # a new copy merely because an unrelated future file exists.
                try:
                    self.ensure_supported([candidate.stem])
                except OfflineError:
                    continue
                path.unlink(missing_ok=True)
