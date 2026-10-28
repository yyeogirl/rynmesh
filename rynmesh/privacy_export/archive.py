"""Bounded ZIP generation with an explicit scope and integrity manifest."""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import threading
import time
import zipfile

MAX_ENTRY = 256 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
MAX_ENTRIES = 40000
SCOPES = {
    'first_reading': 'First-reading progress',
    'reading': 'Bookmarks, reading history and conflict choices',
    'conversations': 'Ask Ryn conversations, drafts and recovery branches',
    'friends': 'Friend relationships and saved sharing cards',
    'friend_feed': 'Publication drafts, audiences, subscriptions and received updates',
    'saved_documents': 'Saved documents, original files and extracted text',
    'offline_reading': 'Download status and verified offline articles and images',
    'devices': 'Device relationships and local sync copies',
    'ai_permissions': 'Friend AI permission choices',
}
EXCLUDED = [
    'Private keys, API credentials, invite secrets and authentication capabilities',
    'Models, runtime installation files and internal task or settlement journals',
    'Search/discovery caches, raw storage backups and unfinished download checkpoints',
    'Other devices, browser databases and open browser views',
    'Legacy friend messages and attachments outside saved sharing cards',
    'Recommendation preferences and audit history: use the separate reading/preferences JSON export',
]


class ExportError(ValueError):
    def __init__(self, code, scope=None):
        super().__init__(code)
        self.code, self.scope = code, scope


class Archive:
    def __init__(self, file, manifest, release):
        self.file, self.manifest, self.release = file, manifest, release

    def close(self):
        if self.release is not None:
            try:
                self.file.close()
            finally:
                release, self.release = self.release, None
                release()

    def chunks(self):
        while chunk := self.file.read(64 * 1024):
            yield chunk


class ExportBuilder:
    """One export per node until its download finishes; no permanent archive."""
    def __init__(self, sources):
        self.sources = sources
        self.running = threading.Lock()

    @staticmethod
    def validate(scopes):
        if (not isinstance(scopes, list) or not scopes or len(scopes) > len(SCOPES)
                or any(not isinstance(scope, str) or scope not in SCOPES for scope in scopes)
                or len(set(scopes)) != len(scopes)):
            raise ExportError('privacy_export_scopes_invalid')
        return [scope for scope in SCOPES if scope in scopes]

    def build(self, scopes):
        scopes = self.validate(scopes)
        if not self.running.acquire(blocking=False):
            raise ExportError('privacy_export_busy')
        file = None
        try:
            # The rolled-over file is private and automatically deleted on close.
            file = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode='w+b')
            manifest = {'version': 'ryn.product-data-export.v1', 'started_at_unix': time.time(),
                        'selected_scopes': scopes, 'excluded': EXCLUDED, 'files': [], 'scopes': [],
                        'snapshot': 'Each scope is read locally during the recorded interval; this is not a global transaction.',
                        'restore': 'Portable reference export. No automatic import, task replay or credential restoration.'}
            total, names = 0, set()
            with zipfile.ZipFile(file, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                for scope in scopes:
                    started = time.time()
                    try:
                        for relative, payload in self.sources(scope):
                            name = scope + '/' + relative
                            if (not re.fullmatch(r'[a-z0-9_./-]+', name) or any(part in {'', '.', '..'} for part in name.split('/'))
                                    or name in names or len(names) >= MAX_ENTRIES):
                                raise ExportError('privacy_export_invalid_entry', scope)
                            if not isinstance(payload, bytes):
                                payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
                            if len(payload) > MAX_ENTRY or total + len(payload) > MAX_TOTAL:
                                raise ExportError('privacy_export_capacity_exhausted', scope)
                            archive.writestr(name, payload)
                            names.add(name)
                            total += len(payload)
                            manifest['files'].append({'path': name, 'bytes': len(payload),
                                                      'sha256': hashlib.sha256(payload).hexdigest()})
                    except ExportError:
                        raise
                    except Exception:
                        # Source errors can contain paths, URLs or private content.
                        raise ExportError('privacy_export_source_unavailable', scope) from None
                    manifest['scopes'].append({'scope': scope, 'started_at_unix': started,
                                               'finished_at_unix': time.time(), 'status': 'included'})
                manifest.update(finished_at_unix=time.time(), total_uncompressed_bytes=total)
                archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, allow_nan=False).encode())
            file.seek(0)
            return Archive(file, manifest, self.running.release)
        except BaseException:
            try:
                if file is not None:
                    file.close()
            finally:
                self.running.release()
            raise
