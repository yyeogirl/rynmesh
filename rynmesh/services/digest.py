"""Daily Digest — the node curates owner-chosen content sources.

The P1 single-user service (docs/superpowers/specs/2026-07-30-daily-digest-design.md):
RSS/Atom is the universal adapter (YouTube channels, subreddits, blogs,
newsletters, podcasts, HN — and RSSHub-style bridges for platforms without
feeds). Items are ranked by the same recommender pipeline that ranks mesh
content; owner feedback tunes source weights and tag affinities.

Stdlib-only. All network goes through an injectable ``fetcher(url, timeout_s)
-> bytes`` so tests run offline.
"""

from __future__ import annotations

import hashlib
import html as _html
import json
import re
import ssl
import threading
import time
import urllib.request
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping
from xml.etree import ElementTree

from ..atomic_io import atomic_write_json
from ..recommendation_evidence import build_evidence_packet
from ..recommendation_profile import RecommendationProfileStore
from ..recommender import (
    BaselineRanker,
    Candidate,
    DedupFilter,
    DismissedContentFilter,
    PeerListSource,
    Recommender,
    SafetyFilter,
    UserState,
)

__all__ = [
    "DEFAULT_DISCOVERY_SOURCES",
    "DigestError",
    "DigestService",
    "parse_feed",
    "resolve_source",
]

Fetcher = Callable[[str, float], bytes]

MAX_ITEMS_PER_SOURCE = 50
SUMMARY_MAX_CHARS = 500
WEIGHT_MIN, WEIGHT_MAX = 0.1, 3.0
WEIGHT_UP, WEIGHT_DOWN, WEIGHT_OPENED = 0.2, -0.3, 0.05
TAG_UP, TAG_DOWN = 1.0, -0.5
DEFAULT_REFRESH_INTERVAL_S = 30 * 60
DEGRADED_REFRESH_INTERVAL_S = 15 * 60
OFFLINE_REFRESH_INTERVAL_S = 5 * 60
DIGEST_SCHEMA_VERSION = 2

_USER_AGENT = "rynmesh-digest/0.5 (+https://github.com/yeogirlyun/rynmesh)"
_YT_CHANNEL_RE = re.compile(r"youtube\.com/channel/(UC[\w-]{10,})")
# Order matters: a channel page mentions many other channels' "channelId";
# its OWN id is "externalId" (or the canonical /channel/ link).
_YT_PAGE_ID_RES = (
    re.compile(r'"externalId"\s*:\s*"(UC[\w-]{10,})"'),
    re.compile(r'rel="canonical"[^>]*href="https://www\.youtube\.com/channel/(UC[\w-]{10,})"'),
    re.compile(r'"channelId"\s*:\s*"(UC[\w-]{10,})"'),
)
_SUBREDDIT_RE = re.compile(r"^(?:https?://(?:www\.)?reddit\.com/)?r/([\w]+)/?$")
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_IMG_SRC_RE = re.compile(r'<img\b[^>]*\bsrc=["\']([^"\']+)', re.IGNORECASE)


# Public, no-login discovery sources shipped with every desktop node. These
# are ordinary feed descriptors, so they enter the same local persistence,
# ranking, feedback, and safety-aware presentation path as owner-added feeds.
# They are intentionally broad: first-run usefulness must not depend on setup.
DEFAULT_DISCOVERY_SOURCES: tuple[dict[str, Any], ...] = (
    {
        "id": "default-youtube-3blue1brown",
        "kind": "youtube",
        "feed_url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCYO_jab_esuFRV4b17AJtAw",
        "title": "YouTube · 3Blue1Brown",
        "tags": ["youtube", "video", "learning", "science", "mathematics", "platform:youtube"],
        "weight": 1.0,
        "content_kind": "video",
    },
    {
        "id": "default-youtube-computerphile",
        "kind": "youtube",
        "feed_url": "https://www.youtube.com/feeds/videos.xml?channel_id=UC9-y-6csu5WGm29I7JiwpnA",
        "title": "YouTube · Computerphile",
        "tags": ["youtube", "video", "technology", "software", "security", "platform:youtube"],
        "weight": 1.0,
        "content_kind": "video",
    },
    {
        "id": "default-youtube-kurzgesagt",
        "kind": "youtube",
        "feed_url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCsXVk37bltHxD1rDPwtNM8Q",
        "title": "YouTube · Kurzgesagt",
        "tags": ["youtube", "video", "science", "learning", "animation", "platform:youtube"],
        "weight": 1.0,
        "content_kind": "video",
    },
    {
        "id": "default-reddit-science",
        "kind": "reddit",
        "feed_url": "https://www.reddit.com/r/science/.rss",
        "title": "Reddit · r/science",
        "tags": ["reddit", "science", "research", "discussion", "platform:reddit"],
        "weight": 1.0,
        "content_kind": "document",
    },
    {
        "id": "default-hacker-news",
        "kind": "news",
        "feed_url": "https://hnrss.org/frontpage",
        "title": "Hacker News · Front Page",
        "tags": ["news", "technology", "engineering", "startups", "platform:news"],
        "weight": 1.0,
        "content_kind": "report",
    },
    {
        "id": "default-bbc-world",
        "kind": "news",
        "feed_url": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "title": "BBC News · World",
        "tags": ["news", "world", "culture", "platform:news"],
        "weight": 0.9,
        "content_kind": "report",
    },
    {
        "id": "default-ars-technica",
        "kind": "news",
        "feed_url": "https://feeds.arstechnica.com/arstechnica/index",
        "title": "Ars Technica",
        "tags": ["news", "technology", "science", "engineering", "platform:news"],
        "weight": 1.0,
        "content_kind": "report",
    },
    {
        "id": "default-arxiv-ai",
        "kind": "research",
        "feed_url": "https://export.arxiv.org/rss/cs.AI",
        "title": "arXiv · Artificial Intelligence",
        "tags": ["arxiv", "research", "science", "ai", "papers", "platform:arxiv"],
        "weight": 1.0,
        "content_kind": "document",
    },
    {
        "id": "default-arxiv-machine-learning",
        "kind": "research",
        "feed_url": "https://export.arxiv.org/rss/cs.LG",
        "title": "arXiv · Machine Learning",
        "tags": ["arxiv", "research", "machine-learning", "science", "papers", "platform:arxiv"],
        "weight": 0.9,
        "content_kind": "document",
    },
    {
        "id": "default-nasa-podcast",
        "kind": "podcast",
        "feed_url": "https://www.nasa.gov/rss/dyn/Houston-We-Have-a-Podcast.rss",
        "title": "NASA · Houston, We Have a Podcast",
        "tags": ["podcast", "audio", "science", "space", "platform:podcasts"],
        "weight": 1.0,
        "content_kind": "audio",
    },
    {
        "id": "default-librivox",
        "kind": "audiobook",
        "feed_url": "https://feeds.feedburner.com/LibrivoxNewReleasesPodcast",
        "title": "LibriVox · New public-domain audiobooks",
        "tags": ["audiobook", "audio", "books", "culture", "platform:podcasts"],
        "weight": 1.0,
        "content_kind": "audio",
    },
    {
        "id": "default-xkcd",
        "kind": "image",
        "feed_url": "https://xkcd.com/rss.xml",
        "title": "xkcd · Comics",
        "tags": ["image", "cartoon", "science", "technology", "creative", "platform:rss"],
        "weight": 1.0,
        "content_kind": "image",
    },
    {
        "id": "default-nasa-image",
        "kind": "image",
        "feed_url": "https://www.nasa.gov/rss/dyn/lg_image_of_the_day.rss",
        "title": "NASA · Image of the Day",
        "tags": ["image", "space", "science", "photography", "platform:rss"],
        "weight": 1.0,
        "content_kind": "image",
    },
    {
        "id": "default-gutenberg-new",
        "kind": "books",
        "feed_url": "https://www.gutenberg.org/cache/epub/feeds/today.rss",
        "title": "Project Gutenberg · New books",
        "tags": ["books", "culture", "literature", "public-domain", "platform:rss"],
        "weight": 0.8,
        "content_kind": "document",
    },
)


