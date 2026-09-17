"""Read the public six-hour rankings, preserving the server's tweet text."""
import datetime as dt
import json
import re
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse

SOURCE_URL = "https://sopilot.net/zh/rank/tweets?range=6h"
# The unfiltered ranking is dominated by finance, celebrity, violence and other
# general-interest posts. AI and Creator retain the two business-relevant pools;
# semantic relevance is still decided by the existing NEWS + Info stages.
CATEGORIES = ("AI", "Creator")


def is_retryable_partial_receipt(receipt, batch_id=""):
    """Return true for bounded retries that cannot duplicate durable writes."""
    stats = (receipt or {}).get("stats") or {}
    durable_failures = (
        "filtered_log_failed", "feishu_create_failed",
        "secondary_sync_failed", "source_state_update_failed",
    )
    common = (
        isinstance(receipt, dict)
        and (not batch_id or receipt.get("batch_id") == batch_id)
        and receipt.get("complete") is False
        and not receipt.get("error")
        and isinstance(receipt.get("news_records"), list)
        and all(int(stats.get(key) or 0) == 0 for key in durable_failures)
    )
    if not common:
        return False
    partial_llm = (
        int(stats.get("sources_processed") or 0) == 1
        and int(stats.get("sources_failed") or 0) == 0
        and int(stats.get("llm_failed") or 0) > 0
        and int(stats.get("llm_failed") or 0) < int(stats.get("queue_total") or 0)
        and int(receipt.get("remaining_failed_items") or 0) > 0
    )
    transient_source = (
        int(stats.get("sources_processed") or 0) == 0
        and int(stats.get("sources_failed") or 0) > 0
        and int(stats.get("queue_total") or 0) == 0
        and int(stats.get("llm_failed") or 0) == 0
        and int(receipt.get("remaining_failed_items") or 0) == 0
        and not receipt.get("news_records")
    )
    return partial_llm or transient_source

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


def _has_term(text, term):
    if term.isascii() and len(term) <= 3:
        return re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])", text) is not None
    return term.lower() in text


def relevance_signals(text):
    normalized = str(text or "").lower()
    strong = [term for term in STRONG_RELEVANCE_TERMS if _has_term(normalized, term)]
    direct = [term for term in DIRECT_CREATOR_TERMS if _has_term(normalized, term)]
    context = [term for term in CREATOR_CONTEXT_TERMS if _has_term(normalized, term)]
    return {"strong": strong, "direct_creator": direct, "creator_context": context}


def is_relevant_tweet(tweet):
    signals = relevance_signals(tweet.get("text"))
    return bool(signals["strong"] or signals["direct_creator"] or len(set(signals["creator_context"])) >= 2)


def tweet_id_from_key(value):
    parsed = urlparse(str(value or ""))
    if parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return None
    match = re.fullmatch(r"/[^/]+/status/(\d+)/?", parsed.path)
    return match[1] if match else None


def is_sopilot_source(url):
    parsed = urlparse(str(url or ""))
    return (parsed.scheme == "https" and parsed.netloc == "sopilot.net"
            and parsed.path.rstrip("/") == "/zh/rank/tweets"
            and parse_qs(parsed.query).get("range") == ["6h"])


