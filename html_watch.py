# -*- coding: utf-8 -*-
import calendar
import datetime as dt
import hashlib
import html
import json
import re
import time
from dataclasses import dataclass, field
from email.utils import formatdate, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin

import requests


WATCH_TYPE = "html_watch"


@dataclass
class HtmlResponse:
    status_code: int
    url: str
    text: str
    headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class HtmlWatchResult:
    status: str
    entries: List[Dict[str, Any]]
    watch_state: Dict[str, Any]
    error: str = ""


class _AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._href_stack: List[str] = []
        self._text_parts: List[str] = []
        self.anchors: List[Dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        if tag.lower() != "a":
            return
        href = ""
        for key, value in attrs:
            if key and key.lower() == "href":
                href = value or ""
                break
        self._href_stack.append(href)
        self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._href_stack:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._href_stack:
            return
        href = self._href_stack.pop()
        text = _clean_text(" ".join(self._text_parts))
        if href and text:
            self.anchors.append({"href": html.unescape(href), "text": text})
        self._text_parts = []


def parse_watch_state(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        data = json.loads(str(raw))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def serialize_watch_state(state: Dict[str, Any]) -> str:
    clean = {k: v for k, v in (state or {}).items() if v not in ("", None, [], {})}
    return json.dumps(clean, ensure_ascii=False, sort_keys=True)


def is_html_watch_source(source: Dict[str, Any]) -> bool:
    return str(source.get("type") or "").strip().lower() == WATCH_TYPE


def should_fetch_html_watch(source: Dict[str, Any], now_ms: int, interval_min: int = 10) -> bool:
    state = parse_watch_state(source.get("watch_state"))
    backoff_until = int(state.get("backoff_until") or 0)
    if backoff_until and now_ms < backoff_until:
        return False
    last_fetch = int(source.get("last_fetch_time") or 0)
    if last_fetch <= 0:
        return True
    jitter_ms = _source_jitter_ms(source)
    return now_ms - last_fetch >= interval_min * 60 * 1000 + jitter_ms


def fetch_html_watch(
    source: Dict[str, Any],
    now_ms: int,
    http_get: Optional[Callable[[str, Dict[str, str], int], HtmlResponse]] = None,
    timeout: int = 12,
) -> HtmlWatchResult:
    url = str(source.get("feed_url") or "").strip()
    state = parse_watch_state(source.get("watch_state"))
    headers = {"User-Agent": "NewsDataRSS/1.0"}
    if state.get("etag"):
        headers["If-None-Match"] = str(state["etag"])
    if state.get("last_modified"):
        headers["If-Modified-Since"] = str(state["last_modified"])

    getter = http_get or _requests_get
    try:
        resp = getter(url, headers, timeout)
    except Exception as exc:
        return _failed_result("parse_failed", state, now_ms, str(exc))

    status_code = int(resp.status_code)
    if status_code == 304:
        state["last_error"] = ""
        return HtmlWatchResult(status="unchanged", entries=[], watch_state=state)
    if status_code == 429:
        return _failed_result("rate_limited", state, now_ms, f"HTTP 429: {resp.text[:120]}", resp.headers)
    if status_code in {401, 403}:
        return _failed_result("blocked", state, now_ms, f"HTTP {status_code}: {resp.text[:120]}", resp.headers)
    if status_code >= 500:
        return _failed_result("parse_failed", state, now_ms, f"HTTP {status_code}: {resp.text[:120]}", resp.headers)
    if status_code >= 400:
        return _failed_result("parse_failed", state, now_ms, f"HTTP {status_code}: {resp.text[:120]}", resp.headers)

    headers_out = _normalized_headers(resp.headers)
    if headers_out.get("etag"):
        state["etag"] = headers_out["etag"]
    if headers_out.get("last-modified"):
        state["last_modified"] = headers_out["last-modified"]

    entries = parse_html_entries(resp.text, resp.url or url)
    state["last_error"] = ""
    state["recent_keys"] = [entry.get("id") for entry in entries[:20] if entry.get("id")]
    return HtmlWatchResult(status="ok", entries=entries, watch_state=state)


def parse_html_entries(raw_html: str, base_url: str) -> List[Dict[str, Any]]:
    base = base_url.lower()
    if "developers.openai.com/api/docs/changelog" in base:
        return _parse_openai_api_changelog(raw_html, base_url)
    if "api-docs.deepseek.com" in base:
        return _parse_deepseek_updates(raw_html, base_url) or _parse_deepseek_news_links(raw_html, base_url)
    if "platform.moonshot." in base:
        return _parse_kimi_changelog(raw_html, base_url)
    if "docs.bigmodel.cn" in base:
        return _parse_zhipu_releases(raw_html, base_url)
    if "platform.claude.com/docs/" in base and "/release-notes/" in base:
        if re.search(r"(?m)^\s*###\s+[A-Za-z]+\s+\d{1,2},\s+20\d{2}\s*$", raw_html or ""):
            return _parse_dated_markdown_sections(raw_html, base_url, "Claude Platform", heading_level=3)
        return _parse_dated_html_sections(raw_html, base_url, "Claude Platform", heading_tag="h3")
    if "ai.google.dev/gemini-api/docs/changelog" in base:
        return _parse_dated_html_sections(raw_html, base_url, "Gemini API", heading_tag="h2")
    if "docs.x.ai/developers/release-notes" in base:
        if re.search(r"(?m)^\s*##\s+[A-Za-z]+(?:\s+20\d{2})?\s*$", raw_html or ""):
            return _parse_xai_markdown_release_notes(raw_html, base_url)
        return _parse_xai_release_notes(raw_html, base_url)
    if "docs.mistral.ai/resources/changelogs" in base:
        return _parse_mistral_changelog(raw_html, base_url)
    if "agent.minimax.io/docs/changelog" in base:
        return _parse_minimax_changelog(raw_html, base_url)
    if "help.aliyun.com/zh/model-studio/newly-released-models" in base:
        return _parse_aliyun_model_lifecycle(raw_html, base_url)
    if "cloud.tencent.com/document/product/1729/97765" in base:
        return _parse_tencent_hunyuan_updates(raw_html, base_url)
    if "artificialanalysis.ai/articles" in base:
        return _parse_artificial_analysis_articles(raw_html, base_url)
    if "metr.org/blog" in base:
        return _parse_metr_blog(raw_html, base_url)

    collector = _AnchorCollector()
    collector.feed(raw_html or "")
    plain_text = _clean_text(_strip_tags(raw_html or ""))
    out: List[Dict[str, Any]] = []
    seen = set()
    for anchor in collector.anchors:
        title = anchor["text"]
        href = anchor["href"]
        if not _looks_like_update(title, href, base_url):
            continue
        link = urljoin(base_url, href)
        if link in seen:
            continue
        seen.add(link)
        published_ts = _nearby_date_ts(title, plain_text)
        entry_id = link or _entry_hash(title, published_ts)
        entry: Dict[str, Any] = {
            "id": entry_id,
            "guid": entry_id,
            "link": link,
            "title": title,
            "summary": title,
        }
        if published_ts:
            entry["published_parsed"] = time.gmtime(published_ts)
            entry["published"] = formatdate(published_ts, usegmt=True)
        out.append(entry)
    return out


def _parse_deepseek_updates(raw_html: str, base_url: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    blocks = re.split(r"<h2[^>]*id=[\"']date-(\d{4}-\d{2}-\d{2})[\"'][^>]*>.*?</h2>", raw_html or "", flags=re.I | re.S)
    for i in range(1, len(blocks), 2):
        date_text = blocks[i]
        body = blocks[i + 1] if i + 1 < len(blocks) else ""
        next_h2 = re.search(r"<h2\b", body, flags=re.I)
        if next_h2:
            body = body[: next_h2.start()]
        published_ts = _date_text_to_ts(date_text)
        for heading in re.finditer(r"<h3[^>]*id=[\"']([^\"']+)[\"'][^>]*>(.*?)</h3>", body, flags=re.I | re.S):
            anchor_id = heading.group(1)
            title = _clean_text(_strip_tags(heading.group(2)))
            if not title:
                continue
            after = body[heading.end() : heading.end() + 1200]
            link_match = re.search(r"<a[^>]+href=[\"']([^\"']*/news/[^\"']+)[\"']", after, flags=re.I)
            link = urljoin(base_url, link_match.group(1)) if link_match else f"{base_url.rstrip('/')}#{anchor_id}"
            summary = _clean_text(_strip_tags(after))[:500] or title
            out.append(_make_entry(title, link, published_ts, summary))
    return out


def _parse_deepseek_news_links(raw_html: str, base_url: str) -> List[Dict[str, Any]]:
    collector = _AnchorCollector()
    collector.feed(raw_html or "")
    plain_text = _clean_text(_strip_tags(raw_html or ""))
    out: List[Dict[str, Any]] = []
    for anchor in collector.anchors:
        href = anchor["href"]
        if "/news/" not in href.lower():
            continue
        link = urljoin(base_url, href)
        title = anchor["text"]
        published_ts = _nearby_date_ts(title, plain_text)
        out.append(_make_entry(title, link, published_ts, title))
    return _dedupe_entries(out)


def _parse_kimi_changelog(raw_html: str, base_url: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    blocks = re.split(r"<h2[^>]*>(.*?)</h2>", raw_html or "", flags=re.I | re.S)
    for i in range(1, len(blocks), 2):
        date_title = _clean_text(_strip_tags(blocks[i]))
        if not re.search(r"20\d{2}年\d{1,2}月\d{1,2}日", date_title):
            continue
        body = blocks[i + 1] if i + 1 < len(blocks) else ""
        next_h2 = re.search(r"<h2\b", body, flags=re.I)
        if next_h2:
            body = body[: next_h2.start()]
        bullets = [_clean_text(_strip_tags(m.group(1))) for m in re.finditer(r"<li[^>]*>(.*?)</li>", body, flags=re.I | re.S)]
        bullets = [b for b in bullets if b]
        if not bullets:
            continue
        published_ts = _date_text_to_ts(date_title)
        title = f"Kimi 开放平台更新：{bullets[0]}"
        summary = "\n".join(bullets[:6])
        link = f"{base_url.rstrip('/')}#{date_title}"
        out.append(_make_entry(title, link, published_ts, summary))
    return out


def _parse_zhipu_releases(raw_html: str, base_url: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    pattern = re.compile(
        r'data-component-part="update-label"[^>]*>(?P<date>20\d{2}-\d{2}-\d{2})</div>'
        r".{0,1200}?"
        r'data-component-part="update-description"[^>]*>(?P<desc>.*?)</div>'
        r".{0,2000}?"
        r'<a[^>]+href=["\'](?P<href>[^"\']+)["\'][^>]*>\s*(?:<strong>)?(?P<title>.*?)(?:</strong>)?\s*</a>',
        flags=re.I | re.S,
    )
    for match in pattern.finditer(raw_html or ""):
        date_text = match.group("date")
        desc = _clean_text(_strip_tags(match.group("desc")))
        title = _clean_text(_strip_tags(match.group("title"))) or desc
        if not title or title in {"​", "公告通知"}:
            continue
        link = urljoin(base_url, match.group("href"))
        published_ts = _date_text_to_ts(date_text)
        summary = desc or title
        out.append(_make_entry(title, link, published_ts, summary))
    return _dedupe_entries(out)


def _parse_dated_html_sections(
    raw_html: str,
    base_url: str,
    source_label: str,
    heading_tag: str,
) -> List[Dict[str, Any]]:
    heading_re = re.compile(
        rf"<{heading_tag}\b(?P<attrs>[^>]*)>(?P<body>.*?)</{heading_tag}>",
        flags=re.I | re.S,
    )
    headings = []
    for match in heading_re.finditer(raw_html or ""):
        date_text = _clean_text(_strip_tags(match.group("body"))).lstrip(" ")
        published_ts = _date_text_to_ts(date_text)
        if not published_ts:
            continue
        anchor_match = re.search(r'id=["\']([^"\']+)["\']', match.group("attrs") + " " + match.group("body"), flags=re.I)
        anchor = anchor_match.group(1) if anchor_match else _slugify(date_text)
        headings.append((match, date_text, published_ts, anchor))

    out: List[Dict[str, Any]] = []
    for index, (match, date_text, published_ts, anchor) in enumerate(headings):
        body_end = headings[index + 1][0].start() if index + 1 < len(headings) else len(raw_html or "")
        summary = _clean_text(_strip_tags((raw_html or "")[match.end() : body_end]))[:1600]
        if not summary:
            continue
        first = _first_summary_line(summary)
        title = f"{source_label} 更新（{date_text}）：{first}"
        link = f"{base_url.split('#', 1)[0]}#{anchor}"
        entry = _make_entry(title[:300], link, published_ts, summary)
        _use_content_hash_id(entry, link, summary)
        out.append(entry)
    return _dedupe_entries(out)


def _parse_dated_markdown_sections(
    raw_text: str,
    base_url: str,
    source_label: str,
    heading_level: int,
) -> List[Dict[str, Any]]:
    marker = "#" * heading_level
    heading_re = re.compile(
        rf"^\s*{re.escape(marker)}\s+(?P<date>[A-Za-z]+\s+\d{{1,2}},\s+20\d{{2}})\s*$",
        flags=re.M,
    )
    headings = list(heading_re.finditer(raw_text or ""))
    page_url = re.sub(r"\.md(?:\?.*)?$", "", base_url, flags=re.I)
    out: List[Dict[str, Any]] = []
    for index, match in enumerate(headings):
        date_text = match.group("date")
        published_ts = _date_text_to_ts(date_text)
        body_end = headings[index + 1].start() if index + 1 < len(headings) else len(raw_text or "")
        summary = _clean_markdown((raw_text or "")[match.end() : body_end])[:1600]
        if not published_ts or not summary:
            continue
        link = f"{page_url}#{_slugify(date_text)}"
        entry = _make_entry(
            f"{source_label} 更新（{date_text}）：{_first_summary_line(summary)}"[:300],
            link,
            published_ts,
            summary,
        )
        _use_content_hash_id(entry, link, summary)
        out.append(entry)
    return _dedupe_entries(out)


def _parse_openai_api_changelog(raw_html: str, base_url: str) -> List[Dict[str, Any]]:
    month_re = re.compile(
        r'<h3\b[^>]*class=["\'][^"\']*ChangelogSectionTitle[^"\']*["\'][^>]*>(?P<month>[A-Za-z]+),\s*(?P<year>20\d{2})</h3>',
        flags=re.I,
    )
    months = list(month_re.finditer(raw_html or ""))
    out: List[Dict[str, Any]] = []
    for index, month in enumerate(months):
        section_end = months[index + 1].start() if index + 1 < len(months) else len(raw_html or "")
        section = (raw_html or "")[month.end() : section_end]
        item_re = re.compile(
            r'data-variant=["\']outline["\'][^>]*>(?P<date>[A-Za-z]{3}\s+\d{1,2})</div>'
            r'(?P<body>.*?)(?=(?:<div class=["\']mt-5["\']>|\Z))',
            flags=re.I | re.S,
        )
        for item in item_re.finditer(section):
            date_text = f"{item.group('date')}, {month.group('year')}"
            published_ts = _date_text_to_ts(date_text)
            summary = _clean_text(_strip_tags(item.group("body")))[:1600]
            if not published_ts or not summary:
                continue
            link = f"{base_url.split('#', 1)[0]}#{_slugify(date_text)}"
            entry = _make_entry(
                f"OpenAI API 更新（{date_text}）：{_first_summary_line(summary)}"[:300],
                link,
                published_ts,
                summary,
            )
            _use_content_hash_id(entry, link, summary)
            out.append(entry)
    return _dedupe_entries(out)


def _parse_xai_release_notes(raw_html: str, base_url: str) -> List[Dict[str, Any]]:
    heading_re = re.compile(r"<h(?P<level>[23])\b(?P<attrs>[^>]*)>(?P<body>.*?)</h(?P=level)>", flags=re.I | re.S)
    headings = list(heading_re.finditer(raw_html or ""))
    current_month = ""
    current_year = dt.datetime.now(dt.timezone.utc).year
    last_published_ts = 0
    previous_end = 0
    out: List[Dict[str, Any]] = []

    for index, match in enumerate(headings):
        level = match.group("level")
        heading = _clean_text(_strip_tags(match.group("body")))
        if level == "2":
            month_match = re.fullmatch(r"([A-Za-z]+)(?:\s+(20\d{2}))?", heading)
            if month_match:
                current_month = month_match.group(1)
                if month_match.group(2):
                    current_year = int(month_match.group(2))
            previous_end = match.end()
            continue
        if level != "3" or not heading or not current_month:
            previous_end = match.end()
            continue

        prefix = _clean_text(_strip_tags((raw_html or "")[previous_end : match.start()]))
        date_matches = re.findall(rf"\b{re.escape(current_month)}\s+(\d{{1,2}})\b", prefix, flags=re.I)
        if date_matches:
            last_published_ts = _date_text_to_ts(f"{current_month} {date_matches[-1]}, {current_year}")
        body_end = headings[index + 1].start() if index + 1 < len(headings) else len(raw_html or "")
        summary = _clean_text(_strip_tags((raw_html or "")[match.end() : body_end]))[:1600]
        anchor_match = re.search(r'id=["\']([^"\']+)["\']', match.group("attrs"), flags=re.I)
        anchor = anchor_match.group(1) if anchor_match else _slugify(heading)
        link = f"{base_url.split('#', 1)[0]}#{anchor}"
        entry = _make_entry(f"xAI API 更新：{heading}"[:300], link, last_published_ts, summary or heading)
        _use_content_hash_id(entry, link, summary or heading)
        out.append(entry)
        previous_end = match.end()
    return _dedupe_entries(out)


def _parse_xai_markdown_release_notes(raw_text: str, base_url: str) -> List[Dict[str, Any]]:
    month_re = re.compile(r"^\s*##\s+(?P<month>[A-Za-z]+)(?:\s+20\d{2})?\s*$", flags=re.I | re.M)
    months = list(month_re.finditer(raw_text or ""))
    if not months:
        return []
    first = months[0]
    section_end = months[1].start() if len(months) > 1 else len(raw_text or "")
    section = (raw_text or "")[first.end() : section_end]
    item_re = re.compile(r"^\s*###\s+(?P<title>.+?)\s*$", flags=re.M)
    items = list(item_re.finditer(section))
    page_url = re.sub(r"\.md(?:\?.*)?$", "", base_url, flags=re.I)
    out: List[Dict[str, Any]] = []
    for index, item in enumerate(items):
        body_end = items[index + 1].start() if index + 1 < len(items) else len(section)
        title = _clean_text(item.group("title"))
        summary = _clean_markdown(section[item.end() : body_end])[:1600]
        link = f"{page_url}#{_slugify(title)}"
        entry = _make_entry(f"xAI API 更新：{title}"[:300], link, 0, summary or title)
        _use_content_hash_id(entry, link, summary or title)
        out.append(entry)
    return _dedupe_entries(out)


def _parse_mistral_changelog(raw_html: str, base_url: str) -> List[Dict[str, Any]]:
    heading_re = re.compile(r"<h2\b[^>]*>(?P<body>.*?)</h2>", flags=re.I | re.S)
    headings = []
    current_year = dt.datetime.now(dt.timezone.utc).year
    previous_month = 13
    for match in heading_re.finditer(raw_html or ""):
        date_text = _clean_text(_strip_tags(match.group("body")))
        month_match = re.match(r"([A-Za-z]+)\s+\d{1,2}$", date_text)
        if not month_match:
            continue
        try:
            month_number = dt.datetime.strptime(month_match.group(1), "%B").month
        except ValueError:
            continue
        if month_number > previous_month:
            current_year -= 1
        previous_month = month_number
        published_ts = _date_text_to_ts(f"{date_text}, {current_year}")
        if published_ts:
            headings.append((match, date_text, published_ts, current_year))

    out: List[Dict[str, Any]] = []
    for index, (match, date_text, published_ts, heading_year) in enumerate(headings):
        body_end = headings[index + 1][0].start() if index + 1 < len(headings) else len(raw_html or "")
        summary = _clean_text(_strip_tags((raw_html or "")[match.end() : body_end]))[:1600]
        if not summary:
            continue
        link = f"{base_url.split('#', 1)[0]}#{_slugify(date_text + '-' + str(heading_year))}"
        title = f"Mistral 更新（{date_text}）：{_first_summary_line(summary)}"
        entry = _make_entry(title[:300], link, published_ts, summary)
        _use_content_hash_id(entry, link, summary)
        out.append(entry)
    return _dedupe_entries(out)


def _parse_minimax_changelog(raw_text: str, base_url: str) -> List[Dict[str, Any]]:
    version_re = re.compile(r"^\s*##\s+(v\d+(?:\.\d+){1,3})\s*$", flags=re.I | re.M)
    versions = list(version_re.finditer(raw_text or ""))
    page_url = re.sub(r"\.md(?:\?.*)?$", "", base_url, flags=re.I)
    out: List[Dict[str, Any]] = []
    for index, match in enumerate(versions[:20]):
        version = match.group(1)
        body_end = versions[index + 1].start() if index + 1 < len(versions) else len(raw_text or "")
        summary = _clean_text(re.sub(r"<[^>]+>", " ", (raw_text or "")[match.end() : body_end]))[:1600]
        link = f"{page_url}#{version.lower().replace('.', '')}"
        entry = _make_entry(f"MiniMax Agent {version}", link, 0, summary or version)
        _use_content_hash_id(entry, link, summary or version)
        out.append(entry)
    return _dedupe_entries(out)


def _parse_tencent_hunyuan_updates(raw_html: str, base_url: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in re.finditer(r"<tr\b[^>]*>(?P<body>.*?)</tr>", raw_html or "", flags=re.I | re.S):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row.group("body"), flags=re.I | re.S)
        if len(cells) < 3:
            continue
        title = _clean_text(_strip_tags(cells[0]))
        summary = _clean_text(_strip_tags(cells[1]))
        date_text = _clean_text(_strip_tags(cells[2]))
        published_ts = _date_text_to_ts(date_text)
        if not title or not published_ts:
            continue
        hrefs = re.findall(r'href=["\']([^"\']+)["\']', row.group("body"), flags=re.I)
        link = urljoin(base_url, hrefs[-1]) if hrefs else f"{base_url}#{date_text}"
        entry = _make_entry(f"腾讯混元更新：{title}"[:300], link, published_ts, summary or title)
        _use_content_hash_id(entry, link, summary or title)
        out.append(entry)
    return _dedupe_entries(out)


def _parse_aliyun_model_lifecycle(raw_html: str, base_url: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in re.finditer(r"<tr\b[^>]*>(?P<body>.*?)</tr>", raw_html or "", flags=re.I | re.S):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row.group("body"), flags=re.I | re.S)
        if len(cells) < 5:
            continue
        model_type = _clean_text(_strip_tags(cells[0]))
        date_text = _clean_text(_strip_tags(cells[1]))
        scope = _clean_text(_strip_tags(cells[2]))
        model = _clean_text(_strip_tags(cells[3]))
        summary = _clean_text(_strip_tags(cells[4]))
        published_ts = _date_text_to_ts(date_text)
        if not model or not published_ts:
            continue
        hrefs = re.findall(r'href=["\']([^"\']+)["\']', cells[4], flags=re.I)
        link = urljoin(base_url, hrefs[0]) if hrefs else f"{base_url}#{_slugify(model)}"
        details = "；".join(part for part in (model_type, scope, summary) if part)
        entry = _make_entry(f"阿里云百炼模型更新：{model}"[:300], link, published_ts, details or model)
        _use_content_hash_id(entry, link, details or model)
        out.append(entry)
    return _dedupe_entries(out)


def _parse_artificial_analysis_articles(raw_html: str, base_url: str) -> List[Dict[str, Any]]:
    card_re = re.compile(
        r'<a\b[^>]*href=["\'](?P<href>/articles/[^"\']+)["\'][^>]*>'
        r'.*?<h3\b[^>]*>(?P<title>.*?)</h3>'
        r'.*?<p\b[^>]*>(?P<date>[A-Za-z]+\s+\d{1,2},\s+20\d{2})</p>'
        r'.*?</a>',
        flags=re.I | re.S,
    )
    out: List[Dict[str, Any]] = []
    for card in card_re.finditer(raw_html or ""):
        title = _clean_text(_strip_tags(card.group("title")))
        date_text = _clean_text(card.group("date"))
        published_ts = _date_text_to_ts(date_text)
        if not title or not published_ts:
            continue
        link = urljoin(base_url, card.group("href"))
        entry = _make_entry(f"Artificial Analysis：{title}"[:300], link, published_ts, title)
        _use_content_hash_id(entry, link, title)
        out.append(entry)
    return _dedupe_entries(out)


def _parse_metr_blog(raw_html: str, base_url: str) -> List[Dict[str, Any]]:
    card_re = re.compile(
        r'<div\b[^>]*class=["\'][^"\']*blog-post-card[^"\']*["\'][^>]*>(?P<body>.*?)</div>\s*</div>\s*</div>',
        flags=re.I | re.S,
    )
    out: List[Dict[str, Any]] = []
    for card in card_re.finditer(raw_html or ""):
        body = card.group("body")
        href_match = re.search(r'<a\b[^>]*href=["\'](?P<href>/blog/[^"\']+)["\']', body, flags=re.I)
        title_match = re.search(r'<div\b[^>]*class=["\'][^"\']*card-title[^"\']*["\'][^>]*>(?P<title>.*?)</div>', body, flags=re.I | re.S)
        date_match = re.search(r'<div\b[^>]*class=["\'][^"\']*card-date[^"\']*["\'][^>]*>(?P<date>.*?)</div>', body, flags=re.I | re.S)
        desc_match = re.search(r'<div\b[^>]*class=["\'][^"\']*card-description[^"\']*["\'][^>]*>(?P<desc>.*?)</div>', body, flags=re.I | re.S)
        if not href_match or not title_match or not date_match:
            continue
        title = _clean_text(_strip_tags(title_match.group("title")))
        date_text = _clean_text(_strip_tags(date_match.group("date")))
        summary = _clean_text(_strip_tags(desc_match.group("desc"))) if desc_match else title
        published_ts = _date_text_to_ts(date_text)
        if not title or not published_ts:
            continue
        link = urljoin(base_url, href_match.group("href"))
        entry = _make_entry(f"METR：{title}"[:300], link, published_ts, summary or title)
        _use_content_hash_id(entry, link, summary or title)
        out.append(entry)
    return _dedupe_entries(out)


def _requests_get(url: str, headers: Dict[str, str], timeout: int) -> HtmlResponse:
    resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    if not resp.encoding or resp.encoding.lower() in {"iso-8859-1", "windows-1252"}:
        resp.encoding = resp.apparent_encoding or "utf-8"
    return HtmlResponse(
        status_code=resp.status_code,
        url=resp.url,
        text=resp.text,
        headers=dict(resp.headers),
    )


def _failed_result(
    status: str,
    state: Dict[str, Any],
    now_ms: int,
    error: str,
    headers: Optional[Dict[str, str]] = None,
) -> HtmlWatchResult:
    retry_ms = _retry_after_ms(headers or {}, now_ms)
    if not retry_ms:
        fail_count = int(state.get("fail_count") or 0) + 1
        retry_ms = now_ms + min(120, 10 * (2 ** min(fail_count - 1, 4))) * 60 * 1000
        state["fail_count"] = fail_count
    state["backoff_until"] = retry_ms
    state["last_error"] = error
    return HtmlWatchResult(status=status, entries=[], watch_state=state, error=error)


def _retry_after_ms(headers: Dict[str, str], now_ms: int) -> int:
    normalized = _normalized_headers(headers)
    raw = normalized.get("retry-after")
    if not raw:
        return 0
    try:
        return now_ms + max(0, int(raw)) * 1000
    except Exception:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _normalized_headers(headers: Dict[str, str]) -> Dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


def _strip_tags(raw: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", raw or "", flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    return re.sub(r"<[^>]+>", " ", text)


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def _clean_markdown(text: str) -> str:
    clean = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    clean = re.sub(r"`([^`]+)`", r"\1", clean)
    clean = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", clean)
    clean = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", clean)
    clean = re.sub(r"(?m)^\s{0,3}(?:#{1,6}|[-*+]|\d+[.)])\s*", "", clean)
    clean = re.sub(r"[*_~]+", "", clean)
    return _clean_text(clean)


def _make_entry(title: str, link: str, published_ts: int, summary: str) -> Dict[str, Any]:
    entry_id = link or _entry_hash(title, published_ts)
    entry: Dict[str, Any] = {
        "id": entry_id,
        "guid": entry_id,
        "link": link,
        "title": title,
        "summary": summary or title,
    }
    if published_ts:
        entry["published_parsed"] = time.gmtime(published_ts)
        entry["published"] = formatdate(published_ts, usegmt=True)
    return entry


def _dedupe_entries(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for entry in entries:
        key = entry.get("id") or entry.get("link") or entry.get("title")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def _date_text_to_ts(text: str) -> int:
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text or "")
    if match:
        year, month, day = match.groups()
        try:
            return calendar.timegm((int(year), int(month), int(day), 0, 0, 0, 0, 0, 0))
        except Exception:
            return 0

    clean = _clean_text(text or "").replace("Sept.", "Sep.")
    clean = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", clean, flags=re.I)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%b. %d, %Y", "%d %B %Y", "%d %b %Y", "%d %b. %Y"):
        try:
            parsed = dt.datetime.strptime(clean, fmt)
            return calendar.timegm((parsed.year, parsed.month, parsed.day, 0, 0, 0, 0, 0, 0))
        except ValueError:
            continue
    return 0


def _first_summary_line(summary: str, limit: int = 180) -> str:
    clean = _clean_text(summary)
    if not clean:
        return ""
    first = re.split(r"(?<=[。！？.!?])\s+", clean, maxsplit=1)[0]
    return first[:limit].rstrip(" ,，;；")


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or hashlib.sha1((text or "").encode("utf-8", errors="ignore")).hexdigest()[:12]


def _use_content_hash_id(entry: Dict[str, Any], link: str, summary: str) -> None:
    raw = f"{link}|{summary}".encode("utf-8", errors="ignore")
    entry_id = "html:" + hashlib.sha1(raw).hexdigest()
    entry["id"] = entry_id
    entry["guid"] = entry_id


def _looks_like_update(title: str, href: str, base_url: str) -> bool:
    text = f"{title} {href}".lower()
    base = base_url.lower()
    absolute_href = urljoin(base_url, href).lower()
    if "api-docs.deepseek.com" in base:
        return "/news/" in href.lower()
    if "platform.moonshot." in base:
        return "changelog" in href.lower() or "release" in text or "更新" in title
    if "docs.bigmodel.cn" in base:
        return (
            ("glm" in text or "autoglm" in text or "coding" in text or "发布" in title or "公告" in title)
            and not href.startswith("#")
            and "new-releases#" not in href
        )
    if "volcengine.com/docs/82379" in base:
        return (
            ("公告" in title or "doubao" in text or "豆包" in title or "seed" in text or "模型" in title)
            and "/docs/82379/" in absolute_href
        )
    if "help.aliyun.com/zh/model-studio" in base:
        return "qwen" in text or "模型" in title
    if "cloud.baidu.com/doc/qianfan" in base:
        return (
            ("ernie" in text or "deepseek" in text or "升级公告" in title or "模型" in title)
            and "cloud.baidu.com/doc/qianfan" in absolute_href
        )
    if "cloud.tencent.com/document/product/1729" in base:
        return "混元" in title or "hunyuan" in text or "tencent hy" in text

    positive = (
        "release",
        "update",
        "changelog",
        "发布",
        "更新",
        "公告",
        "模型",
        "deepseek",
        "kimi",
        "glm",
        "qwen",
        "doubao",
        "hunyuan",
        "ernie",
    )
    negative = ("login", "signup", "contact", "github.com", "javascript:")
    if any(token in text for token in negative):
        return False
    return any(token in text for token in positive)


def _nearby_date_ts(title: str, plain_text: str) -> int:
    idx = plain_text.find(title)
    if idx < 0:
        idx = 0
    window = plain_text[max(0, idx - 160) : idx + len(title) + 240]
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", window)
    if not match:
        return 0
    year, month, day = match.groups()
    month = month.strip()
    day = day.strip()
    try:
        return calendar.timegm((int(year), int(month), int(day), 0, 0, 0, 0, 0, 0))
    except Exception:
        return 0


def _entry_hash(title: str, published_ts: int) -> str:
    raw = f"{title}|{published_ts}".encode("utf-8", errors="ignore")
    return "html:" + hashlib.sha1(raw).hexdigest()


def _source_jitter_ms(source: Dict[str, Any]) -> int:
    raw = str(source.get("record_id") or source.get("feed_url") or source.get("name") or "")
    if not raw:
        return 0
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()
    return (30 + (int(digest[:4], 16) % 91)) * 1000
