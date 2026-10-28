"""Node-side article reader.

Most sites refuse to be framed (`X-Frame-Options: DENY`), so the app cannot
embed them. The node fetches the page instead and extracts the readable part —
which is also the privacy story: your node reads the page, the publisher never
sees you.

Deliberately a readability-lite, stdlib-only implementation: find the element
with the most paragraph text, keep its block-level children, drop chrome.
"""

from __future__ import annotations

import html as _html
import json
import re
import shutil
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from ..atomic_io import atomic_write_json

__all__ = ["ReaderError", "extract_readable", "readable_url", "ReaderCache", "link_post_target"]

MAX_BLOCKS = 400
MIN_PARAGRAPH_CHARS = 25
CACHE_TTL_S = 24 * 3600
# Bump whenever extraction behaviour changes; older entries are then ignored.
EXTRACTOR_VERSION = 3

# Containers whose text is never article body.
_SKIP_TAGS = {
    "script",
    "style",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "noscript",
    "svg",
    "button",
    "select",
    "iframe",
}
_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre"}
# HTML void elements never emit an end tag. Counting them while skipping a
# subtree leaves the depth permanently unbalanced, which silently swallows the
# rest of the document — real pages are full of <img>/<meta>/<br>.
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_CHROME_HINT = re.compile(
    r"(^|[\s_-])(nav|menu|header|footer|sidebar|comment|promo|advert|ad|cookie|"
    r"subscribe|newsletter|share|social|related|recirc|masthead)([\s_-]|$)",
    re.IGNORECASE,
)


class ReaderError(RuntimeError):
    pass


def readable_url(url: str) -> str:
    """Rewrite to a server-rendered equivalent where one exists.

    Modern Reddit is a client-rendered app behind a bot check — fetching it
    yields "Please wait for verification" and no content. old.reddit.com
    serves the same post as plain HTML.
    """
    for host in ("://www.reddit.com/", "://reddit.com/", "://new.reddit.com/"):
        if host in url:
            return url.replace(host, "://old.reddit.com/", 1)
    return url


