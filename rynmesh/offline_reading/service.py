"""Single supervised download worker with authenticated restart checkpoints."""
from __future__ import annotations

import base64
import hashlib
import threading
import time
import uuid
from copy import deepcopy

from ..file_transactions import file_transaction
from ..services.document_extract import extract_document
from ..services.library_imports import LibraryImportError
from ..services.reader import extract_readable, link_post_target, readable_url
from .cleanup import OfflineCleanup, public, receipt
from .fetch import OfflineError, fetch_resource, image_mime
from .store import MAX_ITEM, MAX_META, MAX_RECORDS, MAX_TOTAL, key_for

MAX_PAGE = 8 * 1024 * 1024
MAX_BODY = 4 * 1024 * 1024
MAX_IMAGE = 2 * 1024 * 1024
MAX_IMAGES = 8
ACTIVE = {'queued', 'downloading', 'verifying', 'cancel_requested'}


class OfflineSources:
    def __init__(self, *, consumption, imports, native, fetch=fetch_resource):
        self.consumption, self.imports, self.native, self.fetch = consumption, imports, native, fetch

    def reference(self, item_id):
        row = next((row for row in self.consumption().list() if row['item_id'] == item_id), None)
        if not row:
            raise OfflineError('offline_item_unavailable')
        item = row['item']
        if item.get('content_kind') in {'video', 'audio', 'image'}:
            raise OfflineError('offline_media_unsupported')
        return {'item_id': item_id, 'title': str(item.get('title', ''))[:300],
                'source': str(item.get('source_title', ''))[:300], 'url': str(item.get('link', ''))[:4096]}

    def prepare(self, reference, check):
        item_id = reference['item_id']
        if item_id.startswith('import:'):
            try:
                body = self.imports().body(item_id[7:])
            except LibraryImportError as exc:
                code = 'offline_version_unsupported' if 'version_unsupported' in str(exc) else 'offline_source_verification_failed'
                raise OfflineError(code) from None
            return {**reference, 'text': body['text'], 'truncated': body['truncated'], 'images': [],
                    'images_omitted': False, 'source_mode': 'independent_local_copy'}
        if reference['url'].startswith(('https://', 'http://')):
            target = readable_url(reference['url'])
            resource = self.fetch(target, max_bytes=MAX_PAGE, check=check)
            # Reuse the reader's link-post resolver, keeping the actual source.
            if 'reddit.com' in target:
                target = link_post_target(resource['data'], base_url=resource['url'])
                if target:
                    resource = self.fetch(target, max_bytes=MAX_PAGE, check=check)
            if resource['mime'] in {'text/plain', 'text/markdown'}:
                text = resource['data'].decode('utf-8')
                article = {'title': reference['title'], 'blocks': [{'tag': 'p', 'text': text}], 'images': []}
            elif resource['mime'] in {'text/html', 'application/xhtml+xml', ''}:
                article = extract_readable(resource['data'], url=resource['url'])
                if article.get('access_required'):
                    raise OfflineError('offline_source_access_required')
            else:
                raise OfflineError('offline_source_format_unsupported')
            if not any(block['tag'] in {'p', 'li', 'blockquote', 'pre'} and block['text'].strip() for block in article['blocks']):
                raise OfflineError('offline_no_readable_body')
            return {**reference, 'url': resource['url'], 'title': article.get('title') or reference['title'],
                    'text': '\n\n'.join(block['text'] for block in article['blocks']), 'truncated': bool(article.get('truncated')),
                    'images': article.get('images', []), 'images_omitted': bool(article.get('images_omitted')),
                    'source_mode': 'public_web'}
        native = self.native()
        signed = native.get_local_manifest(item_id)
        manifest = native._validate_peer_manifest(signed).manifest
        path = native.get_local_content_path(item_id)
        with path.open('rb') as handle:
            raw = handle.read(MAX_PAGE + 1)
        if not manifest or len(raw) > MAX_PAGE or 'sha256:' + hashlib.sha256(raw).hexdigest() != manifest.asset.media_hash:
            raise OfflineError('offline_source_verification_failed')
        result = extract_document(path, max_input_bytes=MAX_PAGE)
        if result['status'] not in {'parsed', 'truncated'}:
            raise OfflineError('offline_document_extraction_failed')
        # Refuse content that changed while the existing document helper read it.
        with path.open('rb') as handle:
            if hashlib.sha256(handle.read(MAX_PAGE + 1)).hexdigest() != hashlib.sha256(raw).hexdigest():
                raise OfflineError('offline_source_verification_failed')
        return {**reference, 'text': result['text'], 'truncated': result['status'] == 'truncated',
                'images': [], 'images_omitted': False, 'source_mode': 'independent_local_copy'}


