"""Capture the relevant public SoPilot six-hour rankings as a local RSS feed."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SOURCE_URL = "https://sopilot.net/zh/rank/tweets?range=6h"
CATEGORIES = ("AI", "Creator")
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

STRONG_RELEVANCE_TERMS = (
    "AI", "AIGC", "Agent", "智能体", "大模型", "模型", "LLM", "GPT", "ChatGPT",
    "Claude", "Codex", "Gemini", "DeepSeek", "Grok", "OpenAI", "Anthropic", "xAI",
    "Prompt", "提示词", "Skill", "MCP", "API", "vibe coding", "编程", "代码", "GitHub",
    "开源", "自动化", "工作流", "Computer Use", "生图", "图像生成", "视频生成", "数字人",
    "Seedance", "即梦", "豆包", "可灵", "Vidu", "ComfyUI", "Obsidian", "Remotion",
    "Blender", "Figma", "飞书", "Notion", "RSS", "浏览器自动化", "Browser", "CLI",
)
DIRECT_CREATOR_TERMS = (
    "创作者权益", "内容生产", "内容创作", "短视频", "公众号", "小红书", "抖音", "视频号",
    "独立开发", "一人公司", "订阅收入", "用户增长", "产品增长", "账号增长",
)
CREATOR_CONTEXT_TERMS = (
    "创作者", "自媒体", "口播", "剪辑", "封面", "配图", "选题", "变现", "获客", "MRR",
    "创业", "收入", "营收", "定价", "运营", "营销", "SEO", "广告", "粉丝", "直播", "社群",
    "平台权益", "申诉",
)


@dataclass(frozen=True)
class Capture:
    entries: list[dict]
    discovered: int
    prefiltered: int
    pages: tuple[str, ...]


def _has_term(text: str, term: str) -> bool:
    if term.isascii() and len(term) <= 3:
        return re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])", text) is not None
    return term.lower() in text


def is_relevant_tweet(tweet: dict) -> bool:
    text = str(tweet.get("text") or "").lower()
    strong = [_has_term(text, term) for term in STRONG_RELEVANCE_TERMS]
    direct = [_has_term(text, term) for term in DIRECT_CREATOR_TERMS]
    context = [_has_term(text, term) for term in CREATOR_CONTEXT_TERMS]
    return any(strong) or any(direct) or sum(context) >= 2


def parse_rank_page(raw: str, category: str, page: int) -> dict:
    chunks: list[str] = []
    for match in re.finditer(r"self\.__next_f\.push\((\[.*?\])\)\s*;?\s*</script>", raw, re.S):
        args = json.loads(match[1])
        if len(args) > 1 and isinstance(args[1], str):
            chunks.append(args[1])
    stream = "".join(chunks).encode("utf-8")
    records: dict[str, object] = {}
    position = 0
    while position < len(stream):
        header = re.match(rb"([0-9a-f]+):", stream[position:])
        if not header:
            raise ValueError("Unrecognized SoPilot public data frame")
        key = header[1].decode()
        position += header.end()
        if stream[position:position + 1] == b"T":
            end = stream.index(b",", position)
            size = int(stream[position + 1:end], 16)
            value = stream[end + 1:end + 1 + size]
            if len(value) != size:
                raise ValueError("Truncated SoPilot text frame")
            records[key] = value.decode("utf-8")
            position = end + 1 + size
            continue
        end = stream.find(b"\n", position)
        if end < 0:
            end = len(stream)
        try:
            records[key] = json.loads(stream[position:end])
        except (ValueError, UnicodeDecodeError):
            pass
        position = end + 1

    found: list[dict] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if "risingTweets" in value and "hotTweets" in value:
                found.append(value)
            else:
                for child in value.values():
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for value in records.values():
        visit(value)
    if len(found) != 1:
        raise ValueError("Missing or ambiguous SoPilot ranking data")
    props = found[0]
    if (props.get("range"), props.get("category"), props.get("page")) != ("6h", category, page):
        raise ValueError("SoPilot returned a different window/category/page")
    total = props.get("totalPages")
    if not isinstance(total, int) or not 1 <= page <= total <= 100:
        raise ValueError("Invalid SoPilot pagination; refusing partial capture")
    for name in ("risingTweets", "hotTweets"):
        tweets = props.get(name)
        if not isinstance(tweets, list):
            raise ValueError("Invalid SoPilot tweet list")
        for tweet in tweets:
            if not isinstance(tweet, dict) or not re.fullmatch(r"\d+", str(tweet.get("id", ""))):
                raise ValueError("Invalid SoPilot tweet ID")
            text = tweet.get("text")
            if isinstance(text, str) and re.fullmatch(r"\$[0-9a-f]+", text):
                text = records.get(text[1:])
            if not isinstance(text, str):
                raise ValueError("Unresolved SoPilot tweet text")
            tweet["text"] = text[1:] if text.startswith("$$") else text
            dt.datetime.fromisoformat(str(tweet["tweetCreatedAt"]).replace("Z", "+00:00"))
    return props


def _capture_category(fetch_text: Callable[[str], str], category: str) -> list[tuple[str, dict]]:
    captured: list[tuple[str, dict]] = []
    page = total = 1
    while page <= total:
        params = {"range": "6h", "category": category}
        if page > 1:
            params["page"] = page
        url = "https://sopilot.net/zh/rank/tweets?" + urlencode(params)
        props = parse_rank_page(fetch_text(url), category, page)
        total = props["totalPages"]
        captured.append((url, props))
        page += 1
    return captured


def fetch_sopilot(fetch_text: Callable[[str], str]) -> Capture:
    pages: list[tuple[str, dict]] = []
    with ThreadPoolExecutor(max_workers=len(CATEGORIES)) as pool:
        for captured in pool.map(lambda category: _capture_category(fetch_text, category), CATEGORIES):
            pages.extend(captured)

    tweets: dict[str, dict] = {}
    for _, props in pages:
        for tweet in [*props["risingTweets"], *props["hotTweets"]]:
            key = str(tweet["id"])
            previous = tweets.get(key)
            if previous is None or len(str(tweet["text"])) > len(str(previous["text"])):
                tweets[key] = dict(tweet)
    discovered = len(tweets)
    relevant = [tweet for tweet in tweets.values() if is_relevant_tweet(tweet)]
    entries = []
    for tweet in sorted(relevant, key=lambda item: str(item["id"])):
        author = str(tweet.get("screenName") or "")
        if not re.fullmatch(r"[A-Za-z0-9_]+", author):
            raise ValueError("Invalid SoPilot author handle")
        text = str(tweet["text"])
        first_line = next((line for line in text.splitlines() if line.strip()), "推文")
        entries.append({
            "id": f"https://x.com/i/status/{tweet['id']}",
            "link": f"https://x.com/{author}/status/{tweet['id']}",
            "title": f"{tweet.get('authorName') or author}：{first_line[:180]}",
            "published": dt.datetime.fromisoformat(str(tweet["tweetCreatedAt"]).replace("Z", "+00:00")),
            "content": text,
        })
    return Capture(
        entries=entries,
        discovered=discovered,
        prefiltered=discovered - len(entries),
        pages=tuple(url for url, _ in pages),
    )


def render_rss(capture: Capture) -> bytes:
    ET.register_namespace("content", CONTENT_NS)
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = "SoPilot 6小时推文榜"
    ET.SubElement(channel, "link").text = SOURCE_URL
    ET.SubElement(channel, "description").text = "AI 与 Creator 榜单中预筛出的资讯"
    ET.SubElement(channel, "lastBuildDate").text = dt.datetime.now(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    for entry in capture.entries:
        stored_content = html.escape(entry["content"])
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = entry["title"]
        ET.SubElement(item, "link").text = entry["link"]
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = entry["id"]
        ET.SubElement(item, "pubDate").text = entry["published"].astimezone(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        # FeedParser exposes RSS content as HTML. Escape once here and once via
        # ElementTree so code such as ``print('<value>')`` arrives as text, not
        # as a discarded pseudo-tag. The SoPilot source parser unescapes it.
        ET.SubElement(item, "description").text = stored_content
        ET.SubElement(item, f"{{{CONTENT_NS}}}encoded").text = stored_content
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def write_capture(path: Path, capture: Capture) -> None:
    payload = render_rss(capture)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "rss-ingest-sopilot-watch/1.0"})
    with urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    capture = fetch_sopilot(_fetch_text)
    write_capture(args.output, capture)
    print(json.dumps({"entries": len(capture.entries), "discovered": capture.discovered, "prefiltered": capture.prefiltered}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