class _Extractor(HTMLParser):
    """Collect block-level text grouped by its containing element."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.doc_title = ""
        self.og_title = ""
        self.byline = ""
        self.lead_image = ""
        self._stack: list[str] = []
        self._skip_depth = 0
        self._skip_tag = ""
        self._password_groups: set[int] = set()
        self._in_title = False
        # container id -> list of (tag, text)
        self._groups: dict[int, list[tuple[str, str]]] = {}
        self._chrome_groups: set[int] = set()
        self._group_stack: list[int] = []
        self._block: list[str] = []
        self._block_tag = ""
        self._next_group = 0
        self._images: dict[int, list[dict[str, str]]] = {}
        self.best_group: int | None = None
        self.images_omitted = False

    # -- helpers ----------------------------------------------------------
    def _looks_like_chrome(self, attrs: dict[str, str]) -> bool:
        blob = f"{attrs.get('class', '')} {attrs.get('id', '')} {attrs.get('role', '')}"
        return bool(_CHROME_HINT.search(blob))

    def _flush(self) -> None:
        if not self._block_tag:
            return
        text = _html.unescape(" ".join(self._block)).strip()
        text = re.sub(r"\s+", " ", text)
        if text and self._group_stack:
            self._groups.setdefault(self._group_stack[-1], []).append((self._block_tag, text))
        self._block = []
        self._block_tag = ""

    # -- parser hooks -----------------------------------------------------
    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k: (v or "") for k, v in attrs_list}
        if self._skip_depth:
            # A password form beside the selected prose can be an HTTP-200
            # sign-in gate. Forms inside ignored navigation/sidebars do not
            # belong to the article and must not block a public download.
            if (self._skip_tag == "form" and tag == "input"
                    and attrs.get("type", "").strip().lower() == "password" and self._group_stack):
                self._password_groups.add(self._group_stack[-1])
            if tag not in _VOID_TAGS:
                self._skip_depth += 1
            return
        if tag in _SKIP_TAGS:
            if tag not in _VOID_TAGS:
                self._skip_depth = 1
                self._skip_tag = tag
            return

        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (attrs.get("property") or attrs.get("name") or "").lower()
            content = attrs.get("content", "").strip()
            if name in {"og:title", "twitter:title"} and not self.og_title:
                self.og_title = content
            elif name in {"author", "article:author"} and not self.byline:
                self.byline = content
            elif name in {"og:image", "twitter:image"} and not self.lead_image:
                self.lead_image = content
        elif tag in {"article", "main", "div", "section", "body"}:
            self._next_group += 1
            if self._looks_like_chrome(attrs):
                self._chrome_groups.add(self._next_group)
            self._group_stack.append(self._next_group)
        elif tag in _BLOCK_TAGS:
            self._flush()
            self._block_tag = tag
        elif tag == "br":
            self._block.append(" ")
        elif tag == "img" and self._group_stack:
            # Passive images inside the selected article only; never scripts,
            # frames, navigation art or explicitly tiny tracking pixels.
            dimensions = [attrs.get(key, '').removesuffix('px') for key in ('width', 'height')]
            tiny = any(value.isdigit() and int(value) <= 2 for value in dimensions)
            source = (attrs.get('src') or attrs.get('data-src') or '').strip()
            if source and not tiny and not self._looks_like_chrome(attrs):
                images = self._images.setdefault(self._group_stack[-1], [])
                if len(images) < 64:
                    images.append({'url': source[:4096], 'alt': attrs.get('alt', '')[:300]})
                else:
                    self.images_omitted = True
        self._stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        elif tag in {"article", "main", "div", "section", "body"}:
            self._flush()
            if self._group_stack:
                self._group_stack.pop()
        elif tag in _BLOCK_TAGS:
            self._flush()
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title and not self.doc_title:
            self.doc_title = data.strip()
        elif self._block_tag:
            self._block.append(data)

    # -- result -----------------------------------------------------------
    def best_blocks(self) -> list[dict[str, str]]:
        """The container holding the most real prose wins."""

        def score(item: tuple[int, list[tuple[str, str]]]) -> float:
            group_id, blocks = item
            prose = sum(
                len(text) for tag, text in blocks if tag == "p" and len(text) >= MIN_PARAGRAPH_CHARS
            )
            # Demote rather than delete: a nav-ish class on an ancestor
            # shouldn't be able to discard the article it wraps.
            return prose * (0.05 if group_id in self._chrome_groups else 1.0)

        if not self._groups:
            return []
        best_id, best = max(self._groups.items(), key=score)
        if score((best_id, best)) == 0:
            # No prose anywhere (link dumps, JS-rendered pages): fall back to
            # whatever headings/list text we did find, so the reader still
            # shows something rather than an empty panel.
            best_id, best = max(self._groups.items(), key=lambda row: sum(len(t) for _, t in row[1]))
        self.best_group = best_id
        out = []
        for tag, text in best[:MAX_BLOCKS]:
            if tag == "p" and len(text) < MIN_PARAGRAPH_CHARS:
                continue
            out.append({"tag": tag, "text": text})
        return out


def extract_readable(data: bytes, *, url: str = "") -> dict[str, Any]:
    """HTML bytes -> {title, byline, lead_image, blocks[], word_count}."""
    try:
        text = data.decode("utf-8", "replace")
    except Exception as exc:  # pragma: no cover - decode never raises with replace
        raise ReaderError(f"decode_failed: {exc}") from exc
    parser = _Extractor()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        # Malformed markup is normal on the open web; keep whatever parsed.
        pass
    blocks = parser.best_blocks()
    images = parser._images.get(parser.best_group, [])
    if parser.lead_image:
        images = [{'url': parser.lead_image, 'alt': ''}, *images]
    image_urls = {}
    for image in images:
        source = urljoin(url, image['url'])
        if source.startswith(('https://', 'http://')):
            image_urls.setdefault(source, {**image, 'url': source})
    words = sum(len(block["text"].split()) for block in blocks)
    return {
        "url": url,
        # og:title is the publisher's own headline; <title> usually carries
        # site branding ("How X Works | Some Blog"), so prefer og:title.
        "title": (parser.og_title or parser.doc_title).strip()[:300],
        "byline": parser.byline.strip()[:200],
        "lead_image": parser.lead_image.strip(),
        "blocks": blocks,
        "access_required": parser.best_group in parser._password_groups,
        "word_count": words,
        "images": list(image_urls.values()),
        "images_omitted": parser.images_omitted,
        "truncated": len(parser._groups.get(parser.best_group, [])) > MAX_BLOCKS,
    }


# A link aggregator's own page is not the content — the linked article is.
_REDDIT_TARGET_RE = re.compile(
    r'<a[^>]+class="[^"]*\btitle\b[^"]*"[^>]+href="([^"]+)"', re.IGNORECASE
)
# Subreddit sticky bots publish the same text on every post; extracting it
# looks like success while telling the reader nothing.
_BOILERPLATE = (
    "i am a bot, and this action was performed automatically",
    "welcome to r/",
    "do you have an academic degree?",
    "please contact the moderators of this subreddit",
    "your submission has been",
)


def link_post_target(html: bytes, *, base_url: str) -> str:
    """The external article a Reddit link post points at, if any."""
    match = _REDDIT_TARGET_RE.search(html.decode("utf-8", "replace"))
    if not match:
        return ""
    target = _html.unescape(match.group(1)).strip()
    if not target.startswith(("http://", "https://")):
        return ""
    if "reddit.com" in target or "redd.it" in target:
        return ""  # self-post: the discussion page is the content
    return target


def _drop_boilerplate(blocks: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        block
        for block in blocks
        if not any(marker in block["text"].lower() for marker in _BOILERPLATE)
    ]


class ReaderCache:
    """Disk cache so re-opening an article is instant and re-fetches are rare."""

    def __init__(self, directory: str | Path, *, ttl_s: float = CACHE_TTL_S) -> None:
        self.dir = Path(directory).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ttl_s = ttl_s

    def _path(self, url: str) -> Path:
        import hashlib

        return self.dir / (hashlib.sha256(url.encode("utf-8")).hexdigest()[:20] + ".json")

    def get(self, url: str, *, now: float, allow_stale: bool = False) -> dict[str, Any] | None:
        path = self._path(url)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not allow_stale and now - float(payload.get("cached_at", 0)) > self.ttl_s:
            return None
        if int(payload.get("extractor", 0)) != EXTRACTOR_VERSION:
            return None  # extracted by an older reader: re-fetch
        return payload.get("article")

    def put(self, url: str, article: dict[str, Any], *, now: float) -> None:
        atomic_write_json(
            self._path(url),
            {"cached_at": now, "extractor": EXTRACTOR_VERSION, "article": article},
            sort_keys=False,
            ensure_ascii=False,
        )

    def clear(self) -> None:
        """Erase locally cached article extracts without removing the cache root."""
        if self.dir.exists():
            for child in self.dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)


def read_article(
    url: str,
    *,
    fetcher: Callable[[str, float], bytes],
    cache: ReaderCache | None = None,
    timeout_s: float = 15.0,
    now: float | None = None,
) -> dict[str, Any]:
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ReaderError("reader_url_invalid")
    stamp = time.time() if now is None else now
    if cache is not None:
        hit = cache.get(url, now=stamp)
        if hit is not None:
            return {**hit, "cached": True}
    fetch_url = readable_url(url)
    try:
        payload = fetcher(fetch_url, timeout_s)
    except Exception as exc:
        raise ReaderError(f"reader_unreachable: {exc}") from exc

    # On an aggregator link post, read what it links to. Otherwise the reader
    # returns the subreddit's sticky bot comment and calls that the article.
    source_url = url
    if "reddit.com" in fetch_url:
        target = link_post_target(payload, base_url=fetch_url)
        if target:
            try:
                payload = fetcher(target, timeout_s)
                source_url = target
            except Exception:
                pass  # unreachable target: fall back to the discussion page

    article = extract_readable(payload, url=source_url)
    article["blocks"] = _drop_boilerplate(article["blocks"])
    article["word_count"] = sum(len(b["text"].split()) for b in article["blocks"])
    article["source_url"] = source_url
    if cache is not None:
        cache.put(url, article, now=stamp)
    return {**article, "cached": False}