class OfflineReading:
    def __init__(self, *, store, sources, clock=time.time):
        self.store, self.sources, self.clock = store, sources, clock
        self.running = threading.Lock()
        self.cleanup = OfflineCleanup(store)

    def recover(self):
        def change(data):
            for row in data['records'].values():
                if row['state'] in {'downloading', 'verifying'}:
                    row['state'] = 'queued'
                    row['error_code'] = 'offline_resuming_verified_checkpoints'
                    row['job']['token'] = ''
                elif row['state'] == 'cancel_requested':
                    row['state'] = 'cancelled'
                    row['job']['token'] = ''
        self.store.mutate(change)

    def request(self, item_id, *, update=False):
        if type(update) is not bool:
            raise OfflineError('offline_request_invalid')
        item_key = key_for(item_id)
        existing = self.store.read()['records'].get(item_key)
        if existing and (existing['state'] in ACTIVE or existing.get('current') and not update):
            return self.public(existing)
        reference = self.sources.reference(item_id)

        def change(data):
            prior = data['records'].get(item_key)
            if prior and (prior['state'] in ACTIVE or prior.get('current') and not update):
                return self.public(prior)
            if prior:
                self.store.ensure_supported([(prior.get('current') or {}).get('job_id'), (prior.get('job') or {}).get('id')])
            if not prior and len(data['records']) >= MAX_RECORDS:
                cleared = next((key for key, row in data['records'].items()
                                if row['state'] == 'cleared' and not row.get('current') and not row.get('job')), None)
                if cleared is None:
                    raise OfflineError('offline_record_limit')
                del data['records'][cleared]
            row = {**(prior or {}), 'key': item_key, 'item_id': item_id, 'reference': reference,
                   'state': 'queued', 'error_code': '', 'verified_bytes': 0,
                   'job': {'id': uuid.uuid4().hex, 'token': '', 'created_at': self.clock()},
                   'current': (prior or {}).get('current')}
            data['records'][item_key] = row
            return self.public(row)
        result = self.store.mutate(change)
        self.store.gc()
        return result

    def retry(self, item_id):
        key = key_for(item_id)
        def change(data):
            row = data['records'].get(key)
            if not row or not row.get('job'):
                raise OfflineError('offline_item_unavailable')
            if row['state'] not in ACTIVE:
                row.update(state='queued', error_code='')
                row['job']['token'] = ''
            return self.public(row)
        return self.store.mutate(change)

    def cancel(self, item_id):
        def change(data):
            row = data['records'].get(key_for(item_id))
            if not row:
                raise OfflineError('offline_item_unavailable')
            if row['state'] == 'queued':
                row['state'] = 'cancelled'
            elif row['state'] in {'downloading', 'verifying'}:
                row['state'] = 'cancel_requested'
            return self.public(row)
        return self.store.mutate(change)

    @staticmethod
    def public(row):
        return deepcopy({key: row.get(key) for key in ('key', 'item_id', 'reference', 'state', 'error_code', 'verified_bytes', 'current')})

    def status(self):
        data = self.store.read()
        meta_bytes = self.store.path.stat().st_size if self.store.path.exists() else 0
        return {'records': [self.public(row) for row in data['records'].values()],
                'cleanup': public(receipt(data)),
                'used_bytes': self.store.used() + meta_bytes, 'download_bytes': self.store.used(),
                'limits': {'item_bytes': MAX_ITEM, 'total_bytes': MAX_TOTAL, 'metadata_reserved_bytes': MAX_META,
                           'image_bytes': MAX_IMAGE, 'image_count': MAX_IMAGES},
                'resume_mode': 'verified_checkpoints_only'}

    def read(self, item_id):
        row = self.store.read()['records'].get(key_for(item_id))
        current = (row or {}).get('current')
        if not current:
            raise OfflineError('offline_not_downloaded')
        bundle = self.store.bundle(current['job_id'])
        if bundle.get('item_id') != item_id:
            raise OfflineError('offline_verification_failed')
        return {key: bundle.get(key) for key in ('item_id', 'title', 'source', 'url', 'text', 'truncated', 'images_omitted', 'source_mode')} | {
            'downloaded_at': current['downloaded_at'], 'job_id': current['job_id'], 'partial': current['partial'],
            'images': [{key: image.get(key) for key in ('index', 'alt', 'state', 'error_code', 'mime')} for image in bundle['images']]}

    def resolve(self, item_id):
        """Distinguish no committed download from an unreadable saved copy."""
        key = key_for(item_id)
        with file_transaction(self.store.lock):
            row = self.store.read()['records'].get(key)
            if not (row or {}).get('current'):
                return None
            return {'key': key, 'body': self.read(item_id)}

    def image(self, item_id, index, *, job_id=None):
        if type(index) is not int or not 0 <= index < MAX_IMAGES:
            raise OfflineError('offline_image_unavailable')
        row = self.store.read()['records'].get(key_for(item_id))
        current = (row or {}).get('current')
        if not current:
            raise OfflineError('offline_not_downloaded')
        if job_id is not None and current['job_id'] != job_id:
            raise OfflineError('offline_copy_changed')
        bundle = self.store.bundle(current['job_id'])
        try:
            image = bundle['images'][index]
            raw = base64.b64decode(image['data'], validate=True)
            if image['state'] != 'verified' or hashlib.sha256(raw).hexdigest() != image['sha256'] or image_mime(raw) != image['mime']:
                raise ValueError
        except (KeyError, IndexError, ValueError):
            raise OfflineError('offline_image_unavailable') from None
        return raw, image['mime']

    def _progress(self, key, job_id, token, **updates):
        def change(data):
            self.store.check(key, job_id, token)
            data['records'][key].update(updates)
        self.store.mutate(change)

    @staticmethod
    def _verified_bytes(bundle):
        return len(bundle['text'].encode()) + sum(image.get('size_bytes', 0) for image in bundle['images'] if image['state'] == 'verified')

    def run_once(self):
        if not self.running.acquire(blocking=False):
            return False
        claimed = None
        try:
            def claim(data):
                rows = [row for row in data['records'].values() if row['state'] == 'queued']
                if not rows:
                    return None
                row = min(rows, key=lambda row: row['job']['created_at'])
                row['state'] = 'downloading'
                row['job']['token'] = uuid.uuid4().hex
                return row
            claimed = self.store.mutate(claim)
            if not claimed:
                return False
            key, job_id, token = claimed['key'], claimed['job']['id'], claimed['job']['token']
            def check():
                return self.store.check(key, job_id, token)
            try:
                bundle = self.store.bundle(job_id)
                if bundle.get('item_id') != claimed['item_id']:
                    raise OfflineError('offline_verification_failed')
            except OfflineError as exc:
                if str(exc) == 'offline_version_unsupported':
                    raise
                source = self.sources.prepare(claimed['reference'], check)
                text = source.get('text', '')
                if not isinstance(text, str) or not text.strip():
                    raise OfflineError('offline_no_readable_body') from None
                if len(text.encode()) > MAX_BODY:
                    raise OfflineError('offline_body_too_large') from None
                images = source.get('images', [])
                bundle = {**source, 'body_sha256': hashlib.sha256(text.encode()).hexdigest(),
                    'images_omitted': bool(source.get('images_omitted')) or len(images) > MAX_IMAGES,
                    'images': [{**image, 'index': i, 'state': 'pending', 'error_code': ''} for i, image in enumerate(images[:MAX_IMAGES])]}
                self.store.checkpoint(key, job_id, token, bundle)
            self._progress(key, job_id, token, verified_bytes=self._verified_bytes(bundle))
            for image in bundle['images']:
                check()
                if image['state'] == 'verified':
                    try:
                        raw = base64.b64decode(image['data'], validate=True)
                        if hashlib.sha256(raw).hexdigest() == image['sha256'] and image_mime(raw) == image['mime']:
                            continue
                    except (KeyError, ValueError):
                        pass
                    image.update(state='pending', error_code='offline_verification_failed')
                try:
                    result = self.sources.fetch(image['url'], max_bytes=MAX_IMAGE, check=check)
                    raw = result['data']
                    if len(raw) > MAX_IMAGE:
                        raise OfflineError('offline_resource_too_large')
                    mime = image_mime(raw)
                    image.update(data=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest(),
                                 mime=mime, size_bytes=len(raw), state='verified', error_code='')
                    self.store.checkpoint(key, job_id, token, bundle)
                except OfflineError as exc:
                    if str(exc) == 'offline_cancelled_or_superseded':
                        raise
                    for field in ('data', 'sha256', 'mime', 'size_bytes'):
                        image.pop(field, None)
                    image.update(state='missing', error_code=str(exc))
                    self.store.checkpoint(key, job_id, token, bundle)
                self._progress(key, job_id, token, verified_bytes=self._verified_bytes(bundle))
            self._progress(key, job_id, token, state='verifying', verified_bytes=self._verified_bytes(bundle))
            bundle = self.store.bundle(job_id)
            partial = bool(bundle.get('truncated') or bundle.get('images_omitted') or any(image['state'] != 'verified' for image in bundle['images']))

            def commit(data):
                check()
                row = data['records'][key]
                self.store.ensure_supported([(row.get('current') or {}).get('job_id')])
                row.update(state='partial' if partial else 'ready', error_code='', job=None,
                    current={'job_id': job_id, 'downloaded_at': self.clock(), 'partial': partial,
                             'size_bytes': self.store._path(job_id).stat().st_size},
                    verified_bytes=self._verified_bytes(bundle))
            self.store.mutate(commit)
            self.store.gc()
            return True
        except Exception as exc:
            code = str(exc) if isinstance(exc, OfflineError) else 'offline_download_failed'
            if claimed:
                def fail(data):
                    row = data['records'].get(claimed['key'])
                    job = (row or {}).get('job') or {}
                    if job.get('id') == claimed['job']['id'] and job.get('token') == claimed['job']['token']:
                        row.update(state='cancelled' if row['state'] == 'cancel_requested' else 'failed', error_code=code)
                self.store.mutate(fail)
            if code == 'offline_cancelled_or_superseded':
                return False  # A deliberate cancellation is not a worker failure.
            # Persist a task's safe reason and let the shared supervisor record
            # failures too; an unreadable store must not look like idle work.
            raise OfflineError(code) from None
        finally:
            self.running.release()

    def _clear_preview(self, item_id):
        return self.cleanup.preview(item_id)

    def clear_preview(self, item_id=None):
        if item_id is not None:
            key_for(item_id)
        return self._clear_preview(item_id)

    def clear(self, *, review_token, item_id=None):
        if item_id is not None:
            key_for(item_id)
        return self.cleanup.clear(review_token=review_token, item_id=item_id)

    def clear_remaining_preview(self):
        return self.cleanup.review_remaining()

    def clear_remaining(self, *, review_token):
        return self.cleanup.approve_remaining(review_token=review_token)
