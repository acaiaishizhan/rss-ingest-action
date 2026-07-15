# -*- coding: utf-8 -*-
import calendar
import datetime as dt
import hashlib
import html
import re
import time
from email.utils import formatdate, parsedate_to_datetime
from types import SimpleNamespace
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlparse
from urllib.request import url2pathname

import feedparser
import requests

import config


def _http_get(url: str, headers: Optional[Dict[str, str]], timeout: int) -> requests.Response:
    if config.USE_SYSTEM_PROXY:
        return requests.get(url, headers=headers, timeout=timeout)
    with requests.Session() as sess:
        sess.trust_env = False
        return sess.get(url, headers=headers, timeout=timeout)


def _is_ithome_tag_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    return host.endswith("ithome.com") and path.lower().startswith("/tags/")


def _is_linux_do_rss_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return (host == "linux.do" or host.endswith(".linux.do")) and parsed.path.lower().endswith(".rss")


def _jina_reader_url(url: str) -> str:
    return f"https://r.jina.ai/{url}"


def _is_cloudflare_challenge(resp: requests.Response) -> bool:
    headers = {str(k).lower(): str(v).lower() for k, v in getattr(resp, "headers", {}).items()}
    if headers.get("cf-mitigated") == "challenge":
        return True
    content_type = headers.get("content-type", "")
    server = headers.get("server", "")
    text = getattr(resp, "text", "") or ""
    if resp.status_code in (502, 503) and "cloudflare" in server and "text/html" in content_type:
        return True
    return resp.status_code in (403, 503) and "text/html" in content_type and "Just a moment" in text


def _strip_html(raw: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", raw or "", flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


_CHINA_TZ = dt.timezone(dt.timedelta(hours=8))


def _datetime_to_struct_time(value: dt.datetime) -> time.struct_time:
    if value.tzinfo is None:
        value = value.replace(tzinfo=_CHINA_TZ)
    return value.astimezone(dt.timezone.utc).timetuple()


def _parse_ithome_iso_timestamp(raw: str) -> Optional[time.struct_time]:
    value = html.unescape(str(raw or "")).strip()
    if not value:
        return None
    value = value.replace("Z", "+00:00")
    value = re.sub(r"(\.\d{6})\d+", r"\1", value)
    try:
        return _datetime_to_struct_time(dt.datetime.fromisoformat(value))
    except Exception:
        return None


def _parse_ithome_timestamp(raw: str) -> Optional[time.struct_time]:
    attr_match = re.search(r'\bdata-ot=["\']([^"\']+)["\']', raw or "", flags=re.I)
    if attr_match:
        parsed = _parse_ithome_iso_timestamp(attr_match.group(1))
        if parsed:
            return parsed

    iso_match = re.search(
        r"(20\d{2}-\d{1,2}-\d{1,2}T\d{1,2}:\d{1,2}(?::\d{1,2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)",
        raw or "",
        flags=re.I,
    )
    if iso_match:
        parsed = _parse_ithome_iso_timestamp(iso_match.group(1))
        if parsed:
            return parsed

    text = _strip_html(raw)
    match = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})[ T]+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", text)
    if not match:
        return None
    year, month, day, hour, minute, second = match.groups()
    try:
        # IT之家页面时间是北京时间，转换为 UTC struct_time 以匹配 feedparser 语义。
        return _datetime_to_struct_time(
            dt.datetime(
                int(year),
                int(month),
                int(day),
                int(hour),
                int(minute),
                int(second or 0),
                tzinfo=_CHINA_TZ,
            )
        )
    except Exception:
        return None


def _parse_ithome_tag_html(url: str, raw_html: str) -> SimpleNamespace:
    entries = []
    seen = set()
    pattern = re.compile(
        r'<a[^>]+href=["\'](?P<link>(?:https?:)?//(?:www\.)?ithome\.com/0/\d+/\d+\.htm|/0/\d+/\d+\.htm)["\'][^>]*>'
        r"(?P<title>.*?)</a>",
        flags=re.I | re.S,
    )
    for match in pattern.finditer(raw_html):
        link = urljoin(url, match.group("link"))
        if link in seen:
            continue
        title = _strip_html(match.group("title"))
        if not title:
            continue
        seen.add(link)

        context = raw_html[max(0, match.start() - 600) : match.start() + 1200]
        published_parsed = _parse_ithome_timestamp(context)
        entry = {
            "id": link,
            "guid": link,
            "link": link,
            "title": title,
            "summary": title,
        }
        if published_parsed:
            entry["published_parsed"] = published_parsed
            entry["published"] = formatdate(calendar.timegm(published_parsed), usegmt=True)
        entries.append(entry)

    parsed = urlparse(url)
    tag_name = html.unescape(parsed.path.rstrip("/").rsplit("/", 1)[-1])
    return SimpleNamespace(
        bozo=False,
        entries=entries,
        feed={"title": f"IT之家 - {tag_name}", "link": url},
    )