# Steering / term-level ranking -------------------------------------------
# "More like this" and free-text steering must act on what an item is *about*,
# not merely which feed carried it — otherwise every signal collapses into
# "more Hacker News".
TERMS_PER_ITEM = 8
TERM_UP = 0.4
STEER_TERM_WEIGHT = 2.0

_TERM_RE = re.compile(r"[a-z0-9][a-z0-9+#.-]{2,23}")
_STOPWORDS = frozenset(
    """
the a an and or but of for to in on at by with from as is are was were be been
this that these those it its into about over after before more most less than
you your we our they their he she his her i me my not no can will just how why
what when where who which new news show says said use used using make makes
""".split()
)
_NEGATIVE_LEAD = re.compile(
    r"\b(?:less|no|not|fewer|avoid|without|stop|hide|skip|don'?t want|dont want)\b",
    re.IGNORECASE,
)
_POSITIVE_LEAD = re.compile(
    r"\b(?:more|want|prefer|show|interested in|focus on|like)\b", re.IGNORECASE
)


def _terms_of(item: Mapping[str, Any]) -> list[str]:
    """Subject terms for an item, most distinctive first (title before body)."""
    seen: dict[str, None] = {}
    for field, limit in (("title", 40), ("summary", 60)):
        for word in _TERM_RE.findall(str(item.get(field, "")).lower())[:limit]:
            if word in _STOPWORDS or word.isdigit():
                continue
            seen.setdefault(f"term:{word}", None)
    return list(seen)


def _candidate_tags(source: Mapping[str, Any], item: Mapping[str, Any]) -> tuple[str, ...]:
    """Source tags plus the item's own terms, so ranking can see the subject."""
    tags = [str(tag) for tag in source.get("tags", ())]
    tags.extend(_terms_of(item)[:TERMS_PER_ITEM])
    return tuple(dict.fromkeys(tags))


def parse_steering(text: str) -> dict[str, list[str]]:
    """Turn 'more math explainers, less politics' into interests / avoids.

    Split on separators first so each clause carries its own polarity; a clause
    without a cue word inherits the previous one, which is how people actually
    write these ("more rust, less js, no crypto").
    """
    interests: list[str] = []
    avoids: list[str] = []
    polarity = "+"
    for clause in re.split(r"[,;/\n]|\band\b|\bbut\b", str(text or "").lower()):
        clause = clause.strip()
        if not clause:
            continue
        if _NEGATIVE_LEAD.search(clause):
            polarity = "-"
        elif _POSITIVE_LEAD.search(clause):
            polarity = "+"
        words = [
            word
            for word in _TERM_RE.findall(clause)
            if word not in _STOPWORDS
            and not _NEGATIVE_LEAD.fullmatch(word)
            and not _POSITIVE_LEAD.fullmatch(word)
        ]
        target = interests if polarity == "+" else avoids
        target.extend(f"term:{word}" for word in words)
    return {
        "interests": list(dict.fromkeys(interests)),
        "avoids": list(dict.fromkeys(avoids)),
    }


class DigestError(ValueError):
    """Raised when a source cannot be resolved, fetched, or parsed."""


