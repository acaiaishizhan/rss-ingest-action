# -*- coding: utf-8 -*-
import html
import json
import os
import re
import subprocess
from html.parser import HTMLParser
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlparse

import requests

import config
from http_safety import fetch_public_content, is_public_http_url_literal
from rss_parser import entry_text_content


MIN_USEFUL_TEXT_LENGTH = 120

_GENERIC_EXCLUDED_HOSTS = {
    "github.com",
    "gist.github.com",
    "m.youtube.com",
    "mobile.twitter.com",
    "news.ycombinator.com",
    "news.ycombinator.org",
    "old.reddit.com",
    "reddit.com",
    "twitter.com",
    "www.github.com",
    "www.reddit.com",
    "www.twitter.com",
    "www.youtube.com",
    "x.com",
    "youtu.be",
    "youtube.com",
}
_GENERIC_EXCLUDED_SUFFIXES = (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".mp3",
    ".mp4",
    ".zip",
)


def _http_get(url: str, headers: Optional[Dict[str, str]], timeout: int) -> requests.Response:
    return fetch_public_content(
        url,
        headers=headers,
        timeout=timeout,
        max_bytes=max(1, int(getattr(config, "ARTICLE_FETCH_MAX_BYTES", 2 * 1024 * 1024) or 1)),
        use_system_proxy=bool(config.USE_SYSTEM_PROXY),
    )


