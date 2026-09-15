import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")
import rss_ingest
import sopilot
from article_extractor import extract_article_text


def page_html(category="all", page=1, total=2, ids=("10",), long_text=False):
    text = "正文含链接 https://example.org/code 和中文🙂\n" * (20 if long_text else 1)
    records = ""
    tweets = []
    for key in ids:
        if long_text:
            records += f"a{key}:T{len(text.encode('utf-8')):x}," + text
        tweets.append({"id": key, "screenName": "author", "authorName": "作者",
                       "text": f"$a{key}" if long_text else text,
                       "tweetCreatedAt": "2026-09-15T00:00:00Z"})
    props = {"range": "6h", "category": category, "page": page, "totalPages": total,
             "risingTweets": tweets, "hotTweets": tweets}
    records += "1:" + json.dumps(["$", "$L2", None, props], ensure_ascii=False) + "\n"
    return "<script>self.__next_f.push(" + json.dumps([1, records], ensure_ascii=False) + ")</script>"


def test_unicode_length_frames_and_links_survive():
    props = sopilot.parse_rank_page(page_html(long_text=True), "all", 1)
    assert "https://example.org/code" in props["risingTweets"][0]["text"]
    assert "🙂" in props["risingTweets"][0]["text"]
    assert len(props["risingTweets"][0]["text"]) > 500


def test_original_line_breaks_code_and_literal_entities_reach_processing():
    body = '步骤一\n```python\nif a < b:\n    print("&lt;")\n```\nhttps://example.org/code'
    entry = {"content": [{"type": "text/plain", "value": body}], "_sopilot_complete": True}
    result = extract_article_text("https://x.com/a/status/1", "SoPilot", sopilot.SOURCE_URL, entry)
    assert result["text"] == body
    assert result["method"] == "source_parser:sopilot"
    fields = rss_ingest.build_article_base_fields({"content": result["text"], "extraction": result}, "original-id")
    assert fields["full_content"] == body


def test_all_categories_all_pages_and_cross_rank_dedupe():
    from urllib.parse import parse_qs, urlparse
    seen = []
    def get(url):
        q = parse_qs(urlparse(url).query)
        category, page = q.get("category", ["all"])[0], int(q.get("page", [1])[0])
        seen.append((category, page))
        key = str(100 * sopilot.CATEGORIES.index(category) + page)
        return page_html(category, page, ids=("999", key))
    feed = sopilot.fetch_sopilot(get)
    assert len(seen) == 6
    assert len(feed.entries) == 7
    assert all(entry["id"].startswith("https://x.com/i/status/") for entry in feed.entries)
    assert all(sopilot.tweet_id_from_key(entry["id"]) == sopilot.tweet_id_from_key(entry["link"]) for entry in feed.entries)
    assert len(set(feed.sopilot["tweet_ids"])) == 7


def test_wrong_page_missing_payload_or_page_failure_is_not_empty():
    with pytest.raises(ValueError):
        sopilot.parse_rank_page(page_html(page=1), "all", 2)
    with pytest.raises(ValueError):
        sopilot.parse_rank_page("<html>login</html>", "all", 1)
    def get(url):
        if "page=2" in url:
            raise ConnectionError("offline")
        category = "AI" if "category=AI" in url else "Creator" if "category=Creator" in url else "all"
        return page_html(category)
    with pytest.raises(ConnectionError):
        sopilot.fetch_sopilot(get)


def test_old_newly_ranked_posts_over_200_reach_existing_queue(monkeypatch):
    now = int(time.time())
    old = now - 5 * 3600
    entries = [{"id": f"id-{i}", "link": f"https://x.com/a/status/{i}", "title": "test",
                "published_parsed": dt.datetime.fromtimestamp(old, dt.timezone.utc).timetuple(),
                "content": [{"value": "完整正文"}]} for i in range(251)]
    source = {"feed_url": sopilot.SOURCE_URL, "enabled": True, "record_id": "source",
              "sopilot_batch": "test", "last_item_pub_time": now * 1000}
    monkeypatch.setattr(rss_ingest, "fetch_feed", lambda *a, **kw: SimpleNamespace(entries=entries))
    monkeypatch.setattr(rss_ingest, "extract_article_text", lambda *a, **kw: {"text": "完整正文"})
    queue, _, stats = rss_ingest.split_sources_and_queue([source], {"id-0"}, "unused")
    assert len(queue) == 250
    assert stats["entries_fetched"] == 251
    assert all(item["entry_ts"] == old for item in queue)
    assert not rss_ingest.should_fetch({**source, "sopilot_batch": ""}, now * 1000)
    assert rss_ingest.compute_item_key_prefetch_since_ms([source], now * 1000) <= old * 1000


def test_original_tweet_identity_survives_handle_and_domain_changes(monkeypatch):
    now = int(time.time())
    entry = {"id": "https://x.com/i/status/123", "link": "https://x.com/new_name/status/123",
             "title": "already seen", "published_parsed": time.gmtime(now), "content": [{"value": "正文"}]}
    monkeypatch.setattr(rss_ingest, "fetch_feed", lambda *a, **kw: SimpleNamespace(entries=[entry]))
    monkeypatch.setattr(rss_ingest, "extract_article_text", lambda *a, **kw: {"text": "正文"})
    source = {"feed_url": sopilot.SOURCE_URL, "enabled": True, "record_id": "source", "sopilot_batch": "test"}
    queue, _, _ = rss_ingest.split_sources_and_queue([source], {"https://twitter.com/old_name/status/123"}, "unused")
    assert not queue
    assert sopilot.tweet_id_from_key("https://evilx.com/name/status/123") is None


def test_completion_receipt_never_calls_partial_processing_complete(monkeypatch, tmp_path):
    batch = "00000000-0000-4000-8000-000000000001"
    monkeypatch.setenv("SOPILOT_BATCH_ID", batch)
    monkeypatch.setattr(rss_ingest.config, "BASE_DIR", tmp_path)
    def run(receipt):
        receipt["stats"] = {"sources_processed": 1, "llm_failed": 1}
        receipt["news_records"] = []
        return 0
    monkeypatch.setattr(rss_ingest, "_main", run)
    assert rss_ingest.main() == 1
    saved = json.loads((tmp_path / "out/sopilot" / f"{batch}.json").read_text())
    assert saved["complete"] is False
    assert saved["batch_id"] == batch
