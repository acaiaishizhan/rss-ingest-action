"""Read the public six-hour rankings, preserving the server's tweet text."""
import datetime as dt
import html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse

SOURCE_URL = "https://sopilot.net/zh/rank/tweets?range=6h"
CATEGORIES = ("all", "AI", "Creator")


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
            "id": link, "link": link,
            "title": f"{tweet.get('authorName') or author}：{first_line[:180]}",
            "published_parsed": published.astimezone(dt.timezone.utc).timetuple(),
            "content": [{"type": "text/html", "value": html.escape(tweet["text"]).replace("\n", "<br>\n")}],
            "media_content": [{"url": url, "medium": "image"} for url in images],
        })
    return SimpleNamespace(entries=entries, feed={"title": "SoPilot 6小时推文榜"},
                           sopilot={"captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                                    "pages": [url for url, _ in pages], "tweet_ids": sorted(tweets)})


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
