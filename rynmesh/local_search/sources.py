"""Local-only adapters. Discovery, HTTP fetching and extraction are forbidden here."""
from __future__ import annotations

import hashlib
import math
import time
from datetime import datetime
from typing import Callable
from urllib.parse import quote, urlencode

from ..offline_reading.fetch import OfflineError
from ..store import StoreError
from .index import SearchSnapshot


def _stamp(value) -> float:
    if isinstance(value, str):
        try:
            return max(0, datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0
    return max(0, float(value or 0))


def _clip(value, size=2048) -> str:
    return str(value or "").encode()[:size].decode(errors="ignore")


def _saved_documents(imports, unavailable):
    try:
        documents = imports.list()
    except (OSError, ValueError, TypeError, KeyError):
        unavailable.add('saved_documents')
        return
    for row in documents:
        try:
            if (not isinstance(row, dict) or row.get('state') != 'ready'
                    or any(not isinstance(row.get(key), str) for key in ('import_id', 'sha256', 'filename', 'mime'))
                    or 'created_at_unix' not in row or not math.isfinite(_stamp(row['created_at_unix']))
                    or not isinstance(row.get('source', {}), dict)
                    or not isinstance(row.get('source', {}).get('source_url', ''), str)):
                raise ValueError
            try:
                body = imports.body(row['import_id'])
            except (OSError, ValueError, TypeError, KeyError):
                # Valid metadata can still be found, but never the stale body.
                unavailable.add('saved_documents')
                body = None
            yield row, body
        except (OSError, ValueError, TypeError, KeyError):
            unavailable.add('saved_documents')


class LocalSearchSources:
    def __init__(self, *, consumption: Callable, imports: Callable, reader: Callable,
                 friends: Callable, conversations: Callable, store: Callable | None = None, offline: Callable | None = None):
        self.consumption, self.imports, self.reader = consumption, imports, reader
        self.friends, self.conversations = friends, conversations
        self.store = store
        self.offline = offline

    def snapshot(self) -> list[dict]:
        imports = self.imports()
        friends = self.friends()
        rows = {}
        unavailable = set()
        # A matching URL alone is insufficient to merge different revisions.
        verified = {}
        aliases = {}

        def content(identifier, title, text, source, stamp, kinds, record=None, body_state="available"):
            return {"id": identifier, "title": _clip(title), "text": text, "source": _clip(source),
                    "timestamp": stamp, "kinds": kinds, "friend_ids": [], "body_state": body_state,
                    "reading_record": record,
                    "targets": [{"label": "Read local content", "href": "/search?" + urlencode({"open": identifier})}]}

        history = {record["item_id"]: record for record in self.consumption().list()}
        for record in history.values():
            kinds = (["saved"] if record.get("bookmarked") else []) + (["history"] if record.get("open_count") or record.get('sync_reading_available') else [])
            if not kinds:
                continue
            item = record["item"]
            item_id = record["item_id"]
            if item_id.startswith("import:"):
                continue  # Only committed, verified imports supply these bodies.
            article = self.reader().get(item.get("link", ""), now=time.time(), allow_stale=True)
            text = "\n\n".join(str(block.get("text", "")) for block in (article or {}).get("blocks", []))
            if not text and self.store and not item.get("link", "").startswith(("https://", "http://")):
                try:
                    store = self.store()
                    manifest = store._validate_peer_manifest(store.get_local_manifest(item_id)).manifest
                    path = store.get_local_content_path(item_id)
                    if manifest and (manifest.asset.media_type.startswith("text/") or manifest.asset.media_type in {
                            "application/json", "application/xml", "application/x-yaml"}) and path.stat().st_size <= 8 * 1024 * 1024:
                        data = path.read_bytes()
                        if "sha256:" + hashlib.sha256(data).hexdigest() == manifest.asset.media_hash:
                            text = data.decode("utf-8")
                except (StoreError, OSError, ValueError):
                    text = ""
            identifier = "content:" + item_id
            rows[identifier] = content(identifier, item.get("title"), text, item.get("source_title"),
                _stamp(record.get("last_activity_unix")), kinds, record,
                "available" if text else "not_cached")
            if text:
                verified[(item.get("link", ""), hashlib.sha256(text.encode()).hexdigest())] = identifier

        for imported, body in _saved_documents(imports, unavailable):
            item_id = "import:" + imported["import_id"]
            origin = imported.get("source") or {}
            if body is not None:
                text, body_state = body["text"], "available"
                truncated = bool(body.get("truncated"))
            else:
                text, body_state = "", "unavailable"
                truncated = False
            record = history.get(item_id)
            kinds = ["saved"] + (["history"] if record and (record.get("open_count") or record.get('sync_reading_available')) else [])
            identity = (origin.get("source_url", ""), imported["sha256"])
            identifier = verified.get(identity, "content:" + item_id) if text else "content:" + item_id
            aliases[item_id] = identifier
            if record is None:
                record = {"item_id": item_id, "bookmarked": True, "progress": 0, "open_count": 0,
                          "item": {"item_id": item_id, "title": origin.get("title") or imported["filename"],
                                   "source_title": "Saved document", "link": "rynmesh://content/" + quote(item_id, safe=""),
                                   "content_kind": "document", "content_type": imported["mime"]}}
            if identifier not in rows:
                rows[identifier] = content(identifier, origin.get("title") or imported["filename"], text,
                    origin.get("source_url") or "Saved document", _stamp((record or {}).get("last_activity_unix", imported["created_at_unix"])),
                    kinds, record, body_state)
            else:
                rows[identifier]["kinds"] = sorted(set(rows[identifier]["kinds"] + kinds))
            rows[identifier]["text_truncated"] = truncated or rows[identifier].get("text_truncated", False)
            if text:
                verified[identity] = identifier

        if self.offline:
            offline = self.offline()
            try:
                downloads = offline.status()['records']
            except (OfflineError, OSError):
                unavailable.add('offline_downloads')
                downloads = []  # Fail closed for this source while other local sources remain usable.
            for saved in downloads:
                if not saved.get('current'):
                    continue
                try:
                    body = offline.read(saved['item_id'])
                except (OfflineError, OSError):
                    unavailable.add('offline_downloads')
                    continue  # Corrupt/cleared bodies cannot survive via an old index.
                item_id, text = saved['item_id'], body['text']
                prior_id = aliases.get(item_id, 'content:' + item_id)
                prior = rows.get(prior_id)
                identifier = prior_id if prior is not None and (not prior['text'] or prior['text'] == text) else 'offline:' + saved['key']
                record = history.get(item_id) or {'item_id': item_id, 'bookmarked': False, 'progress': 0, 'open_count': 0,
                    'item': {'item_id': item_id, 'title': body['title'], 'link': body['url'],
                             'source_title': body['source'], 'content_kind': 'document'}}
                if identifier not in rows or not rows[identifier]['text']:
                    rows[identifier] = content(identifier, body['title'], text, body['source'],
                        _stamp(body['downloaded_at']), sorted(set(['saved'] + (prior or {}).get('kinds', []))), record)
                rows[identifier]['offline_key'] = saved['key']
                rows[identifier]['text_truncated'] = bool(body['truncated'])
                if body.get('url'):
                    verified[(body['url'], hashlib.sha256(text.encode()).hexdigest())] = identifier

        relations = {row["relationship_id"]: row for row in friends.store.list_relationships() if row["status"] == "active"}
        for card in friends.store.list_cards():
            relation = relations.get(card.get("relationship_id"))
            if card.get("dir") != "in" or relation is None:
                continue
            info = card["card"]
            identifier = aliases.get(card.get("fetched_library_id")) or verified.get((info.get("source_url", ""), info.get("sha256")))
            target = {"label": "Open share", "href": "/friends?" + urlencode({"card": card["card_id"]})}
            if identifier is None:
                identifier = "card:" + card["card_id"]
                rows[identifier] = content(identifier, info.get("title"), "", info.get("source"),
                    _stamp(card.get("created_at")), ["share"], body_state="not_downloaded")
                rows[identifier]["targets"] = [target]
            else:
                rows[identifier]["kinds"] = sorted(set(rows[identifier]["kinds"] + ["share"]))
                if target not in rows[identifier]["targets"] and len(rows[identifier]["targets"]) < 8:
                    rows[identifier]["targets"].append(target)
            rows[identifier]["friend_ids"] = sorted(set(rows[identifier]["friend_ids"] + [relation["peer_id"]]))

        for relation in {row["peer_id"]: row for row in relations.values()}.values():
            peer_id = relation["peer_id"]
            for message in friends.history(peer_id):
                query = urlencode({"peer": peer_id, "message": message["msg_id"]})
                rows["message:" + peer_id + ":" + message["msg_id"]] = {
                    "id": "message:" + peer_id + ":" + message["msg_id"], "title": _clip(relation.get("node_name") or peer_id),
                    "text": message.get("text", "") + ("\n" + message["attachment"]["filename"] if message.get("attachment") else ""),
                    "source": _clip(relation.get("node_name") or peer_id), "timestamp": _stamp(message.get("ts")),
                    "kinds": ["chat", "share"] if message.get("dir") == "in" else ["chat"], "friend_ids": [peer_id],
                    "targets": [{"label": "Open message", "href": "/friends?" + query}]}

        for conversation in self.conversations().list():
            messages = conversation["messages"] or [{"id": "empty", "content": "", "createdAt": conversation["updatedAt"]}]
            for message in messages:
                identifier = "ask:" + conversation["id"] + ":" + message["id"]
                rows[identifier] = {"id": identifier, "title": _clip(conversation["title"]), "text": message["content"],
                    "source": _clip(conversation["serviceName"]), "timestamp": _stamp(message["createdAt"]),
                    "kinds": ["chat"], "friend_ids": [conversation["providerPeerId"]],
                    "targets": [{"label": "Open Ask Ryn message", "href": "/ask?" + urlencode({"conversation": conversation["id"],
                        "network": conversation["networkId"], "message": message["id"]})}]}
        return SearchSnapshot(rows.values(), unavailable_sources=unavailable)