def parse_rank_page(raw, category, page):
    chunks = []
    for match in re.finditer(r'self\.__next_f\.push\((\[.*?\])\)\s*;?\s*</script>', raw, re.S):
        args = json.loads(match[1])
        if len(args) > 1 and isinstance(args[1], str):
            chunks.append(args[1])
    stream = "".join(chunks).encode("utf-8")
    records, pos = {}, 0
    while pos < len(stream):
        header = re.match(rb'([0-9a-f]+):', stream[pos:])
        if not header:
            raise ValueError("Unrecognized SoPilot public data frame")
        key = header[1].decode()
        pos += header.end()
        if stream[pos:pos+1] == b"T":
            end = stream.index(b",", pos)
            size = int(stream[pos+1:end], 16)
            value = stream[end+1:end+1+size]
            if len(value) != size:
                raise ValueError("Truncated SoPilot text frame")
            records[key] = value.decode("utf-8")
            pos = end + 1 + size
        else:
            end = stream.find(b"\n", pos)
            if end < 0:
                end = len(stream)
            try:
                records[key] = json.loads(stream[pos:end])
            except (ValueError, UnicodeDecodeError):
                pass  # Module and resource frames do not contain ranking props.
            pos = end + 1

    found = []
    def visit(value):
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
        if not isinstance(props[name], list):
            raise ValueError("Invalid SoPilot tweet list")
        for tweet in props[name]:
            if not re.fullmatch(r"\d+", str(tweet.get("id", ""))):
                raise ValueError("Invalid SoPilot tweet ID")
            text = tweet.get("text")
            if isinstance(text, str) and re.fullmatch(r"\$[0-9a-f]+", text):
                text = records.get(text[1:])
            if not isinstance(text, str):
                raise ValueError("Unresolved SoPilot tweet text")
            tweet["text"] = text[1:] if text.startswith("$$") else text
            dt.datetime.fromisoformat(tweet["tweetCreatedAt"].replace("Z", "+00:00"))
    return props


def fetch_sopilot(fetch_text):
    """Fail the entire snapshot if any selected page cannot be read."""
    pages = []
    def capture(category):
        collected, page, total = [], 1, 1
        while page <= total:
            params = {"range": "6h"}
            if category != "all":
                params["category"] = category
            if page > 1:
                params["page"] = page
            url = "https://sopilot.net/zh/rank/tweets?" + urlencode(params)
            props = parse_rank_page(fetch_text(url), category, page)
            total = max(total, props["totalPages"])
            collected.append((url, props))
            page += 1
        return collected
    with ThreadPoolExecutor(max_workers=3) as pool:
        for captured in pool.map(capture, CATEGORIES):
            pages.extend(captured)
    tweets = {}
    for url, props in pages:
        for tweet in props["risingTweets"] + props["hotTweets"]:
            key = tweet["id"]
            previous = tweets.get(key)
            if previous is None or len(tweet["text"]) > len(previous["text"]):
                tweets[key] = dict(tweet)
    discovered = len(tweets)
    tweets = {key: tweet for key, tweet in tweets.items() if is_relevant_tweet(tweet)}
    entries = []
    for key, tweet in sorted(tweets.items()):
        author = str(tweet["screenName"])
        if not re.fullmatch(r"[A-Za-z0-9_]+", author):
            raise ValueError("Invalid SoPilot author handle")
        link = f"https://x.com/{author}/status/{key}"
        published = dt.datetime.fromisoformat(tweet["tweetCreatedAt"].replace("Z", "+00:00"))
        first_line = next((line for line in tweet["text"].splitlines() if line.strip()), "推文")
        images = [url for i, url in enumerate(tweet.get("mediaUrls", []))
                  if i < len(tweet.get("mediaTypes", [])) and tweet["mediaTypes"][i] == "photo"]
        entries.append({
            "id": f"https://x.com/i/status/{key}", "link": link,
            "title": f"{tweet.get('authorName') or author}：{first_line[:180]}",
            "published_parsed": published.astimezone(dt.timezone.utc).timetuple(),
            "content": [{"type": "text/plain", "value": tweet["text"]}],
            "_sopilot_complete": True,
            "media_content": [{"url": url, "medium": "image"} for url in images],
        })
    return SimpleNamespace(entries=entries, feed={"title": "SoPilot 6小时推文榜"},
                           sopilot={"captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                                    "pages": [url for url, _ in pages], "tweet_ids": sorted(tweets),
                                    "discovered": discovered, "prefiltered": discovered - len(tweets)})


if __name__ == "__main__":
    import argparse
    from pathlib import Path
    import requests
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    def get(url):
        response = requests.get(url, timeout=45)
        response.raise_for_status()
        return response.content.decode("utf-8")
    feed = fetch_sopilot(get)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"snapshot": feed.sopilot, "entries": feed.entries}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"pages": len(feed.sopilot["pages"]), "unique_tweets": len(feed.entries), "output": str(target)}))
