"""Causal registers for the three explicitly selected personal-data scopes.

Each entity keeps a version vector and its concurrent values. A merge retains a
value only when it is still present at both replicas or has not been observed by
the other replica. The vector survives deletion, so an old snapshot cannot
resurrect a removed value. Wall-clock timestamps never order these operations.
"""
from __future__ import annotations

import hashlib
import math
import re
from copy import deepcopy
from functools import lru_cache
from urllib.parse import quote, urlsplit

from ..ask_ryn.store import clean_conversation
from ..crypto import canonical_json

SCOPES = frozenset({'bookmarks', 'reading', 'conversations'})
MAX_ACTORS = 64
MAX_COUNTER = 2**53 - 1
MAX_RECORD_BYTES = 8 * 1024 * 1024
_ACTOR = re.compile(r'^[a-f0-9]{64}$')
_CONTROL = re.compile(r'[\x00-\x1f]')
_FINAL = frozenset({'complete', 'failed', 'cancelled', 'interrupted'})


class SyncError(ValueError):
    pass


def actor_id(value):
    if not isinstance(value, str) or not _ACTOR.fullmatch(value):
        raise SyncError('sync_actor_invalid')
    return value


def entity_id(value):
    if not isinstance(value, str) or not 0 < len(value) <= 256 or _CONTROL.search(value):
        raise SyncError('sync_entity_invalid')
    return value


def scope_id(value):
    if not isinstance(value, str) or value not in SCOPES:
        raise SyncError('sync_scope_invalid')
    return value


