"""Exact public-mirror nodes frozen from local_feed_publisher.py; no production imports."""
from __future__ import annotations
import datetime as dt
import email.utils
import hashlib
import html
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List
from urllib.parse import unquote, urljoin, urlparse

PUBLISHER_SHA256 = '9fd3b2ea4daf4f75181f9f55aaa553ab03c4a75f9ca0a19ddbdca34e534593ad'
NODE_AST_SHA256 = {'SUBSTACK_MIRROR_FEEDS': 'd684bca1e1e866768b5f03aa590c839cdbd0f68a4365a1990193d5d5b00ddd2d', 'REDDIT_MIRROR_FEEDS': '331402a0141a98572ab4f9be896b87c44e1b6843ff87bdc442636f00b2db3dd9', 'FeedValidationError': '1895f784f7a99e52e2bb715840cf7343b0b45c45fea1342068f0b4c3564c3109', 'SourceSpec': '87400579a14b2f2c1b013040d6b2e63a28795e0cc00a50c9238c68de356830d9', '_local_name': '1b3a7dfb443528fab46d6bce22721d9fb3a433c8523da82fa3e0047081285cc3', 'validate_feed_bytes': 'fefb60cb0cf60a2fef45f1b5027d4a3488787cd189a984a4861d160359d74a46', '_PromptHubBlogParser': 'c51b31958581083dfeecf76c31b054b926fdcffefa6e9d7d78205107db63abf9', 'build_prompthub_blog_feed': 'c80de91a76d6cf6f35cc0d5974583145d88c3729e8d010fe0874ce1702251176', 'feed_fingerprint': 'd7693ac90814f943f599f1d8d46c5371e4059d3ae1f509702d959f5b1bc5114a', '_default_source_reader': 'e94f2563c52b98ad37fbc2b5a309eefc25fdfd18e599e7c1f0d60da62048ebb3'}

SUBSTACK_MIRROR_FEEDS = (
    ("substack-ai-maker", "https://aimaker.substack.com/feed", "feeds/public-mirror/ai-maker-substack.xml"),
    ("prompthub-blog", "https://www.prompthub.us/blog", "feeds/public-mirror/prompthub-blog.xml"),
    ("substack-a16z", "https://a16z.substack.com/feed", "feeds/public-mirror/a16z-substack.xml"),
    (
        "substack-department-of-product",
        "https://departmentofproduct.substack.com/feed",
        "feeds/public-mirror/departmentofproduct-substack.xml",
    ),
    ("substack-ai-models", "https://aimodels.substack.com/feed", "feeds/public-mirror/aimodels-substack.xml"),
    ("substack-the-sequence", "https://thesequence.substack.com/feed", "feeds/public-mirror/thesequence-substack.xml"),
    ("substack-gary-marcus", "https://garymarcus.substack.com/feed", "feeds/public-mirror/garymarcus-substack.xml"),
    (
        "substack-micro-saas-idea",
        "https://microsaasidea.substack.com/feed",
        "feeds/public-mirror/microsaasidea-substack.xml",
    ),
)

REDDIT_MIRROR_FEEDS = (
    (
        "reddit-indiehackers-ai-revenue",
        "https://www.reddit.com/r/indiehackers/search.rss?q=AI%20revenue&restrict_sr=1&sort=new",
        "feeds/public-mirror/reddit-indiehackers-ai-revenue.xml",
    ),
    (
        "reddit-sideproject-ai-revenue",
        "https://www.reddit.com/r/SideProject/search.rss?q=AI%20revenue&restrict_sr=1&sort=new",
        "feeds/public-mirror/reddit-sideproject-ai-revenue.xml",
    ),
)

class FeedValidationError(ValueError):
    """Raised when a candidate feed must not replace the last good snapshot."""

@dataclass(frozen=True)
class SourceSpec:
    name: str
    source: str
    target: str
    soft_fail: bool = False
    kind: str = "feed"

def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()

