"""Local snapshot preparation and explicit private-card imports."""

from __future__ import annotations

import hashlib
import time
from typing import Any, Callable
from urllib.parse import quote

from ..offline_reading.fetch import OfflineError
from ..services.library_imports import LibraryImportStore
from .service import MAX_SHARED_CONTENT_BYTES, FriendError


class FriendContent:
    def __init__(self, *, store: Any, imports: LibraryImportStore, cache: Callable, consumption: Callable, offline: Callable | None = None):
        self.store = store
        self.imports = imports
        self.cache = cache
        self.consumption = consumption
        self.offline = offline

    def prepare(self, reference: dict[str, Any]) -> dict[str, Any]:
        """Freeze bytes already present locally; never fetch a supplied URL."""
        item_id = str(reference.get("item_id", ""))
        record = next((row for row in self.consumption().list() if row.get("item_id") == item_id), None)
        expected_job = reference.get('offline_job_id')
        prefer_source = reference.get('prefer_source', False)
        if type(prefer_source) is not bool or prefer_source and expected_job is not None:
            raise FriendError('friend_card_content_changed')
        if expected_job is not None and (not isinstance(expected_job, str) or len(expected_job) != 32):
            raise FriendError('friend_card_content_changed')
        try:
            offline = self.offline() if self.offline and not prefer_source else None
            saved = offline.resolve(item_id) if offline else None
        except OfflineError:
            raise FriendError('friend_card_content_unavailable') from None
        if expected_job is not None and (not saved or saved['body']['job_id'] != expected_job):
            raise FriendError('friend_card_content_changed')
        if saved:
            body = saved['body']
            data = body['text'].encode()
            if len(data) > MAX_SHARED_CONTENT_BYTES:
                raise FriendError('friend_card_content_too_large')
            imported = self.imports.save(data, filename='article.txt', mime='text/plain',
                source={'title': body['title'], 'source_url': body['url'], 'content_truncated': body['truncated']})
            return {'library_id': 'import:' + imported['import_id'], 'title': body['title'], 'kind': 'document',
                    'source': body['source'], 'source_url': body['url'], 'summary': '',
                    'source_offline_job_id': body['job_id'], 'content_truncated': bool(body['truncated'])}
        if item_id.startswith("import:"):
            imported = self.imports.get(item_id[7:])
            resource = self.resolve(item_id)
            if not resource:
                raise FriendError("friend_card_content_unavailable")
            origin = imported.get("source", {})
            return {"library_id": item_id, "title": origin.get("title") or imported["filename"],
                    "kind": "document", "source": "Saved document", "source_url": origin.get("source_url", ""),
                    "publisher_peer_id": origin.get("publisher_peer_id", ""), "summary": "",
                    'content_truncated': origin.get('content_truncated') is True}
        if record and str(record["item"].get("link", "")).startswith(("http://", "https://")):
            item = record["item"]
            article = self.cache().get(item["link"], now=time.time())
            if not article:
                raise FriendError("friend_card_read_first")
            text = "\n\n".join(str(block.get("text", "")) for block in article.get("blocks", []))
            data = text.encode("utf-8")
            if len(data) > MAX_SHARED_CONTENT_BYTES:
                raise FriendError("friend_card_content_too_large")
            source_url = str(article.get("source_url") or item["link"])
            title = str(article.get("title") or item.get("title", "Shared article"))
            source = {"title": title, "source_url": source_url, "content_truncated": bool(article.get('truncated'))}
            imported = self.imports.save(data, filename="article.txt", mime="text/plain", source=source)
            return {"library_id": "import:" + imported["import_id"], "title": title,
                    "kind": "document", "source": str(item.get("source_title", "")),
                    "source_url": source_url, "summary": str(item.get("summary", ""))[:500],
                    'content_truncated': bool(article.get('truncated'))}
        if not item_id or len(item_id) > 256:
            raise FriendError("friend_card_content_unavailable")
        try:
            signed = self.store.get_local_manifest(item_id)
            manifest = self.store._validate_peer_manifest(signed).manifest
            if manifest is None:
                raise FriendError("friend_card_content_unavailable")
            path = self.store.get_local_content_path(item_id)
            with path.open("rb") as handle:
                data = handle.read(MAX_SHARED_CONTENT_BYTES + 1)
            if len(data) > MAX_SHARED_CONTENT_BYTES:
                raise FriendError("friend_card_content_too_large")
            if "sha256:" + hashlib.sha256(data).hexdigest() != manifest.asset.media_hash:
                raise FriendError("friend_card_hash_mismatch")
            mime = manifest.asset.media_type
            suffix = ".pdf" if mime == "application/pdf" else ".md" if mime == "text/markdown" else ".txt"
            imported = self.imports.save(data, filename="document" + suffix, mime=mime,
                                         source={"title": manifest.title, "publisher_peer_id": manifest.publisher})
        except FriendError:
            raise
        except (OSError, ValueError):
            raise FriendError("friend_card_content_unavailable") from None
        return {"library_id": "import:" + imported["import_id"], "title": manifest.title,
                "kind": "document", "source": "Ryn content", "publisher_peer_id": manifest.publisher,
                "summary": manifest.description[:500]}

    def resolve(self, library_id: str) -> dict[str, Any] | None:
        if not library_id.startswith("import:"):
            return None
        record = self.imports.get(library_id[7:])
        if int(record.get("size_bytes", 0)) > MAX_SHARED_CONTENT_BYTES:
            raise FriendError("friend_card_content_too_large")
        return {"data": self.imports.read_bytes(library_id[7:]), "filename": record["filename"],
                "mime": record["mime"], "publisher_peer_id": (record.get("source") or {}).get("publisher_peer_id", "")}

    def import_card(self, resource: dict[str, Any]) -> dict[str, Any]:
        card = resource["card"]
        source = {"peer_id": resource["peer_id"], "card_id": resource["card_id"],
                  "title": card["title"], "source_url": card["source_url"], "publisher_peer_id": card["publisher_peer_id"],
                  'content_truncated': card.get('content_truncated') is True}
        imported = self.imports.save(resource["data"], filename=resource["filename"], mime=resource["mime"], source=source,
                                     repair=bool(resource.get("repair")), expected_generation=resource.get("generation"))
        library_id = "import:" + imported["import_id"]
        self.consumption().record({"item_id": library_id, "title": card["title"], "summary": card["summary"],
                                   "link": "rynmesh://content/" + quote(library_id, safe=""),
                                   "content_kind": "document", "content_type": imported["mime"],
                                   "source_id": resource["peer_id"], "source_title": "Shared by a friend", "tags": []}, "bookmark")
        return {"library_id": library_id, "title": card["title"], "import_id": imported["import_id"]}
