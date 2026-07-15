# -*- coding: utf-8 -*-
import html
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Set, Tuple
from urllib.parse import urlparse


AIHOT_FEED_HOST = "aihot.virxact.com"
AIHOT_FEED_PATH = "/feed/all.xml"
AIHOT_ALL_FEED_PATH = "/feed/all.xml"
AIHOT_SELECTED_FEED_PATHS = {"/feed", "/feed.xml", "/rss"}
LOCAL_RSSHUB_HOSTS = {"192.168.1.76:1200", "localhost:1200", "127.0.0.1:1200"}
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
URL_TRAILING_CHARS = ".,;:!?)，。；：！）】》\"'"

MatchKey = Tuple[str, str]


@dataclass
class AihotDecision:
    action: str
    reason: str
    matched_enabled_keys: List[str] = field(default_factory=list)
    matched_disabled_keys: List[str] = field(default_factory=list)


def normalize_host(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def base_domain(host: str) -> str:
    host = normalize_host(host)
    for suffix in (".co.uk", ".com.cn", ".com.tw", ".com.hk", ".net.cn", ".org.cn"):
        if host.endswith(suffix):
            parts = host.split(".")
            return ".".join(parts[-3:]) if len(parts) >= 3 else host
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def is_aihot_source(source: Dict[str, Any]) -> bool:
    return bool(aihot_feed_kind(source))


def aihot_feed_kind(source: Any) -> str:
    if isinstance(source, dict):
        value = str(source.get("feed_url") or "")
    else:
        value = str(source or "")
    parsed = urlparse(value)
    path = parsed.path.rstrip("/") or "/"
    if normalize_host(parsed.netloc) != AIHOT_FEED_HOST:
        return ""
    if path == AIHOT_ALL_FEED_PATH:
        return "all"
    if path in AIHOT_SELECTED_FEED_PATHS:
        return "selected"
    return ""


def is_aihot_selected_source(source: Dict[str, Any]) -> bool:
    return aihot_feed_kind(source) == "selected"


def is_aihot_all_source(source: Dict[str, Any]) -> bool:
    return aihot_feed_kind(source) == "all"


def _entry_value(entry: Dict[str, Any], key: str, default: Any = "") -> Any:
    if hasattr(entry, "get"):
        return entry.get(key, default)
    return getattr(entry, key, default)


def _entry_text_candidates(entry: Dict[str, Any]) -> List[str]:
    candidates: List[str] = []
    for key in ("summary", "description"):
        value = _entry_value(entry, key)
        if value:
            candidates.append(str(value))

    detail = _entry_value(entry, "summary_detail")
    if isinstance(detail, dict) and detail.get("value"):
        candidates.append(str(detail.get("value")))

    content = _entry_value(entry, "content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("value"):
                candidates.append(str(item.get("value")))
    elif content:
        candidates.append(str(content))
    return candidates


def _clean_url(url: str) -> str:
    return html.unescape((url or "").strip()).rstrip(URL_TRAILING_CHARS)


def extract_aihot_original_url(entry: Dict[str, Any]) -> str:
    direct_url = _clean_url(str(_entry_value(entry, "link") or _entry_value(entry, "url") or ""))
    if direct_url and normalize_host(urlparse(direct_url).netloc) != AIHOT_FEED_HOST:
        return direct_url

    for text in _entry_text_candidates(entry):
        for match in URL_RE.findall(html.unescape(text or "")):
            url = _clean_url(match)
            if url and normalize_host(urlparse(url).netloc) != AIHOT_FEED_HOST:
                return url
    return ""


def entry_effective_url(entry: Dict[str, Any]) -> str:
    return extract_aihot_original_url(entry) or str(_entry_value(entry, "link") or _entry_value(entry, "url") or "")


def entry_for_ingest(entry: Dict[str, Any], source: Dict[str, Any] | None = None, feed_url: str = "") -> Dict[str, Any]:
    feed_kind = aihot_feed_kind(source or feed_url) or ""
    original_url = extract_aihot_original_url(entry)
    if feed_kind != "all" or not original_url:
        return entry

    parsed = urlparse(original_url)
    if normalize_host(parsed.netloc) not in {"x.com", "twitter.com", "mobile.twitter.com"}:
        return entry

    patched = dict(entry)
    feed_link = str(_entry_value(entry, "link") or "")
    patched["aihot_feed_link"] = feed_link
    patched["aihot_original_url"] = original_url
    patched["link"] = original_url
    patched["id"] = original_url
    patched["guid"] = original_url
    return patched


def is_x_entry(entry: Dict[str, Any]) -> bool:
    url = entry_effective_url(entry)
    parsed = urlparse(url)
    host = normalize_host(parsed.netloc)
    return host in {"x.com", "twitter.com", "mobile.twitter.com"}


def is_local_rsshub_source(source: Dict[str, Any]) -> bool:
    parsed = urlparse(str(source.get("feed_url") or ""))
    host = normalize_host(parsed.netloc)
    return host in LOCAL_RSSHUB_HOSTS or host.endswith(":1200")


def is_enabled(source: Dict[str, Any]) -> bool:
    return bool(source.get("enabled"))


def _twitter_handle_from_url(url: str) -> str:
    patterns = (
        r"/twitter/user/([^/?#]+)",
        r"(?:x|twitter)\.com/([^/?#]+)/status/",
        r"(?:x|twitter)\.com/([^/?#]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, url or "", re.I)
        if match:
            handle = match.group(1).strip("@").lower()
            if handle not in {"i", "home", "search"}:
                return handle
    return ""


def _twitter_handle_from_author(author: str) -> str:
    match = re.search(r"@([A-Za-z0-9_]{2,30})", author or "")
    return match.group(1).lower() if match else ""


def source_match_keys(source: Dict[str, Any]) -> Set[MatchKey]:
    url = str(source.get("feed_url") or "")
    name = str(source.get("name") or "")
    parsed = urlparse(url)
    host = normalize_host(parsed.netloc)
    path = parsed.path.strip("/")
    keys: Set[MatchKey] = set()

    handle = _twitter_handle_from_url(url) or _twitter_handle_from_author(name)
    if "/twitter/" in url.lower() or "twitter" in name.lower() or handle:
        if handle:
            keys.add(("twitter", handle))
        return keys

    if host in {"github.com"}:
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2:
            keys.add(("github", f"{parts[0].lower()}/{parts[1].lower()}"))
        return keys

    if "youtube.com" in host or "youtu.be" in host:
        return keys

    if is_local_rsshub_source(source):
        route = path.split("/", 1)[0].lower() if path else ""
        if route:
            keys.add(("rsshub_route", route))
        if route == "reuters":
            keys.add(("domain", "reuters.com"))
        if route == "aibase":
            keys.add(("domain", "aibase.com"))
        return keys

    if host:
        keys.add(("domain", host))
        root = base_domain(host)
        if root != host:
            keys.add(("domain", root))
    return keys


def entry_match_keys(entry: Dict[str, Any]) -> Set[MatchKey]:
    url = entry_effective_url(entry)
    author = str(_entry_value(entry, "author") or "")
    parsed = urlparse(url)
    host = normalize_host(parsed.netloc)
    keys: Set[MatchKey] = set()

    handle = _twitter_handle_from_url(url) or _twitter_handle_from_author(author)
    if handle:
        keys.add(("twitter", handle))

    if host:
        keys.add(("domain", host))
        root = base_domain(host)
        if root != host:
            keys.add(("domain", root))
    return keys


def format_key(key: MatchKey) -> str:
    return f"{key[0]}:{key[1]}"


def build_shadow_key_sets(sources: Iterable[Dict[str, Any]]) -> Tuple[Set[MatchKey], Set[MatchKey]]:
    enabled_keys: Set[MatchKey] = set()
    disabled_local_rsshub_keys: Set[MatchKey] = set()

    for source in sources:
        if is_aihot_source(source):
            continue
        keys = source_match_keys(source)
        if is_enabled(source):
            enabled_keys.update(keys)
        elif is_local_rsshub_source(source):
            disabled_local_rsshub_keys.update(keys)

    return enabled_keys, disabled_local_rsshub_keys


def decide_aihot_entry(
    entry: Dict[str, Any],
    sources: Iterable[Dict[str, Any]],
    source: Dict[str, Any] | None = None,
    feed_url: str = "",
) -> AihotDecision:
    enabled_keys, disabled_local_rsshub_keys = build_shadow_key_sets(sources)
    keys = entry_match_keys(entry)
    matched_enabled = sorted(keys & enabled_keys)
    feed_kind = aihot_feed_kind(source or feed_url) or "all"

    if feed_kind == "selected":
        if matched_enabled:
            return AihotDecision(
                action="skip",
                reason="covered_by_enabled_source",
                matched_enabled_keys=[format_key(key) for key in matched_enabled],
            )
        return AihotDecision(action="allow", reason="selected_uncovered_by_enabled_source")

    if is_x_entry(entry):
        return AihotDecision(action="allow", reason="twitter_entry")

    matched_disabled = sorted(keys & disabled_local_rsshub_keys)
    if matched_disabled:
        return AihotDecision(
            action="skip",
            reason="disabled_local_rsshub_non_twitter",
            matched_disabled_keys=[format_key(key) for key in matched_disabled],
        )

    return AihotDecision(action="skip", reason="not_twitter_or_selected")