def _parse_jina_linux_do_html(url: str, raw_html: str) -> SimpleNamespace:
    title_match = re.search(r"<title[^>]*>(?P<title>.*?)</title>", raw_html or "", flags=re.I | re.S)
    feed_title = _strip_html(title_match.group("title")) if title_match else "LINUX DO"
    entries = []
    seen = set()
    pattern = re.compile(
        r"<h3[^>]*>\s*<a[^>]+href=[\"'](?P<link>https://linux\.do/t/topic/\d+)[\"'][^>]*>"
        r"(?P<title>.*?)</a>\s*</h3>.*?<time[^>]*>(?P<published>.*?)</time>",
        flags=re.I | re.S,
    )
    for match in pattern.finditer(raw_html or ""):
        link = html.unescape(match.group("link")).strip()
        if link in seen:
            continue
        title = _strip_html(match.group("title"))
        published = _strip_html(match.group("published"))
        if not title or not link:
            continue
        seen.add(link)
        entry = {
            "id": link,
            "guid": link,
            "link": link,
            "title": title,
            "summary": title,
        }
        if published:
            entry["published"] = published
            try:
                entry["published_parsed"] = _datetime_to_struct_time(parsedate_to_datetime(published))
            except Exception:
                pass
        entries.append(entry)

    return SimpleNamespace(
        bozo=False,
        entries=entries,
        feed={"title": feed_title, "link": url},
    )


def _local_feed_path(url: str) -> Optional[str]:
    if url.startswith("file://"):
        return url2pathname(urlparse(url).path)
    if re.match(r"^[A-Za-z]:[\\/]", url) or url.startswith("/") or url.startswith("\\\\"):
        return url
    return None


def _fetch_linux_do_jina_fallback(url: str, headers: Optional[Dict[str, str]], timeout: int) -> SimpleNamespace:
    fallback_headers = dict(headers or {})
    fallback_headers.update(
        {
            "x-respond-with": "html",
            "x-cache-tolerance": "600",
        }
    )
    resp = _http_get(_jina_reader_url(url), headers=fallback_headers, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"Jina fallback HTTP {resp.status_code}: {resp.text[:200]}")
    feed = _parse_jina_linux_do_html(url, resp.text)
    if not feed.entries:
        raise RuntimeError("Jina fallback returned no parsed linux.do entries")
    return feed


def fetch_feed(url: str, timeout: int, retries: int, headers: Optional[Dict[str, str]] = None) -> feedparser.FeedParserDict:
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            local_path = _local_feed_path(url)
            if local_path:
                with open(local_path, "rb") as fh:
                    raw = fh.read()
                feed = feedparser.parse(raw)
                local_entries = getattr(feed, "entries", None) or []
                if feed.bozo and not local_entries:
                    raise RuntimeError(f"Local feed parse error: {feed.bozo_exception}")
                return feed
            resp = _http_get(url, headers=headers, timeout=timeout)
            if resp.status_code != 200:
                if _is_linux_do_rss_url(url) and _is_cloudflare_challenge(resp):
                    return _fetch_linux_do_jina_fallback(url, headers, timeout)
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            if _is_ithome_tag_url(url):
                feed = _parse_ithome_tag_html(url, resp.text)
                if not feed.entries:
                    raise RuntimeError("IT之家 tag page has no parsed entries")
                return feed
            feed = feedparser.parse(resp.content)
            entries = getattr(feed, "entries", None) or []
            if feed.bozo and not entries:
                raise RuntimeError(f"Feed parse error: {feed.bozo_exception}")
            return feed
        except Exception as exc:
            last_err = exc
            time.sleep(min(8.0, 0.8 * (2 ** attempt)))
    raise RuntimeError(f"fetch_feed failed: {last_err}")


def entry_published_ts(entry: Dict[str, Any]) -> int:
    tm = entry.get("published_parsed") or entry.get("updated_parsed")
    if tm:
        return int(calendar.timegm(tm))
    return 0


def entry_text_content(entry: Dict[str, Any]) -> str:
    content = entry.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("value"):
            return str(first.get("value"))
    for key in ("content:encoded", "content_encoded", "encoded"):
        value = entry.get(key)
        if value:
            return str(value)
    summary = entry.get("summary") or entry.get("description")
    if summary:
        return str(summary)
    return ""


def build_item_key(entry: Dict[str, Any], strategy: Optional[str], content_hash_algo: Optional[str]) -> str:
    strategy = (strategy or "").strip().lower()
    if strategy == "guid":
        return str(entry.get("id") or entry.get("guid") or "").strip()
    if strategy == "link":
        return str(entry.get("link") or "").strip()
    if strategy == "title_pubdate":
        title = str(entry.get("title") or "").strip()
        published = str(entry.get("published") or entry.get("updated") or "").strip()
        return f"{title}|{published}".strip("|")
    if strategy == "content_hash":
        algo = (content_hash_algo or "md5").lower()
        raw = entry_text_content(entry)
        if not raw:
            return ""
        try:
            h = hashlib.new(algo)
        except Exception:
            h = hashlib.new("md5")
        h.update(raw.encode("utf-8", errors="ignore"))
        return f"{h.name}:{h.hexdigest()}"

    key = str(entry.get("id") or entry.get("guid") or entry.get("link") or "").strip()
    if key:
        return key
    title = str(entry.get("title") or "").strip()
    published = str(entry.get("published") or entry.get("updated") or "").strip()
    return f"{title}|{published}".strip("|")