def fingerprint(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


@lru_cache(maxsize=30000)
def _entity_key(scope, identifier):
    return fingerprint([scope, identifier])


def entity_key(scope, identifier):
    """Bounded memo of immutable identifiers, not record content or validity.

    Validate before the memo lookup, including malformed unhashable inputs.
    Every content value continues through its own canonical-byte validation.
    """
    return _entity_key(scope_id(scope), entity_id(identifier))


def conflict_count(rows):
    """Count distinct concurrent values in already validated source records."""
    return sum(len(heads := row['record']['heads']) > 1 and
               len({fingerprint(head['value']) for head in heads}) > 1 for row in rows)


def clean_value(scope, identifier, value):
    """Project local records to allowed data. Never copy arbitrary metadata."""
    scope_id(scope)
    entity_id(identifier)
    if value is None:  # A causal deletion, distinct from an absent entity.
        return None
    if not isinstance(value, dict):
        raise SyncError('sync_value_invalid')
    if scope == 'conversations':
        try:
            result = clean_conversation(value)
        except (ValueError, TypeError, KeyError):
            raise SyncError('sync_conversation_invalid') from None
        if result['id'] != identifier:
            raise SyncError('sync_entity_invalid')
        result.pop('draft', None)
        # Unfinished requests and their associated user input do not become
        # resumable remote tasks. No order, credentials or runtime state moves.
        unfinished = {row.get('taskId') for row in result['messages'] if row['status'] not in _FINAL and row.get('taskId')}
        original_count = len(result['messages'])
        result['messages'] = [row for row in result['messages'] if row['status'] in _FINAL and row.get('taskId') not in unfinished]
        if len(result['messages']) != original_count:
            # The run service derives the initial title from its question. Do
            # not leak a filtered in-flight request through title or timestamp.
            first = next((row['content'] for row in result['messages'] if row['role'] == 'user'), 'Conversation')
            result['title'] = ''.join(char if ord(char) >= 32 else ' ' for char in first)[:512] or 'Conversation'
            result['updatedAt'] = result['messages'][-1]['createdAt'] if result['messages'] else result['createdAt']
        return result
    item = value.get('item')
    if not isinstance(item, dict) or item.get('item_id') != identifier:
        raise SyncError('sync_item_invalid')
    metadata = {'item_id': identifier}
    # Body, snippets, thumbnails, private chat and model inputs aren't metadata.
    for key, limit in (('title', 4000), ('source_title', 4000), ('source_id', 4000), ('content_kind', 32), ('link', 4096)):
        text = item.get(key, '')
        if not isinstance(text, str) or len(text) > limit or _CONTROL.search(text):
            raise SyncError('sync_item_invalid')
        metadata[key] = text
    try:
        link = urlsplit(metadata['link'])
        public = link.scheme in {'http', 'https'} and bool(link.hostname) and link.username is None and link.password is None
        local = metadata['link'] == 'rynmesh://content/' + quote(identifier, safe='')
        if not public and not local:
            raise ValueError
    except ValueError:
        raise SyncError('sync_item_link_invalid') from None
    result = {'item': metadata}
    if scope == 'bookmarks':
        if type(value.get('bookmarked')) is not bool:
            raise SyncError('sync_value_invalid')
        result['bookmarked'] = value['bookmarked']
    else:
        progress = value.get('progress')
        if type(progress) not in {float, int} or not math.isfinite(progress) or not 0 <= progress <= 1 or type(value.get('completed')) is not bool:
            raise SyncError('sync_value_invalid')
        version = value.get('content_version', '')
        if not isinstance(version, str) or len(version) > 256 or _CONTROL.search(version):
            raise SyncError('sync_value_invalid')
        result.update(progress=round(progress, 4), completed=value['completed'], content_version=version)
    return result


def empty():
    return {'clock': {}, 'heads': [], 'erased': False}


def validate(scope, identifier, record):
    scope_id(scope)
    entity_id(identifier)
    if not isinstance(record, dict) or set(record) != {'clock', 'heads', 'erased'}:
        raise SyncError('sync_record_invalid')
    clock, heads = record['clock'], record['heads']
    if not isinstance(clock, dict) or len(clock) > MAX_ACTORS or not isinstance(heads, list) or len(heads) > MAX_ACTORS or type(record['erased']) is not bool:
        raise SyncError('sync_record_invalid')
    for actor, counter in clock.items():
        actor_id(actor)
        if type(counter) is not int or not 1 <= counter <= MAX_COUNTER:
            raise SyncError('sync_clock_invalid')
    seen = set()
    for head in heads:
        if not isinstance(head, dict) or set(head) != {'actor', 'counter', 'value'}:
            raise SyncError('sync_record_invalid')
        actor = actor_id(head['actor'])
        if actor in seen or type(head['counter']) is not int or head['counter'] != clock.get(actor):
            raise SyncError('sync_clock_invalid')
        seen.add(actor)
        if clean_value(scope, identifier, head['value']) != head['value']:
            raise SyncError('sync_value_invalid')
    if record['erased'] and (scope != 'conversations' or heads):
        raise SyncError('sync_record_invalid')
    if len(canonical_json(record)) > MAX_RECORD_BYTES:
        raise SyncError('sync_record_limit')
    _binding(scope, heads)
    return record


def validate_cached(scope, identifier, record, cache, *, max_entries):
    """Reuse validation only for the same scope, ID and canonical record bytes.

    Local store instances retain bounded fingerprints, never record values.
    This does not cache file reads/decryption, use file timestamps, or bypass
    envelope/identity checks. Wire input continues to use validate directly.
    """
    digest = fingerprint(record)
    key = (scope, identifier, digest)
    if key not in cache:
        validate(scope, identifier, record)
        if len(cache) >= max_entries:
            cache.clear()
        cache.add(key)
    return digest


def _binding(scope, heads):
    if scope != 'conversations':
        return
    bindings = {tuple(head['value'][key] for key in ('serviceKey', 'providerPeerId', 'networkId', 'createdAt'))
                for head in heads if head['value'] is not None}
    if len(bindings) > 1:
        raise SyncError('sync_service_binding_mismatch')


def write(scope, identifier, previous, actor, value, *, erase=False):
    validate(scope, identifier, previous)
    actor_id(actor)
    if previous['erased']:
        raise SyncError('sync_entity_erased')
    if type(erase) is not bool or erase and scope != 'conversations':
        raise SyncError('sync_record_invalid')
    value = clean_value(scope, identifier, value)
    if scope == 'conversations' and value is not None and any(head['value'] is None for head in previous['heads']):
        raise SyncError('sync_conversation_deleted')
    _binding(scope, previous['heads'] + [{'value': value}])
    clock = dict(previous['clock'])
    clock[actor] = clock.get(actor, 0) + 1
    result = {'clock': clock, 'heads': [] if erase else [{'actor': actor, 'counter': clock[actor], 'value': value}], 'erased': erase}
    validate(scope, identifier, result)
    return result


def merge(scope, identifier, left, right):
    validate(scope, identifier, left)
    validate(scope, identifier, right)
    clock = {actor: max(left['clock'].get(actor, 0), right['clock'].get(actor, 0)) for actor in left['clock'] | right['clock']}
    if len(clock) > MAX_ACTORS:
        raise SyncError('sync_device_limit')
    if left['erased'] or right['erased']:
        return {'clock': clock, 'heads': [], 'erased': True}
    maps = [{(head['actor'], head['counter']): head for head in side['heads']} for side in (left, right)]
    for dot in maps[0].keys() & maps[1].keys():
        if canonical_json(maps[0][dot]) != canonical_json(maps[1][dot]):
            raise SyncError('sync_dot_conflict')
    heads = {}
    for index, other in ((0, right), (1, left)):
        for dot, head in maps[index].items():
            if dot in maps[1-index] or other['clock'].get(dot[0], 0) < dot[1]:
                heads[dot] = head
    result = {'clock': clock, 'heads': [deepcopy(heads[dot]) for dot in sorted(heads)], 'erased': False}
    validate(scope, identifier, result)
    return result


def view(scope, identifier, record):
    validate(scope, identifier, record)
    options = {}
    for head in record['heads']:
        options.setdefault(fingerprint(head['value']), {'choice_id': f"{head['actor']}:{head['counter']}", 'value': deepcopy(head['value'])})
    candidates = [options[key] for key in sorted(options)]
    result = {'id': identifier, 'revision': fingerprint(record), 'conflict': len(candidates) > 1, 'erased': record['erased']}
    if scope == 'bookmarks':
        result.update(bookmarked=bool(candidates) and all(row['value'] is not None and row['value']['bookmarked'] for row in candidates),
                      candidates=candidates)
    elif scope == 'reading':
        result.update(candidates=candidates, value=candidates[0]['value'] if len(candidates) == 1 else None)
    else:
        deleted = record['erased'] or any(row['value'] is None for row in candidates)
        branches = [row for row in candidates if row['value'] is not None]
        common = []
        if branches:
            for messages in zip(*(row['value']['messages'] for row in branches), strict=False):
                if any(message != messages[0] for message in messages[1:]):
                    break
                common.append(deepcopy(messages[0]))
        result.update(deleted=deleted, common_messages=[] if deleted else common,
                      branches=[] if deleted else branches, recovery=branches if deleted else [])
    return result