def default_fetcher(url: str, timeout_s: float) -> bytes:
    if not url.startswith(("http://", "https://")):
        raise DigestError(f"unsupported_url_scheme: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    context = ssl.create_default_context()
    try:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    with urllib.request.urlopen(request, timeout=timeout_s, context=context) as response:
        return response.read(8 * 1024 * 1024)


# ------------------------------------------------------------- parsing ----
def _text(node: ElementTree.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


def _strip_html(value: str) -> str:
    return _html.unescape(_TAG_STRIP_RE.sub(" ", value)).strip()[:SUMMARY_MAX_CHARS].strip()


def _parse_when(value: str) -> float:
    value = value.strip()
    if not value:
        return 0.0
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(data: bytes) -> tuple[str, list[dict[str, Any]]]:
    """Parse RSS 2.0 or Atom bytes -> (feed_title, normalized entries).

    Entry: {title, link, summary, author, published_unix, thumbnail}.
    Raises DigestError on non-XML / non-feed input.
    """
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise DigestError(f"feed_not_xml: {exc}") from exc

    kind = _localname(root.tag)
    entries: list[dict[str, Any]] = []

    if kind == "rss":
        channel = root.find("channel")
        if channel is None:
            raise DigestError("feed_missing_channel")
        feed_title = _text(channel.find("title"))
        for item in channel.findall("item"):
            thumbnail = media_url = content_type = ""
            raw_summary = _text(item.find("description"))
            for child in item.iter():
                child_name = _localname(child.tag)
                if child_name == "thumbnail" and not thumbnail:
                    thumbnail = child.get("url", "")
                elif child_name in {"enclosure", "content"} and not media_url:
                    candidate = child.get("url", "")
                    candidate_type = child.get("type", "")
                    if candidate and (
                        child_name == "enclosure"
                        or candidate_type.startswith(("audio/", "video/", "image/"))
                    ):
                        media_url = candidate
                        content_type = candidate_type
            if not thumbnail:
                image = _IMG_SRC_RE.search(raw_summary)
                if image:
                    thumbnail = _html.unescape(image.group(1))
            if content_type.startswith("image/") and not thumbnail:
                thumbnail = media_url
            entries.append(
                {
                    "title": _strip_html(_text(item.find("title"))),
                    "link": _text(item.find("link")),
                    "summary": _strip_html(raw_summary),
                    "author": _text(item.find("author")),
                    "published_unix": _parse_when(_text(item.find("pubDate"))),
                    "thumbnail": thumbnail,
                    "media_url": media_url,
                    "content_type": content_type,
                }
            )
    elif kind == "feed":  # Atom (plain or YouTube-flavored)
        ns_title = next((c for c in root if _localname(c.tag) == "title"), None)
        feed_title = _text(ns_title)
        for entry in (c for c in root if _localname(c.tag) == "entry"):
            title = link = summary = author = thumbnail = media_url = content_type = ""
            published = ""
            for child in entry.iter():
                name = _localname(child.tag)
                if name == "title" and not title:
                    title = _text(child)
                elif name == "link" and not link:
                    if child.get("rel", "alternate") == "alternate":
                        link = child.get("href", "")
                elif name in {"summary", "content", "description"} and not summary:
                    summary = _text(child)
                elif name == "name" and not author:
                    author = _text(child)
                elif name in {"published", "updated"} and not published:
                    published = _text(child)
                elif name == "thumbnail" and not thumbnail:
                    thumbnail = child.get("url", "")
                elif name in {"enclosure", "content"} and not media_url:
                    candidate = child.get("url", "") or child.get("href", "")
                    candidate_type = child.get("type", "")
                    if candidate and (
                        child.get("rel") == "enclosure"
                        or candidate_type.startswith(("audio/", "video/", "image/"))
                    ):
                        media_url = candidate
                        content_type = candidate_type
            entries.append(
                {
                    "title": _strip_html(title),
                    "link": link,
                    "summary": _strip_html(summary),
                    "author": author,
                    "published_unix": _parse_when(published),
                    "thumbnail": thumbnail,
                    "media_url": media_url,
                    "content_type": content_type,
                }
            )
    else:
        raise DigestError(f"feed_unrecognized_root: {kind}")

    return feed_title, [entry for entry in entries if entry["link"]]


# ---------------------------------------------------------- page parsing ----
class _PageMeta(HTMLParser):
    """Extract title / og:title / description from an HTML page (stdlib-only)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.og_title = ""
        self.description = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            attr = {key: (value or "") for key, value in attrs}
            name = (attr.get("property") or attr.get("name") or "").lower()
            if name == "og:title" and not self.og_title:
                self.og_title = attr.get("content", "").strip()
            elif name in {"og:description", "description"} and not self.description:
                self.description = attr.get("content", "").strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def _extract_page(data: bytes) -> tuple[str, str]:
    """(title, description) from raw HTML bytes; empty strings when absent."""
    text = data.decode("utf-8", "replace")
    parser = _PageMeta()
    try:
        parser.feed(text)
    except Exception:
        pass
    title = (parser.og_title or parser.title).strip()[:300]
    return title, parser.description.strip()[:SUMMARY_MAX_CHARS]


def _page_hash(data: bytes) -> str:
    """Content signature over visible text, so markup/timestamp noise in
    attributes doesn't count as a change."""
    text = _strip_all_html(data.decode("utf-8", "replace"))
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)


def _strip_all_html(html_text: str) -> str:
    return _TAG_STRIP_RE.sub(" ", _SCRIPT_STYLE_RE.sub(" ", html_text))


# ------------------------------------------------------ source resolving ----
def resolve_source(text: str, fetcher: Fetcher, *, timeout_s: float = 10.0) -> dict[str, Any]:
    """Normalize user input (URL / @handle / r/sub) to a validated source dict."""
    raw = str(text or "").strip()
    if not raw:
        raise DigestError("source_empty")

    sub = _SUBREDDIT_RE.match(raw)
    if sub:
        feed_url, kind = f"https://www.reddit.com/r/{sub.group(1)}/.rss", "reddit"
    elif "youtube.com" in raw or raw.startswith("@"):
        channel = _YT_CHANNEL_RE.search(raw)
        if channel:
            channel_id = channel.group(1)
        elif "feeds/videos.xml" in raw:
            channel_id = ""
        else:
            page_url = raw if raw.startswith("http") else f"https://www.youtube.com/{raw}"
            try:
                page = fetcher(page_url, timeout_s).decode("utf-8", "replace")
            except DigestError:
                raise
            except Exception as exc:
                raise DigestError(f"youtube_page_unreachable: {exc}") from exc
            for pattern in _YT_PAGE_ID_RES:
                found = pattern.search(page)
                if found:
                    channel_id = found.group(1)
                    break
            else:
                raise DigestError("youtube_channel_id_not_found")
        feed_url = (
            raw
            if "feeds/videos.xml" in raw
            else f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        )
        kind = "youtube"
    else:
        feed_url = raw if raw.startswith(("http://", "https://")) else f"https://{raw}"
        kind = "rss"

    try:
        payload = fetcher(feed_url, timeout_s)
    except DigestError:
        raise
    except Exception as exc:
        raise DigestError(f"feed_unreachable: {exc}") from exc
    feed_title, entries = parse_feed(payload)

    return {
        "id": hashlib.sha256(feed_url.encode("utf-8")).hexdigest()[:16],
        "kind": kind,
        "feed_url": feed_url,
        "title": feed_title or feed_url,
        "tags": [kind],
        "weight": 1.0,
        # Entries from the validation fetch; add_source() seeds the item store
        # with them so a new source shows items immediately and the first
        # refresh isn't a duplicate hit (rate-limited hosts like Reddit 429
        # on rapid re-fetches).
        "_entries": entries,
    }


# --------------------------------------------------------------- service ----
class DigestService:
    """Sources + items + preferences + digest builder for one node."""

    def __init__(
        self,
        home: str | Path,
        *,
        fetcher: Fetcher | None = None,
        bootstrap_defaults: bool = False,
        profile_store: RecommendationProfileStore | None = None,
    ) -> None:
        self.dir = Path(home).expanduser() / "digest"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.fetcher = fetcher or default_fetcher
        self.bootstrap_defaults = bootstrap_defaults
        self.profile_store = profile_store
        self._refresh_lock = threading.RLock()
        if bootstrap_defaults:
            self.ensure_default_sources()

    # -- persistence ------------------------------------------------------
    def _load(self, name: str, fallback: Any) -> Any:
        path = self.dir / name
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return fallback

    def _save(self, name: str, payload: Any) -> None:
        atomic_write_json(self.dir / name, payload, indent=2, sort_keys=True)

    # -- sources ----------------------------------------------------------
    def list_sources(self) -> list[dict[str, Any]]:
        return list(self._load("sources.json", []))

    def ensure_default_sources(self) -> int:
        """Install the built-in public discovery catalog without network I/O."""
        sources = self.list_sources()
        known = {str(source.get("id", "")) for source in sources}
        state = self._load("discovery-state.json", {})
        disabled = {str(value) for value in state.get("disabled_default_ids", [])}
        added = 0
        for descriptor in DEFAULT_DISCOVERY_SOURCES:
            if descriptor["id"] in known or descriptor["id"] in disabled:
                continue
            sources.append({**descriptor, "builtin": True})
            known.add(descriptor["id"])
            added += 1
        if added:
            self._save("sources.json", sources)
        return added

    def add_source(
        self, text: str, *, tags: list[str] | None = None, timeout_s: float = 10.0
    ) -> dict[str, Any]:
        source = resolve_source(text, self.fetcher, timeout_s=timeout_s)
        entries = source.pop("_entries", [])
        if tags:
            source["tags"] = sorted({*source["tags"], *(str(tag) for tag in tags)})
        sources = self.list_sources()
        if any(existing["id"] == source["id"] for existing in sources):
            raise DigestError("source_already_added")
        sources.append(source)
        self._save("sources.json", sources)
        self._merge_items(source["id"], entries)
        health = [entry for entry in self._load("health.json", []) if entry["id"] != source["id"]]
        health.append(
            {
                "id": source["id"],
                "title": source["title"],
                "ok": True,
                "error": "",
                "item_count": len(entries),
            }
        )
        self._save("health.json", health)
        return source

    def _merge_items(self, source_id: str, entries: list[dict[str, Any]]) -> int:
        items = self._load("items.json", {})
        known = {item["item_id"] for item in items.get(source_id, [])}
        merged = list(items.get(source_id, []))
        new_count = 0
        for entry in entries:
            item_id = (
                entry.get("item_id")
                or hashlib.sha256(entry["link"].encode("utf-8")).hexdigest()[:16]
            )
            if item_id in known:
                continue
            merged.append({**entry, "item_id": item_id, "source_id": source_id})
            new_count += 1
        merged.sort(key=lambda item: -float(item.get("published_unix", 0.0)))
        items[source_id] = merged[:MAX_ITEMS_PER_SOURCE]
        self._save("items.json", items)
        return new_count

    def remove_source(self, source_id: str) -> bool:
        sources = self.list_sources()
        removed = next((source for source in sources if source["id"] == source_id), None)
        remaining = [source for source in sources if source["id"] != source_id]
        if len(remaining) == len(sources):
            return False
        self._save("sources.json", remaining)
        items = self._load("items.json", {})
        items.pop(source_id, None)
        self._save("items.json", items)
        if removed and removed.get("builtin"):
            state = self._load("discovery-state.json", {})
            disabled = {str(value) for value in state.get("disabled_default_ids", [])}
            disabled.add(source_id)
            state["disabled_default_ids"] = sorted(disabled)
            self._save("discovery-state.json", state)
        return True

    # -- read-it-later ------------------------------------------------------
    READLATER_ID = "readlater"
    WATCHERS_ID = "watchers"

    def _ensure_local_source(self, source_id: str, title: str, kind: str, weight: float) -> None:
        sources = self.list_sources()
        if any(source["id"] == source_id for source in sources):
            return
        sources.append(
            {
                "id": source_id,
                "kind": kind,
                "feed_url": f"local:{source_id}",
                "title": title,
                "tags": [kind],
                "weight": weight,
            }
        )
        self._save("sources.json", sources)

    def save_link(self, url: str, *, now_unix: float, timeout_s: float = 10.0) -> dict[str, Any]:
        """Pocket-style save: fetch the page, extract title/summary, queue it."""
        url = str(url or "").strip()
        if not url.startswith(("http://", "https://")):
            raise DigestError("readlater_url_invalid")
        try:
            page = self.fetcher(url, timeout_s)
        except DigestError:
            raise
        except Exception as exc:
            raise DigestError(f"readlater_unreachable: {exc}") from exc
        title, description = _extract_page(page)
        # Saved items rank with a fresh timestamp and a boosted source weight —
        # the owner explicitly asked for this one.
        self._ensure_local_source(self.READLATER_ID, "Read later", "saved", 1.5)
        entry = {
            "title": title or url,
            "link": url,
            "summary": description,
            "author": "",
            "published_unix": float(now_unix),
            "thumbnail": "",
        }
        self._merge_items(self.READLATER_ID, [entry])
        return {"ok": True, "title": entry["title"], "summary": entry["summary"]}

    # -- watchers -------------------------------------------------------------
    def list_watchers(self) -> list[dict[str, Any]]:
        return list(self._load("watchers.json", []))

    def add_watcher(self, url: str, *, note: str = "", timeout_s: float = 10.0) -> dict[str, Any]:
        url = str(url or "").strip()
        if not url.startswith(("http://", "https://")):
            raise DigestError("watcher_url_invalid")
        try:
            page = self.fetcher(url, timeout_s)
        except DigestError:
            raise
        except Exception as exc:
            raise DigestError(f"watcher_unreachable: {exc}") from exc
        title, _ = _extract_page(page)
        watcher = {
            "id": hashlib.sha256(f"watch:{url}".encode("utf-8")).hexdigest()[:16],
            "url": url,
            "note": str(note or "").strip(),
            "title": title or url,
            "content_hash": _page_hash(page),
        }
        watchers = self.list_watchers()
        if any(existing["id"] == watcher["id"] for existing in watchers):
            raise DigestError("watcher_already_added")
        watchers.append(watcher)
        self._save("watchers.json", watchers)
        return watcher

    def remove_watcher(self, watcher_id: str) -> bool:
        watchers = self.list_watchers()
        remaining = [watcher for watcher in watchers if watcher["id"] != watcher_id]
        if len(remaining) == len(watchers):
            return False
        self._save("watchers.json", remaining)
        return True

    def check_watchers(self, *, now_unix: float, timeout_s: float = 8.0) -> int:
        """Re-fetch every watched page; changed pages become digest items."""
        watchers = self.list_watchers()
        if not watchers:
            return 0
        changed = 0
        for watcher in watchers:
            try:
                page = self.fetcher(watcher["url"], timeout_s)
            except Exception:
                continue  # unreachable now; try again next refresh
            new_hash = _page_hash(page)
            if new_hash == watcher["content_hash"]:
                continue
            watcher["content_hash"] = new_hash
            changed += 1
            label = watcher["note"] or watcher["title"]
            self._ensure_local_source(self.WATCHERS_ID, "Watchers", "watch", 1.5)
            self._merge_items(
                self.WATCHERS_ID,
                [
                    {
                        # Unique id per change — the link-derived default would dedupe
                        # every change after the first one away.
                        "item_id": hashlib.sha256(
                            f"{watcher['url']}:{new_hash}".encode("utf-8")
                        ).hexdigest()[:16],
                        "title": f"Changed: {label}",
                        "link": watcher["url"],
                        "summary": "This page changed since your node last checked it.",
                        "author": "",
                        "published_unix": float(now_unix),
                        "thumbnail": "",
                    }
                ],
            )
        if changed:
            self._save("watchers.json", watchers)
        return changed

    # -- ingestion ----------------------------------------------------------
    def refresh(self, *, timeout_s: float = 8.0, source_id: str | None = None) -> dict[str, Any]:
        """Fetch all or one source, rejecting overlapping fetches with a stable code."""
        if not self._refresh_lock.acquire(blocking=False):
            raise DigestError("discovery_busy")
        try:
            return self._refresh_sources(timeout_s=timeout_s, only_source_id=source_id)
        finally:
            self._refresh_lock.release()

    def _refresh_sources(self, *, timeout_s: float, only_source_id: str | None) -> dict[str, Any]:
        if self.bootstrap_defaults:
            self.ensure_default_sources()
        sources = self.list_sources()
        if only_source_id is not None and not any(row["id"] == only_source_id for row in sources):
            raise DigestError("source_not_found")
        previous_health = {
            str(entry.get("id", "")): entry for entry in self._load("health.json", [])
        }
        health: list[dict[str, Any]] = []
        new_count = 0
        for source in sources:
            source_id = source["id"]
            if only_source_id is not None and source_id != only_source_id:
                if source_id in previous_health:
                    health.append(previous_health[source_id])
                continue
            checked_at = time.time()
            previous = previous_health.get(source_id, {})
            if str(source.get("feed_url", "")).startswith("local:"):
                # read-later / watcher pseudo-sources have no feed to poll
                health.append(
                    {
                        "id": source_id,
                        "title": source["title"],
                        "ok": True,
                        "error": "",
                        "item_count": len(self._load("items.json", {}).get(source_id, [])),
                        "last_checked_unix": checked_at,
                        "last_success_unix": checked_at,
                        "consecutive_failures": 0,
                        "using_cached_items": False,
                    }
                )
                continue
            existing = len(self._load("items.json", {}).get(source_id, []))
            try:
                payload = self.fetcher(source["feed_url"], timeout_s)
                _, entries = parse_feed(payload)
            except DigestError:
                health.append(
                    {
                        "id": source_id,
                        "title": source["title"],
                        "ok": False,
                        "error": "source_feed_invalid",
                        "item_count": existing,
                        "last_checked_unix": checked_at,
                        "last_success_unix": float(previous.get("last_success_unix", 0.0) or 0.0),
                        "consecutive_failures": int(previous.get("consecutive_failures", 0) or 0)
                        + 1,
                        "using_cached_items": existing > 0,
                    }
                )
                continue
            except Exception:  # network layer, DNS, TLS — degrade, don't leak details
                health.append(
                    {
                        "id": source_id,
                        "title": source["title"],
                        "ok": False,
                        "error": "source_fetch_failed",
                        "item_count": existing,
                        "last_checked_unix": checked_at,
                        "last_success_unix": float(previous.get("last_success_unix", 0.0) or 0.0),
                        "consecutive_failures": int(previous.get("consecutive_failures", 0) or 0)
                        + 1,
                        "using_cached_items": existing > 0,
                    }
                )
                continue
            new_count += self._merge_items(source_id, entries)
            health.append(
                {
                    "id": source_id,
                    "title": source["title"],
                    "ok": True,
                    "error": "",
                    "item_count": len(self._load("items.json", {}).get(source_id, [])),
                    "last_checked_unix": checked_at,
                    "last_success_unix": checked_at,
                    "consecutive_failures": 0,
                    "using_cached_items": False,
                }
            )
        self._save("health.json", health)
        return {"sources": health, "new_items": new_count}

    def source_health(self) -> list[dict[str, Any]]:
        """Bounded owner-visible health, including sources not checked yet."""
        previous = {str(row.get("id", "")): row for row in self._load("health.json", [])}
        rows = []
        for source in self.list_sources():
            entry = previous.get(source["id"], {})
            ok = bool(entry.get("ok"))
            cached = bool(entry.get("using_cached_items"))
            error = str(entry.get("error", ""))
            rows.append({
                "id": source["id"], "title": str(source.get("title", ""))[:256],
                "ok": ok, "status": "not_checked" if not entry else "healthy" if ok else "cached" if cached else "failed",
                "error": "" if ok or not entry else error if error in {
                    "source_feed_invalid", "source_fetch_failed"
                } else "source_fetch_failed",
                "item_count": int(entry.get("item_count", 0) or 0),
                "last_checked_unix": float(entry.get("last_checked_unix", 0) or 0),
                "last_success_unix": float(entry.get("last_success_unix", 0) or 0),
                "consecutive_failures": int(entry.get("consecutive_failures", 0) or 0),
                "using_cached_items": cached,
            })
        return rows

    # -- proactive discovery -------------------------------------------------
    def discovery_status(self) -> dict[str, Any]:
        state = self._load("discovery-state.json", {})
        digest = self.last_digest() or {}
        items = list(digest.get("items", []))
        unread = [
            str(value) for value in state.get("unread_item_ids", []) if isinstance(value, str)
        ]
        formats = sorted(
            {str(item.get("content_kind", "document") or "document") for item in items}
        )
        health = self.source_health()
        healthy_sources = sum(1 for entry in health if entry.get("ok"))
        failed_sources = sum(1 for entry in health if entry["status"] in {"cached", "failed"})
        cached_sources = sum(1 for entry in health if entry.get("using_cached_items"))
        return {
            "phase": str(state.get("phase", "ready" if items else "waiting")),
            "message": str(state.get("message", "")),
            "last_started_unix": float(state.get("last_started_unix", 0.0) or 0.0),
            "last_completed_unix": float(state.get("last_completed_unix", 0.0) or 0.0),
            "next_refresh_unix": float(state.get("next_refresh_unix", 0.0) or 0.0),
            "new_items": int(state.get("new_items", 0) or 0),
            "unread_count": len(unread),
            "item_count": len(items),
            "source_count": len(self.list_sources()),
            "formats": formats,
            "healthy_sources": healthy_sources,
            "failed_sources": failed_sources,
            "cached_sources": cached_sources,
            "degraded": failed_sources > 0,
            "offline_ready": bool(items),
            "source_health": health,
        }

    def proactive_refresh(
        self,
        *,
        now_unix: float,
        timeout_s: float = 8.0,
    ) -> dict[str, Any]:
        """Run the zero-configuration discovery cycle and publish status."""
        if self.bootstrap_defaults:
            self.ensure_default_sources()
        previous = self.last_digest() or {}
        previous_ids = {str(item.get("item_id", "")) for item in previous.get("items", [])}
        state = self._load("discovery-state.json", {})
        state.update(
            {
                "phase": "refreshing",
                "message": "Reviewing fresh public content",
                "last_started_unix": float(now_unix),
            }
        )
        self._save("discovery-state.json", state)
        try:
            self.check_watchers(now_unix=now_unix, timeout_s=timeout_s)
            refresh = self.refresh(timeout_s=timeout_s)
            digest = self.build(now_unix=now_unix)
        except Exception:
            state.update(
                {
                    "phase": "error",
                    "message": "discovery_refresh_failed",
                    "next_refresh_unix": float(now_unix + DEFAULT_REFRESH_INTERVAL_S),
                }
            )
            self._save("discovery-state.json", state)
            raise
        current_ids = [str(item.get("item_id", "")) for item in digest.get("items", [])]
        newly_ranked = [
            item_id for item_id in current_ids if item_id and item_id not in previous_ids
        ]
        unread = {
            str(value)
            for value in state.get("unread_item_ids", [])
            if isinstance(value, str) and str(value) in current_ids
        }
        unread.update(newly_ranked)
        source_health = list(refresh.get("sources", []))
        healthy_sources = sum(1 for entry in source_health if entry.get("ok"))
        failed_sources = sum(1 for entry in source_health if not entry.get("ok"))
        if failed_sources and healthy_sources == 0:
            retry_delay = OFFLINE_REFRESH_INTERVAL_S
        elif failed_sources:
            retry_delay = DEGRADED_REFRESH_INTERVAL_S
        else:
            retry_delay = DEFAULT_REFRESH_INTERVAL_S
        state.update(
            {
                "phase": "ready" if current_ids else "waiting",
                "message": (
                    f"{len(current_ids)} recommendations ready"
                    + (f"; {failed_sources} sources will retry" if failed_sources else "")
                    if current_ids
                    else "No public feeds responded yet; the agent will retry"
                ),
                "last_completed_unix": float(now_unix),
                "next_refresh_unix": float(now_unix + retry_delay),
                "new_items": int(refresh.get("new_items", 0)),
                "unread_item_ids": sorted(unread),
            }
        )
        self._save("discovery-state.json", state)
        return {"refresh": refresh, "digest": digest, "status": self.discovery_status()}

    def mark_discovery_seen(self, *, now_unix: float) -> dict[str, Any]:
        state = self._load("discovery-state.json", {})
        state["unread_item_ids"] = []
        state["last_seen_unix"] = float(now_unix)
        self._save("discovery-state.json", state)
        return self.discovery_status()

    def personalization_data(self) -> dict[str, Any]:
        """Return owner-created digest preferences for a local privacy export."""
        return dict(self._load("prefs.json", {}))

    def clear_personalization(self) -> None:
        """Erase learned source/tag weights, steering, and seen-item signals."""
        self._save("prefs.json", {})
        self.build(now_unix=time.time())

    def clear_cached_content(self) -> None:
        """Erase public discovery results while retaining the source catalog."""
        for name, empty in (
            ("items.json", {}),
            ("health.json", []),
            ("last_digest.json", {}),
            ("discovery-state.json", {}),
        ):
            self._save(name, empty)

    def cached_item_count(self) -> int:
        return sum(
            len(items) for items in self._load("items.json", {}).values() if isinstance(items, list)
        )

    def enrich_latest(self, provider: Any) -> dict[str, Any]:
        """Add local-model summaries after the fast baseline slate is ready."""
        return self.build(now_unix=datetime.now(UTC).timestamp(), provider=provider)

    # -- feedback -----------------------------------------------------------
    # -- steering ------------------------------------------------------------
    def get_steering(self) -> dict[str, Any]:
        prefs = self._load("prefs.json", {})
        steer = prefs.get("steering", {}) or {}
        return {
            "text": str(steer.get("text", "")),
            "interests": list(steer.get("interests", [])),
            "avoids": list(steer.get("avoids", [])),
        }

    def steer(self, text: str) -> dict[str, Any]:
        """Record free-text direction from the owner ("more X, less Y")."""
        text = str(text or "").strip()
        parsed = parse_steering(text)
        prefs = self._load("prefs.json", {})
        prefs["steering"] = {"text": text, **parsed}
        self._save("prefs.json", prefs)
        if self.profile_store is not None:
            self.profile_store.patch({"direction": text})
        return {"text": text, **parsed}

    def feedback(self, item_id: str, action: str) -> dict[str, Any]:
        if action not in {"up", "down", "hide", "opened", "more_like_this"}:
            raise DigestError(f"feedback_action_unknown: {action}")
        prefs = self._load(
            "prefs.json", {"seen": [], "tag_affinity": {}, "source_weight": {}, "events": 0}
        )
        item = self._find_item(item_id)
        if item is None:
            raise DigestError("feedback_item_unknown")
        source = next((s for s in self.list_sources() if s["id"] == item["source_id"]), None)

        if self.profile_store is not None:
            # New actions have one canonical influence. Legacy derived weights
            # remain on disk for export, but must not survive undo in the ranker.
            if action != "opened":
                self.profile_store.feedback({
                    "content_id": f"digest:{item_id}", "title": item.get("title", ""),
                    "tags": list(_candidate_tags(source, item)) if source else [],
                    "publisher_peer_id": f"source:{item['source_id']}",
                    "source_platform": self._source_platform(source) if source else "",
                }, "more" if action in {"up", "more_like_this"} else
                    "hide" if action == "hide" else "less")
            else:
                prefs["seen"] = sorted(set(prefs.get("seen", [])) | {item_id})
                self._save("prefs.json", prefs)
            return {"ok": True}

        seen = set(prefs.get("seen", []))
        seen.add(item_id)
        prefs["seen"] = sorted(seen)

        delta = {
            "up": WEIGHT_UP,
            "down": WEIGHT_DOWN,
            "hide": WEIGHT_DOWN,
            "opened": WEIGHT_OPENED,
            "more_like_this": WEIGHT_UP,
        }[action]
        weights = prefs.setdefault("source_weight", {})
        current = float(weights.get(item["source_id"], source["weight"] if source else 1.0))
        weights[item["source_id"]] = round(min(WEIGHT_MAX, max(WEIGHT_MIN, current + delta)), 3)

        if source and action in {"up", "down", "hide", "more_like_this"}:
            affinity = prefs.setdefault("tag_affinity", {})
            tag_delta = TAG_DOWN if action in {"down", "hide"} else TAG_UP
            for tag in source.get("tags", []):
                affinity[tag] = round(float(affinity.get(tag, 0.0)) + tag_delta, 3)
            if action in {"up", "more_like_this"}:
                # Promote the item's own subject terms, weighted below source
                # tags so one click can't drown the rest of the profile.
                for term in _terms_of(item)[:TERMS_PER_ITEM]:
                    affinity[term] = round(float(affinity.get(term, 0.0)) + TERM_UP, 3)
        prefs["events"] = int(prefs.get("events", 0)) + 1
        self._save("prefs.json", prefs)
        return {"ok": True, "source_weight": weights[item["source_id"]]}

    def _find_item(self, item_id: str) -> dict[str, Any] | None:
        for source_items in self._load("items.json", {}).values():
            for item in source_items:
                if item["item_id"] == item_id:
                    return item
        return None

    # -- AI enrichment ---------------------------------------------------------
    # Bounded model usage per build: one brief + summaries for the top items
    # that don't have one yet (cached in items.json across builds).
    BRIEF_ITEMS = 10
    SUMMARIZE_TOP = 6
    AI_GROUNDING_VERSION = 1

    @staticmethod
    def _content_shape(source: dict[str, Any], item: dict[str, Any]) -> tuple[str, str]:
        declared = str(source.get("content_kind", "") or "")
        media_type = str(item.get("content_type", "") or "")
        if media_type.startswith("audio/"):
            return "audio", media_type
        if media_type.startswith("video/"):
            return "video", media_type
        if media_type.startswith("image/"):
            return "image", media_type
        if declared:
            default_type = {
                "audio": "audio/mpeg",
                "video": "text/html",
                "image": "image/*",
                "report": "text/html",
                "document": "text/html",
            }.get(declared, "text/html")
            return declared, media_type or default_type
        kind = str(source.get("kind", "rss"))
        inferred = {
            "youtube": "video",
            "podcast": "audio",
            "audiobook": "audio",
            "image": "image",
            "news": "report",
            "research": "document",
        }.get(kind, "document")
        return inferred, media_type or ("audio/mpeg" if inferred == "audio" else "text/html")

    @staticmethod
    def _source_platform(source: Mapping[str, Any]) -> str:
        return {
            "youtube": "youtube",
            "reddit": "reddit",
            "research": "arxiv",
            "podcast": "podcasts",
            "audiobook": "podcasts",
            "news": "news",
        }.get(str(source.get("kind", "rss") or "rss"), "rss")

    def _enrich(self, digest: dict[str, Any], provider: Any) -> dict[str, Any]:
        items = digest["items"]
        if not items:
            return digest
        stored = self._load("items.json", {})

        summarized = 0
        for item in items[: self.SUMMARIZE_TOP]:
            if (
                item.get("ai_summary")
                and item.get("ai_summary_grounding_version") == self.AI_GROUNDING_VERSION
            ):
                continue
            source_text = (item.get("summary") or item["title"])[:2000]
            try:
                summary = provider.generate(
                    "You are summarizing public-feed metadata, not the full item. "
                    "Use only the supplied title and feed summary. Do not infer, add, or "
                    "verify facts that are absent. If the metadata is insufficient, say "
                    "'The feed does not provide enough detail to summarize this item.' "
                    "Return one plain sentence of at most 30 words with no preamble or markdown.\n\n"
                    f"Source: {item['source_title']}\nTitle: {item['title']}\n"
                    f"Feed summary: {source_text}",
                    max_tokens=120,
                )
            except Exception:
                break  # provider went away mid-build; keep the digest usable
            if not summary:
                continue
            item["ai_summary"] = summary
            item["ai_summary_grounding_version"] = self.AI_GROUNDING_VERSION
            summarized += 1
            for stored_item in stored.get(item["source_id"], []):
                if stored_item["item_id"] == item["item_id"]:
                    stored_item["ai_summary"] = summary
                    stored_item["ai_summary_grounding_version"] = self.AI_GROUNDING_VERSION
        if summarized:
            self._save("items.json", stored)

        try:
            listing = "\n".join(
                f"[{index}] Source={item['source_title']} | Title={item['title']} | Feed summary="
                f"{(item.get('ai_summary') or item.get('summary') or '')[:200]}"
                for index, item in enumerate(items[: self.BRIEF_ITEMS], start=1)
            )
            digest["brief"] = provider.generate(
                "You are the owner's personal briefing agent. Every entry below is only "
                "a title and public-feed summary, not full content. Use only those entries; "
                "do not add outside facts or claim to have watched, listened to, or read them. "
                "Write 2-4 plain-text bullets beginning with '- '. End each bullet with the "
                "supporting item numbers, such as [1] or [2][4]. If evidence is insufficient, "
                "say so. No header or preamble.\n\n"
                f"{listing}",
                max_tokens=300,
            )
        except Exception:
            digest["brief"] = ""
        digest["ai"] = {
            "provider": provider.id,
            "model": provider.model,
            "review_basis": "metadata",
            "grounding_version": self.AI_GROUNDING_VERSION,
        }
        return digest

    # -- digest ---------------------------------------------------------------
    def build(
        self, *, now_unix: float, limit: int = 30, provider: Any | None = None
    ) -> dict[str, Any]:
        sources = {source["id"]: source for source in self.list_sources()}
        prefs = self._load("prefs.json", {})
        profile_signals = self.profile_store.signals() if self.profile_store is not None else None
        weights = {
            source_id: float(prefs.get("source_weight", {}).get(source_id, source["weight"]))
            for source_id, source in sources.items()
        }
        if profile_signals is not None:
            weights = {
                source_id: min(WEIGHT_MAX, max(WEIGHT_MIN, float(source["weight"]) +
                    float(profile_signals["publisher_weights"].get(f"source:{source_id}", 0)) * 0.1))
                for source_id, source in sources.items()
            }
        max_weight = max(weights.values(), default=0.0)
        trust = {sid: (w / max_weight if max_weight > 0 else 0.0) for sid, w in weights.items()}
        rated_sources = set(prefs.get("source_weight", {}))
        if profile_signals is not None:
            rated_sources = {key.removeprefix("source:") for key in
                             profile_signals["publisher_weights"] if key.startswith("source:")}

        candidates = []
        by_id: dict[str, dict[str, Any]] = {}
        for source_id, source_items in self._load("items.json", {}).items():
            source = sources.get(source_id)
            if source is None:
                continue
            for item in source_items:
                by_id[item["item_id"]] = item
                candidates.append(
                    Candidate(
                        content_id=item["item_id"],
                        publisher_peer_id=source_id,
                        title=item["title"],
                        tags=_candidate_tags(source, item),
                        safety_outcome="pass",
                        published_at_unix=float(item.get("published_unix", 0.0)),
                        summary=item.get("summary", ""),
                    )
                )

        steering = prefs.get("steering", {}) or {}
        steer_up = dict.fromkeys(steering.get("interests", []), STEER_TERM_WEIGHT)
        steer_down = set(steering.get("avoids", []))

        liked = {t: v for t, v in prefs.get("tag_affinity", {}).items() if v > 0}
        liked.update(steer_up)
        hidden_content_ids: set[str] = set()
        if profile_signals is not None:
            liked = {}  # Profile already includes explicit direction and feedback.
            steer_down = set()
            for tag, weight in profile_signals["tag_weights"].items():
                liked[tag] = liked.get(tag, 0.0) + float(weight)
            hidden_content_ids.update(
                content_id.removeprefix("digest:")
                for content_id in profile_signals["hidden_content_ids"]
                if str(content_id).startswith("digest:")
            )
        user = UserState(
            liked_tags=liked,
            fetched_content_ids=set(prefs.get("seen", [])),
            now_unix=float(now_unix),
        )
        # Rank the WHOLE candidate set. Capping the pool first is what let two
        # 50-posts-a-day text feeds bury every video, podcast, and comic: the
        # other media never reached the pool to be balanced.
        ranked_pool = Recommender(
            sources=[PeerListSource({"feeds": candidates})],
            trust=trust,
            ranker=BaselineRanker(),
            filters=[SafetyFilter(), DedupFilter(), DismissedContentFilter(hidden_content_ids)],
            exploration_fraction=0.15,
            newcomer_predicate=lambda cand: cand.publisher_peer_id not in rated_sources,
        ).recommend(user, k=len(candidates))

        if steer_down:
            filtered = [
                (candidate, score)
                for candidate, score in ranked_pool
                if not steer_down.intersection(candidate.tags)
            ]
            # Never empty the slate on steering alone — an over-broad "less X"
            # should bias the feed, not blank it.
            if filtered:
                ranked_pool = filtered

        # Round-robin across formats, best-first within each. A high-volume
        # text feed can still lead the slate, but it can no longer own it.
        by_format: dict[str, list[tuple[Candidate, float]]] = {}
        for candidate, score in ranked_pool:
            source = sources[candidate.publisher_peer_id]
            content_kind, _ = self._content_shape(source, by_id[candidate.content_id])
            by_format.setdefault(content_kind, []).append((candidate, score))
        # Format order follows each format's best score, so the strongest
        # medium of the moment still leads.
        order = sorted(by_format, key=lambda kind: -by_format[kind][0][1])
        ranked = []
        index = 0
        while len(ranked) < limit and any(index < len(by_format[k]) for k in order):
            for kind in order:
                if len(ranked) >= limit:
                    break
                bucket = by_format[kind]
                if index < len(bucket):
                    ranked.append(bucket[index])
            index += 1
        ranked.sort(key=lambda pair: -pair[1])

        max_score = max((score for _, score in ranked), default=0.0)
        digest_items = []
        for cand, score in ranked:
            item = by_id[cand.content_id]
            source = sources[cand.publisher_peer_id]
            content_kind, content_type = self._content_shape(source, item)
            weight = weights.get(cand.publisher_peer_id, 1.0)
            reasons = []
            age_h = (now_unix - cand.published_at_unix) / 3600.0 if cand.published_at_unix else None
            if age_h is not None and age_h < 48:
                reasons.append("fresh" if age_h >= 1 else "just published")
            if weight > 1.0:
                reasons.append("source you like")
            matched = sorted(tag for tag in cand.tags if user.liked_tags.get(tag))
            if matched:
                reasons.append("matches " + ", ".join(matched[:2]))
            if cand.publisher_peer_id not in rated_sources:
                reasons.append("new source")
            evidence_signals: list[str] = []
            if age_h is not None and age_h < 48:
                evidence_signals.append("fresh")
            if weight > 1.0:
                evidence_signals.append("source_affinity")
            if matched:
                evidence_signals.append("tag_match")
            if cand.publisher_peer_id not in rated_sources:
                evidence_signals.append("new_source")
            digest_item = {
                "item_id": cand.content_id,
                "source_id": cand.publisher_peer_id,
                "source_title": source["title"],
                "source_kind": source["kind"],
                "title": item["title"],
                "link": item["link"],
                "summary": item.get("summary", ""),
                "author": item.get("author", ""),
                "thumbnail": item.get("thumbnail", ""),
                "media_url": item.get("media_url", ""),
                "content_kind": content_kind,
                "content_type": content_type,
                "tags": list(cand.tags),
                "published_unix": item.get("published_unix", 0.0),
                "ai_summary": (
                    item.get("ai_summary", "")
                    if item.get("ai_summary_grounding_version") == self.AI_GROUNDING_VERSION
                    else ""
                ),
                "ai_summary_grounding_version": (
                    self.AI_GROUNDING_VERSION
                    if item.get("ai_summary_grounding_version") == self.AI_GROUNDING_VERSION
                    else 0
                ),
                "score": round(score / max_score, 3) if max_score > 0 else 0.0,
                "reasons": reasons or ["from your sources"],
                "review_basis": "metadata",
                "safety_outcome": "unscanned",
                "provenance_status": "unsigned",
            }
            digest_item["evidence_packet"] = build_evidence_packet(
                digest_item,
                signals=evidence_signals,
                reviewed_at_unix=now_unix,
            )
            digest_items.append(digest_item)

        digest: dict[str, Any] = {
            "schema_version": DIGEST_SCHEMA_VERSION,
            "generated_at_unix": float(now_unix),
            "brief": "",
            "ai": None,
            "items": digest_items,
            "sources": self._load("health.json", []),
        }
        if provider is not None:
            digest = self._enrich(digest, provider)
        self._save("last_digest.json", digest)
        return digest

    def last_digest(self) -> dict[str, Any] | None:
        digest = self._load("last_digest.json", None)
        if not isinstance(digest, dict):
            return None
        if digest.get("schema_version") != DIGEST_SCHEMA_VERSION:
            return self.build(now_unix=time.time())
        return digest

    def recommendation_items(self) -> list[dict[str, Any]]:
        """Expose ranked digest entries to the shared recommendation engine."""
        digest = self.last_digest() or {}
        out: list[dict[str, Any]] = []
        for item in digest.get("items", []):
            raw_id = str(item.get("item_id", ""))
            if not raw_id:
                continue
            published_unix = float(item.get("published_unix", 0.0) or 0.0)
            published = (
                datetime.fromtimestamp(published_unix, UTC).isoformat()
                if published_unix > 0
                else None
            )
            source_kind = str(item.get("source_kind", "rss") or "rss")
            platform = self._source_platform({"kind": source_kind})
            out.append(
                {
                    "content_id": f"digest:{raw_id}",
                    "digest_item_id": raw_id,
                    "manifest_hash": "",
                    "title": str(item.get("title", "")),
                    "description": str(item.get("ai_summary") or item.get("summary") or ""),
                    "source_description": str(item.get("summary") or ""),
                    "tags": list(item.get("tags", [])),
                    "content_kind": str(item.get("content_kind", "document")),
                    "content_type": str(item.get("content_type", "text/html")),
                    "publisher_peer_id": f"source:{item.get('source_id', '')}",
                    "provider_peer_id": f"source:{item.get('source_id', '')}",
                    "source_peer_name": str(item.get("source_title", "Public source")),
                    "source_platform": platform,
                    "identity_tier": "unverified",
                    "credit_score": 0.0,
                    "distribution_weight": float(item.get("score", 0.0) or 0.0),
                    "safety_outcome": "unscanned",
                    "provenance_status": "unsigned",
                    "provenance_head_hash": None,
                    "fetch_status": "discovered",
                    "review_basis": "metadata",
                    "size": "",
                    "published": published,
                    "external": True,
                    "external_url": str(item.get("link", "")),
                    "thumbnail_url": str(item.get("thumbnail", "")),
                    "media_url": str(item.get("media_url", "")),
                }
            )
        return out