def validate_feed_bytes(payload: bytes, allow_empty: bool = False) -> int:
    if not payload or len(payload) > 50 * 1024 * 1024:
        raise FeedValidationError("feed is empty or exceeds 50 MiB")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise FeedValidationError(f"feed XML is invalid: {exc}") from exc
    root_name = _local_name(root.tag)
    if root_name not in {"rss", "feed", "rdf"}:
        raise FeedValidationError(f"unexpected feed root element: {root_name}")
    item_names = {"item"} if root_name in {"rss", "rdf"} else {"entry"}
    item_count = sum(1 for element in root.iter() if _local_name(element.tag) in item_names)
    if item_count <= 0 and not allow_empty:
        raise FeedValidationError("feed contains no items")
    return item_count

class _PromptHubBlogParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: List[Dict[str, str]] = []
        self.current: Dict[str, str] | None = None
        self.capture = ""
        self.capture_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        classes = set(str(values.get("class") or "").split())
        if tag == "a" and "blog-post-preview-content" in classes:
            self.current = {"href": str(values.get("href") or ""), "title": "", "date": ""}
        elif self.current is not None and tag == "h2" and "blog-title-list-page" in classes:
            self.capture = "title"
            self.capture_parts = []
        elif self.current is not None and tag == "p" and "blog-date" in classes:
            self.capture = "date"
            self.capture_parts = []

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.capture_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.current is not None and self.capture and tag in {"h2", "p"}:
            self.current[self.capture] = " ".join("".join(self.capture_parts).split())
            self.capture = ""
            self.capture_parts = []
        if tag == "a" and self.current is not None:
            if self.current.get("href") and self.current.get("title"):
                self.posts.append(self.current)
            self.current = None
            self.capture = ""
            self.capture_parts = []

def build_prompthub_blog_feed(payload: bytes, source_url: str) -> bytes:
    parser = _PromptHubBlogParser()
    parser.feed(payload.decode("utf-8", errors="replace"))
    seen = set()
    posts = []
    for post in parser.posts:
        link = urljoin(source_url, post["href"])
        if link in seen:
            continue
        seen.add(link)
        posts.append({**post, "link": link})
    if not posts:
        raise FeedValidationError("PromptHub blog page contains no posts")
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "PromptHub Blog"
    ET.SubElement(channel, "link").text = source_url
    ET.SubElement(channel, "description").text = "PromptHub product updates and prompt engineering guides"
    for post in posts[:50]:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = html.unescape(post["title"])
        ET.SubElement(item, "link").text = post["link"]
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = post["link"]
        try:
            published = dt.datetime.strptime(post.get("date", "").strip(), "%B %d, %Y").replace(
                tzinfo=dt.timezone.utc
            )
            ET.SubElement(item, "pubDate").text = email.utils.format_datetime(published)
        except ValueError:
            pass
    result = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    validate_feed_bytes(result)
    return result

def feed_fingerprint(payload: bytes) -> str:
    """Hash item/entry content while ignoring volatile feed-level metadata."""

    validate_feed_bytes(payload)
    root = ET.fromstring(payload)
    root_name = _local_name(root.tag)
    item_names = {"item"} if root_name in {"rss", "rdf"} else {"entry"}
    digest = hashlib.sha256()
    for element in root.iter():
        if _local_name(element.tag) not in item_names:
            continue
        digest.update(ET.tostring(element, encoding="utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()

def _default_source_reader(source: SourceSpec) -> bytes:
    parsed = urlparse(source.source)
    if parsed.scheme == "file":
        return Path(unquote(parsed.path)).read_bytes()
    request = urllib.request.Request(
        source.source,
        headers={"User-Agent": "Mozilla/5.0 (compatible; rss-local-feed-publisher/1.0)"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if getattr(response, "status", 200) != 200:
                    raise RuntimeError(f"HTTP {getattr(response, 'status', 'unknown')}")
                return response.read(50 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                raise
            if attempt >= 2:
                raise
            retry_after = str(exc.headers.get("Retry-After") or "").strip()
            delay = float(retry_after) if retry_after.isdigit() else float(10 * (attempt + 1))
            time.sleep(min(30.0, max(1.0, delay)))
    raise RuntimeError(f"remote source retries exhausted: {source.name}")