def _clean_text(raw: str) -> str:
    text = html.unescape(raw or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


class _TargetTextParser(HTMLParser):
    def __init__(self, target: str):
        super().__init__(convert_charrefs=True)
        self.target = target
        self.depth = 0
        self.skip_depth = 0
        self.parts = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_dict = {name.lower(): value or "" for name, value in attrs}
        attr_text = " ".join(attrs_dict.values()).lower()

        if self.depth <= 0 and self._matches_target(tag, attrs_dict, attr_text):
            self.depth = 1
            return

        if self.depth > 0:
            self.depth += 1
            if self.skip_depth > 0:
                self.skip_depth += 1
            elif self._should_skip(tag, attr_text):
                self.skip_depth = 1
            elif tag in {"p", "h1", "h2", "h3", "li", "tr", "br"}:
                self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.depth <= 0:
            return
        if self.skip_depth > 0:
            self.skip_depth -= 1
        if tag in {"p", "h1", "h2", "h3", "li", "tr"} and self.skip_depth <= 0:
            self.parts.append("\n")
        self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.depth > 0 and self.skip_depth <= 0:
            text = _clean_text(data)
            if text:
                self.parts.append(text)

    def _matches_target(self, tag: str, attrs: Dict[str, str], attr_text: str) -> bool:
        if tag != "div":
            return False
        if self.target == "huggingface":
            return "blog-content" in attr_text
        if self.target == "ithome":
            return attrs.get("id") == "paragraph" or "post_content" in attr_text
        return False

    def _should_skip(self, tag: str, attr_text: str) -> bool:
        if tag in {"script", "style", "svg", "nav", "dialog", "form", "button"}:
            return True
        return any(
            marker in attr_text
            for marker in (
                "not-prose",
                "svelte_hydrater",
                "related_post",
                "ad-tips",
                "share",
                "comment",
            )
        )

    def text(self) -> str:
        return _clean_text("\n".join(self.parts))


def _extract_target_text(raw_html: str, target: str) -> str:
    parser = _TargetTextParser(target)
    parser.feed(raw_html or "")
    return parser.text()


class _XOembedTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag == "br" and self.depth > 0:
            self.parts.append("\n")
            return
        if tag == "p" and self.depth <= 0:
            self.depth = 1
            return
        if self.depth > 0:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self.depth <= 0:
            return
        if tag == "p":
            self.parts.append("\n")
        self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.depth > 0:
            text = _clean_text(data)
            if text:
                self.parts.append(text)

    def text(self) -> str:
        return _clean_text(" ".join(self.parts))


def _extract_x_oembed_text(raw_html: str) -> str:
    parser = _XOembedTextParser()
    parser.feed(raw_html or "")
    text = parser.text()
    if text:
        return text
    return _clean_text(re.sub(r"<[^>]+>", " ", html.unescape(raw_html or "")))


class _GenericArticleTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.parts = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_dict = {name.lower(): value or "" for name, value in attrs}
        attr_text = " ".join(attrs_dict.values()).lower()
        if self.skip_depth > 0:
            self.skip_depth += 1
            return
        if tag in {"script", "style", "svg", "nav", "header", "footer", "aside", "form", "button"}:
            self.skip_depth = 1
            return
        if any(marker in attr_text for marker in ("comment", "cookie", "newsletter", "subscribe", "share")):
            self.skip_depth = 1
            return
        if tag in {"article", "main", "section", "p", "h1", "h2", "h3", "li", "pre", "blockquote", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth > 0:
            self.skip_depth -= 1
            return
        if tag in {"article", "main", "section", "p", "h1", "h2", "h3", "li", "pre", "blockquote"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth > 0:
            return
        text = _clean_text(data)
        if text:
            self.parts.append(text)

    def text(self) -> str:
        return _clean_text("\n".join(self.parts))


def _extract_generic_article_text(raw_html: str) -> str:
    parser = _GenericArticleTextParser()
    parser.feed(raw_html or "")
    text = parser.text()
    # Drop common boilerplate left by full-page parsing.
    text = re.sub(r"(?i)\b(skip to content|all rights reserved|privacy policy|terms of service)\b", " ", text)
    return _clean_text(text)


def _is_huggingface_blog(url: str, source_name: str, feed_url: str) -> bool:
    parsed = urlparse(url or feed_url or "")
    return parsed.hostname == "huggingface.co" and (
        "/blog/" in (parsed.path or "") or "hugging face blog" in (source_name or "").lower()
    )


def _is_ithome_article(url: str, source_name: str, feed_url: str) -> bool:
    parsed = urlparse(url or feed_url or "")
    host = (parsed.hostname or "").lower()
    return host.endswith("ithome.com") and (
        re.match(r"^/0/\d+/\d+\.htm$", parsed.path or "") is not None
        or "it之家" in (source_name or "").lower()
        or "ithome" in (feed_url or "").lower()
    )


def _is_hacker_news_source(source_name: str, feed_url: str) -> bool:
    text = f"{source_name} {feed_url}".lower()
    return "hacker news" in text or "hnrss.org" in text or "news.ycombinator.com" in text


def _is_medium_article(url: str, source_name: str, feed_url: str) -> bool:
    parsed = urlparse(url or feed_url or "")
    host = (parsed.hostname or "").lower()
    source_text = f"{source_name} {feed_url}".lower()
    if host in {"medium.com", "www.medium.com", "pub.towardsai.net"}:
        return True
    if host.endswith(".medium.com") or host.endswith(".towardsai.net"):
        return True
    return "medium" in source_text and _is_generic_article_candidate(url)


def _is_medium_preview_text(text: str) -> bool:
    normalized = _clean_text(text).lower()
    return "continue reading on" in normalized and ("medium" in normalized or "towards ai" in normalized)


def _is_x_status_url(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    return host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"} and re.search(
        r"/status/\d+", parsed.path or ""
    ) is not None


def _x_status_id(url: str) -> str:
    match = re.search(r"/status/(\d+)", url or "")
    return match.group(1) if match else ""


def _is_hacker_news_link_stub(text: str) -> bool:
    normalized = _clean_text(text).lower()
    return (
        "article url:" in normalized
        and "comments url:" in normalized
        and "news.ycombinator.com/item" in normalized
    )


def _is_generic_article_candidate(url: str) -> bool:
    parsed = urlparse(url or "")
    if not is_public_http_url_literal(url):
        return False
    host = (parsed.hostname or "").lower()
    if not host or host in _GENERIC_EXCLUDED_HOSTS:
        return False
    path = (parsed.path or "").lower()
    if path.endswith(_GENERIC_EXCLUDED_SUFFIXES):
        return False
    if path.rstrip("/").endswith(("/feed", "/rss", "/atom")):
        return False
    return True


def _default_browser_endpoint_file() -> str:
    local_app_data = os.getenv("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(local_app_data, "gpt-browser", "state", "chrome-ws-endpoint.txt")


def _default_browser_node_modules() -> str:
    app_data = os.getenv("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(app_data, "npm", "node_modules", "gpt-browser", "node_modules")


def _fetch_browser_article_text(url: str, timeout: int) -> str:
    endpoint_file = getattr(config, "BROWSER_ARTICLE_FETCH_ENDPOINT_FILE", "") or _default_browser_endpoint_file()
    if not os.path.exists(endpoint_file):
        raise RuntimeError(f"browser endpoint file not found: {endpoint_file}")

    node_modules = getattr(config, "BROWSER_ARTICLE_FETCH_NODE_MODULES", "") or _default_browser_node_modules()
    command = getattr(config, "BROWSER_ARTICLE_FETCH_COMMAND", "node") or "node"
    timeout_ms = max(1000, int(timeout or 12) * 1000)
    script = r"""
const fs = require('fs');
const puppeteer = require('puppeteer-core');

const endpointFile = process.env.BROWSER_ARTICLE_FETCH_ENDPOINT_FILE;
const targetUrl = process.env.BROWSER_ARTICLE_FETCH_URL;
const timeoutMs = Number(process.env.BROWSER_ARTICLE_FETCH_TIMEOUT_MS || 12000);

(async () => {
  const endpoint = fs.readFileSync(endpointFile, 'utf8').trim();
  const browser = await puppeteer.connect({ browserWSEndpoint: endpoint, defaultViewport: null });
  const pages = await browser.pages();
  const urlKey = new URL(targetUrl).pathname.split('/').filter(Boolean).pop() || targetUrl;
  let page = pages.find((item) => item.url().startsWith(targetUrl) || item.url().includes(urlKey));
  let shouldClose = false;
  if (!page) {
    page = await browser.newPage();
    shouldClose = true;
    await page.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
  } else {
    await page.bringToFront();
  }
  await page.waitForSelector('body', { timeout: Math.min(timeoutMs, 10000) }).catch(() => {});
  for (let i = 0; i < 12; i += 1) {
    await page.evaluate(() => window.scrollBy(0, Math.floor(window.innerHeight * 0.9))).catch(() => {});
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  const payload = await page.evaluate(() => {
    const visible = (el) => {
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width >= 0 && rect.height >= 0;
    };
    const article = document.querySelector('article') || document.querySelector('main') || document.body;
    const blocks = Array.from(document.querySelectorAll('article h1, article h2, article h3, article p, article pre, article li'))
      .filter(visible)
      .map((el) => (el.innerText || '').trim())
      .filter(Boolean);
    const text = (blocks.length ? blocks.join('\n\n') : (article.innerText || document.body.innerText || ''))
      .replace(/[ \t]+\n/g, '\n')
      .replace(/\n[ \t]+/g, '\n')
      .trim();
    return { text, href: location.href, title: document.title, blockCount: blocks.length };
  });
  if (shouldClose) await page.close().catch(() => {});
  await browser.disconnect();
  console.log(JSON.stringify(payload));
})().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
"""
    env = os.environ.copy()
    if node_modules:
        existing = env.get("NODE_PATH", "")
        env["NODE_PATH"] = node_modules if not existing else f"{node_modules}{os.pathsep}{existing}"
    env["BROWSER_ARTICLE_FETCH_ENDPOINT_FILE"] = endpoint_file
    env["BROWSER_ARTICLE_FETCH_URL"] = url
    env["BROWSER_ARTICLE_FETCH_TIMEOUT_MS"] = str(timeout_ms)
    completed = subprocess.run(
        [command, "-e", script],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=max(5, int(timeout or 12) + 8),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "browser article fetch failed").strip()[:500])
    try:
        payload = json.loads((completed.stdout or "").strip())
    except Exception as exc:
        raise RuntimeError(f"browser article fetch returned invalid JSON: {(completed.stdout or '')[:200]}") from exc
    text = _clean_text(str(payload.get("text") or ""))
    if not text:
        raise RuntimeError("browser article fetch returned empty text")
    return text


def _json_object_after_key(raw: str, key: str) -> Dict[str, Any]:
    if not key:
        return {}
    marker = json.dumps(str(key), ensure_ascii=False) + ":{"
    start = (raw or "").find(marker)
    if start < 0:
        return {}
    object_start = start + len(marker) - 1
    depth = 0
    in_string = False
    escaped = False
    for index in range(object_start, len(raw)):
        ch = raw[index]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    payload = json.loads(raw[object_start : index + 1])
                    return payload if isinstance(payload, dict) else {}
                except Exception:
                    return {}
    return {}


def _format_x_status_text(payload: Dict[str, Any], user_payload: Optional[Dict[str, Any]] = None) -> str:
    text = _clean_text(str(payload.get("full_text") or payload.get("text") or ""))
    if not text:
        return ""
    parts = ["X/Twitter 原帖"]
    user_payload = user_payload or {}
    name = _clean_text(str(user_payload.get("name") or ""))
    screen_name = _clean_text(str(user_payload.get("screen_name") or ""))
    if name and screen_name:
        parts.append(f"作者：{name} (@{screen_name})")
    elif screen_name:
        parts.append(f"作者：@{screen_name}")
    created_at = _clean_text(str(payload.get("created_at") or ""))
    if created_at:
        parts.append(f"发布时间：{created_at}")
    parts.append(f"正文：{text}")
    return "\n".join(parts)


def _x_screen_name_from_author_url(author_url: str) -> str:
    parsed = urlparse(author_url or "")
    parts = [part for part in (parsed.path or "").split("/") if part]
    if not parts:
        return ""
    screen_name = parts[0].strip("@")
    return screen_name if screen_name.lower() not in {"i", "home", "search"} else ""


def _format_x_oembed_status_text(payload: Dict[str, Any]) -> str:
    text = _extract_x_oembed_text(str(payload.get("html") or ""))
    if not text:
        return ""
    parts = ["X/Twitter 原帖"]
    name = _clean_text(str(payload.get("author_name") or ""))
    screen_name = _x_screen_name_from_author_url(str(payload.get("author_url") or ""))
    if name and screen_name:
        parts.append(f"作者：{name} (@{screen_name})")
    elif name:
        parts.append(f"作者：{name}")
    elif screen_name:
        parts.append(f"作者：@{screen_name}")
    parts.append(f"正文：{text}")
    return "\n".join(parts)


def _fetch_embedded_x_status_text(url: str, timeout: int) -> str:
    tweet_id = _x_status_id(url)
    if not tweet_id:
        raise RuntimeError("x status id not found")
    resp = _http_get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {(resp.text or '')[:200]}")
    if getattr(resp, "apparent_encoding", None) and (
        not getattr(resp, "encoding", None) or str(resp.encoding).lower() in {"iso-8859-1", "latin-1"}
    ):
        resp.encoding = resp.apparent_encoding

    payload = _json_object_after_key(resp.text or "", tweet_id)
    if not payload:
        raise RuntimeError("embedded x status payload not found")
    user_payload = _json_object_after_key(resp.text or "", str(payload.get("user") or ""))
    text = _format_x_status_text(payload, user_payload=user_payload)
    if not text:
        raise RuntimeError("embedded x status text not found")
    return text


def _fetch_oembed_x_status_text(url: str, timeout: int) -> str:
    oembed_url = "https://publish.twitter.com/oembed?" + urlencode(
        {
            "url": url,
            "omit_script": "true",
            "dnt": "true",
        }
    )
    resp = _http_get(
        oembed_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Accept": "application/json,text/javascript,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {(resp.text or '')[:200]}")
    try:
        payload = json.loads(resp.text or "{}")
    except Exception as exc:
        raise RuntimeError(f"oembed x status returned invalid JSON: {(resp.text or '')[:200]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("oembed x status returned non-object JSON")
    text = _format_x_oembed_status_text(payload)
    if not text:
        raise RuntimeError("oembed x status text not found")
    return text


def _fetch_x_status_text(url: str, timeout: int) -> str:
    errors = []
    try:
        return _fetch_embedded_x_status_text(url, timeout)
    except Exception as exc:
        errors.append(f"embedded x fetch failed: {exc}")

    try:
        return _fetch_oembed_x_status_text(url, timeout)
    except Exception as exc:
        errors.append(f"oembed x fetch failed: {exc}")

    if bool(getattr(config, "ENABLE_X_BROWSER_FALLBACK", False)):
        try:
            return _fetch_browser_x_status_text(url, timeout)
        except Exception as exc:
            errors.append(f"browser x fetch failed: {exc}")
    else:
        errors.append("browser x fallback disabled")

    raise RuntimeError("; ".join(errors))


def _fetch_browser_x_status_text(url: str, timeout: int) -> str:
    endpoint_file = getattr(config, "BROWSER_ARTICLE_FETCH_ENDPOINT_FILE", "") or _default_browser_endpoint_file()
    if not os.path.exists(endpoint_file):
        raise RuntimeError(f"browser endpoint file not found: {endpoint_file}")

    node_modules = getattr(config, "BROWSER_ARTICLE_FETCH_NODE_MODULES", "") or _default_browser_node_modules()
    command = getattr(config, "BROWSER_ARTICLE_FETCH_COMMAND", "node") or "node"
    timeout_ms = max(1000, int(timeout or 12) * 1000)
    script = r"""
const fs = require('fs');
const puppeteer = require('puppeteer-core');

const endpointFile = process.env.BROWSER_ARTICLE_FETCH_ENDPOINT_FILE;
const targetUrl = process.env.BROWSER_ARTICLE_FETCH_URL;
const timeoutMs = Number(process.env.BROWSER_ARTICLE_FETCH_TIMEOUT_MS || 12000);

const normalizeText = (value) => String(value || '').replace(/\s+/g, ' ').trim();

(async () => {
  const endpoint = fs.readFileSync(endpointFile, 'utf8').trim();
  const browser = await puppeteer.connect({ browserWSEndpoint: endpoint, defaultViewport: null });
  const pages = await browser.pages();
  const tweetId = (targetUrl.match(/status\/(\d+)/) || [])[1] || '';
  const canonicalUrl = tweetId ? `https://x.com/i/web/status/${tweetId}` : targetUrl;
  let page = pages.find((item) => tweetId && item.url().includes(tweetId));
  let shouldClose = false;
  if (!page) {
    page = await browser.newPage();
    shouldClose = true;
  }
  await page.goto(canonicalUrl, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
  await page.waitForSelector('article', { timeout: Math.min(timeoutMs, 10000) }).catch(() => {});
  await new Promise((resolve) => setTimeout(resolve, 1200));
  const payload = await page.evaluate((tweetId) => {
    const articles = Array.from(document.querySelectorAll('article'));
    const article = articles.find((node) => {
      if (!tweetId) return false;
      return Array.from(node.querySelectorAll('a[href*="/status/"]')).some((a) => (a.getAttribute('href') || '').includes(tweetId));
    }) || articles[0] || document.body;

    const text = Array.from(article.querySelectorAll('[data-testid="tweetText"]'))
      .map((node) => node.innerText || '')
      .map((value) => value.trim())
      .filter(Boolean)
      .join('\n\n');
    const time = article.querySelector('time');
    const userName = article.querySelector('[data-testid="User-Name"]');
    const media = Array.from(article.querySelectorAll('img[src*="pbs.twimg.com/media"], img[src*="pbs.twimg.com/card_img"]'))
      .map((img) => img.src)
      .filter(Boolean);
    return {
      text,
      datetime: time ? time.getAttribute('datetime') : '',
      author: userName ? userName.innerText : '',
      media: Array.from(new Set(media)).slice(0, 8),
      pageText: document.body ? document.body.innerText : '',
      href: location.href,
    };
  }, tweetId);
  if (shouldClose) await page.close().catch(() => {});
  await browser.disconnect();
  console.log(JSON.stringify(payload));
})().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
"""
    env = os.environ.copy()
    if node_modules:
        existing = env.get("NODE_PATH", "")
        env["NODE_PATH"] = node_modules if not existing else f"{node_modules}{os.pathsep}{existing}"
    env["BROWSER_ARTICLE_FETCH_ENDPOINT_FILE"] = endpoint_file
    env["BROWSER_ARTICLE_FETCH_URL"] = url
    env["BROWSER_ARTICLE_FETCH_TIMEOUT_MS"] = str(timeout_ms)
    completed = subprocess.run(
        [command, "-e", script],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=max(5, int(timeout or 12) + 8),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "browser x status fetch failed").strip()[:500])
    try:
        payload = json.loads((completed.stdout or "").strip())
    except Exception as exc:
        raise RuntimeError(f"browser x status fetch returned invalid JSON: {(completed.stdout or '')[:200]}") from exc

    text = _clean_text(str(payload.get("text") or ""))
    if not text:
        body_text = str(payload.get("pageText") or "").lower()
        if "log in" in body_text or "登录" in body_text:
            raise RuntimeError("x status fetch requires login")
        raise RuntimeError("browser x status fetch returned empty text")

    parts = ["X/Twitter 原帖"]
    author = _clean_text(str(payload.get("author") or ""))
    if author:
        parts.append(f"作者：{author}")
    datetime_value = _clean_text(str(payload.get("datetime") or ""))
    if datetime_value:
        parts.append(f"发布时间：{datetime_value}")
    parts.append(f"正文：{text}")
    media = payload.get("media") or []
    if isinstance(media, list) and media:
        parts.append("媒体：" + " ".join(str(item) for item in media[:8]))
    return "\n".join(parts)


def _fetch_source_text(url: str, target: str, timeout: int) -> str:
    if target == "browser_medium_article":
        return _fetch_browser_article_text(url, timeout)
    if target == "x_status_browser":
        return _fetch_x_status_text(url, timeout)
    resp = _http_get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {(resp.text or '')[:200]}")
    if getattr(resp, "apparent_encoding", None) and (
        not getattr(resp, "encoding", None) or str(resp.encoding).lower() in {"iso-8859-1", "latin-1"}
    ):
        resp.encoding = resp.apparent_encoding
    content_type = (resp.headers.get("Content-Type") or "") if hasattr(resp, "headers") else ""
    if content_type and "html" not in content_type.lower() and "text/" not in content_type.lower():
        raise RuntimeError(f"unsupported content type: {content_type}")
    if target == "generic_article":
        return _extract_generic_article_text(resp.text or "")
    return _extract_target_text(resp.text or "", target)


def extract_article_text(
    url: str,
    source_name: str,
    feed_url: str,
    entry: Dict[str, Any],
    timeout: int = 12,
    min_length: int = MIN_USEFUL_TEXT_LENGTH,
    force_fetch: bool = False,
) -> Dict[str, Any]:
    rss_text = _clean_text(entry_text_content(entry))
    method = "rss_content" if entry.get("content") else ("rss_summary" if rss_text else "none")
    result = {
        "text": rss_text,
        "method": method,
        "status": "ok" if len(rss_text) >= min_length else ("short" if rss_text else "empty"),
        "error": "",
        "content_length": len(rss_text),
        "raw_excerpt_length": len(rss_text),
    }

    hn_link_stub = _is_hacker_news_source(source_name, feed_url) and _is_hacker_news_link_stub(rss_text)
    needs_medium_browser = _is_medium_article(url, source_name, feed_url) and (
        len(rss_text) < min_length or _is_medium_preview_text(rss_text)
    )
    browser_fetch_enabled = bool(getattr(config, "ENABLE_BROWSER_ARTICLE_FETCH", False))

    if (
        not force_fetch
        and rss_text
        and len(rss_text) >= min_length
        and not hn_link_stub
        and not (needs_medium_browser and browser_fetch_enabled)
    ):
        return result
    if not url:
        return result

    target = ""
    source_method = ""
    if _is_huggingface_blog(url, source_name, feed_url):
        target = "huggingface"
        source_method = "source_parser:huggingface_blog"
    elif _is_ithome_article(url, source_name, feed_url):
        target = "ithome"
        source_method = "source_parser:ithome_article"
    elif force_fetch and _is_x_status_url(url):
        target = "x_status_browser"
        source_method = "source_parser:x_status_browser"
    elif needs_medium_browser and browser_fetch_enabled:
        target = "browser_medium_article"
        source_method = "source_parser:browser_medium_article"
    elif hn_link_stub and _is_hacker_news_source(source_name, feed_url):
        parsed = urlparse(url or "")
        if (parsed.hostname or "").lower() not in {"news.ycombinator.com", "news.ycombinator.org"}:
            target = "generic_article"
            source_method = "source_parser:hacker_news_article"
    elif _is_generic_article_candidate(url):
        target = "generic_article"
        source_method = "source_parser:generic_article"

    if not target:
        return result

    try:
        text = _fetch_source_text(url, target, timeout)
        if not text:
            result["error"] = f"{source_method} returned empty text"
            return result
        if target in {"generic_article", "browser_medium_article"} and len(text) < min_length and rss_text:
            result["status"] = "short"
            result["error"] = f"{source_method} returned short text"
            return result
        result.update(
            {
                "text": text,
                "method": source_method,
                "status": "ok" if target == "x_status_browser" or len(text) >= min_length else "short",
                "content_length": len(text),
            }
        )
    except Exception as exc:
        error_text = str(exc)
        error_lower = error_text.lower()
        result["status"] = "fetch_error" if "HTTP" in error_text or "timeout" in error_lower or "timed out" in error_lower else "parse_error"
        result["error"] = error_text
    return result
