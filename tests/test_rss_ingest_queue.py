import os
import sys
import threading
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

import rss_ingest
import html_watch
from rss_ingest import collect_queue_items, split_sources_and_queue


def qa_items():
    return [
        {"question": "q1", "answer": "a1"},
        {"question": "q2", "answer": "a2"},
        {"question": "q3", "answer": "a3"},
    ]


def test_collect_queue_items_skips_existing_keys():
    items = [
        {"item_key": "a", "content": "x"},
        {"item_key": "b", "content": "y"},
    ]
    existing = {"a"}
    out = collect_queue_items(items, existing)
    assert [i["item_key"] for i in out] == ["b"]


def test_split_sources_and_queue_returns_queue(monkeypatch):
    monkeypatch.setattr(rss_ingest, "update_bitable_record_fields", lambda *args, **kwargs: None)
    sources = [{"feed_url": "x", "enabled": False, "record_id": "r1"}]
    queue, source_states, stats = split_sources_and_queue(sources, existing_keys=set(), tenant_token="t")
    assert isinstance(queue, list)
    assert isinstance(source_states, dict)
    assert isinstance(stats, dict)


def test_validate_summary_result_accepts_qa_schema_without_title():
    result = rss_ingest.validate_summary_result(
        {
            "qa": [
                {"question": "问题1", "answer": "回答1"},
                {"question": "问题2", "answer": "回答2"},
                {"question": "问题3", "answer": "回答3"},
            ],
        }
    )

    assert result == {
        "qa": [
            {"question": "问题1", "answer": "回答1"},
            {"question": "问题2", "answer": "回答2"},
            {"question": "问题3", "answer": "回答3"},
        ],
    }


def test_validate_summary_result_requires_at_least_three_qa_items():
    try:
        rss_ingest.validate_summary_result(
            {
                "qa": [
                    {"question": "问题1", "answer": "回答1"},
                    {"question": "问题2", "answer": "回答2"},
                ],
            }
        )
    except ValueError as exc:
        assert "qa must contain at least 3 items" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_validate_screen_result_requires_and_keeps_ingest_title():
    result = rss_ingest.validate_screen_result(
        {
            "action": "ingest",
            "categories": ["AI工具与自动化"],
            "score": 8.0,
            "reason": "保留",
            "title_zh": "中文标题",
            "brief_summary": "事实摘要",
            "keywords": [{"name": "OpenAI", "type": "org"}],
        }
    )

    assert result["title_zh"] == "中文标题"

    try:
        rss_ingest.validate_screen_result(
            {
                "action": "ingest",
                "categories": ["AI工具与自动化"],
                "score": 8.0,
                "reason": "保留",
                "brief_summary": "事实摘要",
                "keywords": [{"name": "OpenAI", "type": "org"}],
            }
        )
    except ValueError as exc:
        assert "missing title_zh" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_build_summary_renders_qa_without_labels():
    summary = rss_ingest.build_summary(
        qa=[
            {"question": "问题1", "answer": "回答1"},
            {"question": "问题2", "answer": "回答2"},
        ]
    )

    assert summary == "问题1\n回答1\n\n问题2\n回答2"


def test_build_news_fields_renders_qa_summary():
    fields = rss_ingest.build_news_fields(
        {
            "title": "原始标题",
            "link": "https://example.com",
            "published": 100,
            "source": "src",
            "content": "body",
        },
        {
            "title_zh": "改写标题",
            "score": 8.0,
            "categories": ["AI工具与自动化"],
            "qa": [
                {"question": "问题1", "answer": "回答1"},
                {"question": "问题2", "answer": "回答2"},
                {"question": "问题3", "answer": "回答3"},
            ],
        },
        "item-1",
    )

    assert fields[rss_ingest.config.NEWS_FIELD_SUMMARY] == "问题1\n回答1\n\n问题2\n回答2\n\n问题3\n回答3"


def test_should_fetch_uses_latest_fetch_time_for_interval(monkeypatch):
    monkeypatch.setattr(rss_ingest.config, "DEFAULT_FETCH_INTERVAL_MIN", 10, raising=False)
    now_ms = 1_000_000
    source = {
        "enabled": True,
        "last_fetch_time": now_ms,
        "last_item_pub_time": now_ms - 7 * 24 * 60 * 60 * 1000,
    }

    assert rss_ingest.should_fetch(source, now_ms + 60_000) is False


def test_normalize_entry_published_ts_clamps_far_future_timestamp():
    now_ms = 1_700_000_000_000
    future_seconds = int((now_ms + 12 * 60 * 60 * 1000) / 1000)
    entry = {"published_parsed": time.gmtime(future_seconds)}

    assert rss_ingest.normalize_entry_published_ts(entry, now_ms) == now_ms // 1000


def test_normalize_entry_published_ts_clamps_near_future_timestamp():
    now_ms = 1_700_000_000_000
    future_seconds = int((now_ms + 45 * 60 * 1000) / 1000)
    entry = {"published_parsed": time.gmtime(future_seconds)}

    assert rss_ingest.normalize_entry_published_ts(entry, now_ms) == now_ms // 1000


def test_split_sources_and_queue_dedups_same_item_key_across_sources(monkeypatch):
    entry = {
        "id": "shared-key",
        "title": "same title",
        "link": "https://example.com/item",
        "summary": "same content",
    }

    class DummyFeed:
        entries = [entry]

    monkeypatch.setattr(rss_ingest, "update_bitable_record_fields", lambda *args, **kwargs: None)
    monkeypatch.setattr(rss_ingest, "fetch_feed", lambda *args, **kwargs: DummyFeed())

    sources = [
        {
            "record_id": "source-1",
            "feed_url": "https://example.com/rss-a",
            "enabled": True,
            "name": "A",
            "last_fetch_time": 0,
            "last_item_pub_time": 0,
            "consecutive_fail_count": 0,
            "item_id_strategy": "guid",
            "content_hash_algo": "md5",
            "failed_items": None,
        },
        {
            "record_id": "source-2",
            "feed_url": "https://example.com/rss-b",
            "enabled": True,
            "name": "B",
            "last_fetch_time": 0,
            "last_item_pub_time": 0,
            "consecutive_fail_count": 0,
            "item_id_strategy": "guid",
            "content_hash_algo": "md5",
            "failed_items": None,
        },
    ]

    queue, _, stats = split_sources_and_queue(sources, existing_keys=set(), tenant_token="t")

    assert [item["item_key"] for item in queue] == ["shared-key"]
    assert stats["queue_total"] == 1


def test_split_sources_and_queue_advances_cursor_for_existing_newer_item(monkeypatch):
    now_ms = 1_700_000_000_000
    old_ts = int((now_ms - 60_000) / 1000)
    new_ts = int(now_ms / 1000)
    old_entry = {
        "id": "already-written-old",
        "title": "old title",
        "link": "https://example.com/old",
        "summary": "old content",
        "published_parsed": time.gmtime(old_ts),
    }
    new_entry = {
        "id": "already-written-new",
        "title": "new title",
        "link": "https://example.com/new",
        "summary": "new content",
        "published_parsed": time.gmtime(new_ts),
    }

    class DummyFeed:
        entries = [new_entry, old_entry]

    monkeypatch.setattr(rss_ingest, "update_bitable_record_fields", lambda *args, **kwargs: None)
    monkeypatch.setattr(rss_ingest, "fetch_feed", lambda *args, **kwargs: DummyFeed())

    source = {
        "record_id": "source-1",
        "feed_url": "https://example.com/rss",
        "enabled": True,
        "name": "Example",
        "last_fetch_time": 0,
        "last_item_pub_time": (old_ts - 60) * 1000,
        "consecutive_fail_count": 0,
        "item_id_strategy": "guid",
        "content_hash_algo": "md5",
        "failed_items": None,
    }

    queue, source_states, stats = split_sources_and_queue(
        [source],
        existing_keys={"already-written-old", "already-written-new"},
        tenant_token="t",
    )

    assert queue == []
    assert stats["queue_total"] == 0
    assert source_states["source-1"]["latest_pub_ms"] == new_ts * 1000
    assert source_states["source-1"]["latest_key"] == "already-written-new"


def test_split_sources_and_queue_includes_late_arriving_item_within_lookback(monkeypatch):
    now_ms = 1_700_000_000_000
    cursor_ts = int(now_ms / 1000)
    late_ts = int((now_ms - 15 * 60_000) / 1000)
    late_entry = {
        "id": "late-arrival",
        "title": "late title",
        "link": "https://example.com/late",
        "summary": "late content",
        "published_parsed": time.gmtime(late_ts),
    }

    class DummyFeed:
        entries = [late_entry]

    monkeypatch.setattr(rss_ingest, "update_bitable_record_fields", lambda *args, **kwargs: None)
    monkeypatch.setattr(rss_ingest, "fetch_feed", lambda *args, **kwargs: DummyFeed())
    monkeypatch.setattr(
        rss_ingest,
        "extract_article_text",
        lambda url, source_name, feed_url, entry_arg, timeout, force_fetch=False: {
            "text": entry_arg.get("summary") or "",
            "method": "rss",
            "status": "ok",
            "error": "",
            "content_length": len(entry_arg.get("summary") or ""),
        },
    )
    monkeypatch.setattr(rss_ingest.config, "RSS_FETCH_LOOKBACK_MINUTES", 60, raising=False)

    source = {
        "record_id": "source-1",
        "feed_url": "https://example.com/rss",
        "enabled": True,
        "name": "Example",
        "last_fetch_time": cursor_ts * 1000,
        "last_item_pub_time": cursor_ts * 1000,
        "last_item_guid": "cursor-item",
        "consecutive_fail_count": 0,
        "item_id_strategy": "guid",
        "content_hash_algo": "md5",
        "failed_items": None,
    }

    queue, source_states, stats = split_sources_and_queue(
        [source],
        existing_keys=set(),
        tenant_token="t",
    )

    assert [item["item_key"] for item in queue] == ["late-arrival"]
    assert stats["queue_total"] == 1
    assert source_states["source-1"]["latest_pub_ms"] == cursor_ts * 1000
    assert source_states["source-1"]["latest_key"] == "cursor-item"


def test_split_sources_and_queue_uses_extractor_with_bounded_timeout(monkeypatch):
    entry = {
        "id": "item-1",
        "title": "title",
        "link": "https://huggingface.co/blog/example",
        "summary": "",
    }

    class DummyFeed:
        entries = [entry]

    captured = {}

    def fake_extract(url, source_name, feed_url, entry_arg, timeout, force_fetch=False):
        captured["url"] = url
        captured["source_name"] = source_name
        captured["feed_url"] = feed_url
        captured["timeout"] = timeout
        captured["force_fetch"] = force_fetch
        return {
            "text": "extracted body",
            "method": "source_parser:huggingface_blog",
            "status": "ok",
            "error": "",
            "content_length": 14,
        }

    monkeypatch.setattr(rss_ingest, "update_bitable_record_fields", lambda *args, **kwargs: None)
    monkeypatch.setattr(rss_ingest, "fetch_feed", lambda *args, **kwargs: DummyFeed())
    monkeypatch.setattr(rss_ingest, "extract_article_text", fake_extract)
    monkeypatch.setattr(rss_ingest.config, "HTTP_TIMEOUT", 20, raising=False)

    sources = [
        {
            "record_id": "source-1",
            "feed_url": "https://huggingface.co/blog/feed.xml",
            "enabled": True,
            "name": "Hugging Face Blog",
            "last_fetch_time": 0,
            "last_item_pub_time": 0,
            "consecutive_fail_count": 0,
            "item_id_strategy": "guid",
            "content_hash_algo": "md5",
            "failed_items": None,
        }
    ]

    queue, _, stats = split_sources_and_queue(sources, existing_keys=set(), tenant_token="t")

    assert queue[0]["article"]["content"] == "extracted body"
    assert queue[0]["article"]["extraction"]["method"] == "source_parser:huggingface_blog"
    assert captured == {
        "url": "https://huggingface.co/blog/example",
        "source_name": "Hugging Face Blog",
        "feed_url": "https://huggingface.co/blog/feed.xml",
        "timeout": 12,
        "force_fetch": False,
    }
    assert stats["queue_total"] == 1


def test_split_sources_and_queue_aihot_all_allows_x_entries_only(monkeypatch):
    entries = [
        {
            "id": "cmqowdwfa04sbslx6f185qwnr",
            "title": "x item",
            "link": "https://aihot.virxact.com/items/cmqowdwfa04sbslx6f185qwnr",
            "summary": "short x summary\n\n阅读原文：https://x.com/MiniMax_AI/status/123",
            "author": "X：MiniMax (@MiniMax_AI)",
        },
        {
            "id": "reuters-1",
            "title": "reuters item",
            "link": "https://www.reuters.com/technology/example-ai-news",
            "summary": "short reuters summary",
        },
    ]

    class DummyFeed:
        def __init__(self, feed_entries):
            self.entries = feed_entries

    extracted = []

    def fake_extract(url, source_name, feed_url, entry_arg, timeout, force_fetch=False):
        extracted.append((url, force_fetch))
        return {
            "text": f"body for {url}",
            "method": "source_parser:x_status_browser",
            "status": "ok",
            "error": "",
            "content_length": 20,
        }

    monkeypatch.setattr(rss_ingest, "update_bitable_record_fields", lambda *args, **kwargs: None)
    monkeypatch.setattr(rss_ingest, "fetch_feed", lambda *args, **kwargs: DummyFeed(entries))
    monkeypatch.setattr(rss_ingest, "extract_article_text", fake_extract)

    source = {
        "record_id": "aihot-all",
        "feed_url": "https://aihot.virxact.com/feed/all.xml",
        "enabled": True,
        "name": "AI HOT 全部",
        "last_fetch_time": 0,
        "last_item_pub_time": 0,
        "consecutive_fail_count": 0,
        "item_id_strategy": "guid",
        "content_hash_algo": "md5",
        "failed_items": None,
    }

    queue, _, stats = split_sources_and_queue([source], existing_keys=set(), tenant_token="t")

    assert [item["item_key"] for item in queue] == ["https://x.com/MiniMax_AI/status/123"]
    assert extracted == [("https://x.com/MiniMax_AI/status/123", True)]
    assert stats["aihot_allowed"] == 1
    assert stats["aihot_allowed_twitter"] == 1
    assert stats["aihot_skipped_scope"] == 1


def test_split_sources_and_queue_aihot_selected_respects_enabled_original_source(monkeypatch):
    selected_entries = [
        {
            "id": "covered",
            "title": "covered item",
            "link": "https://techcrunch.com/2026/06/04/example-ai-news",
            "summary": "covered summary",
        },
        {
            "id": "selected-only",
            "title": "selected item",
            "link": "https://example.com/selected-ai-news",
            "summary": "selected summary",
        },
    ]

    class DummyFeed:
        def __init__(self, feed_entries):
            self.entries = feed_entries

    extracted = []

    def fake_fetch_feed(url, *args, **kwargs):
        if "aihot.virxact.com" in url:
            return DummyFeed(selected_entries)
        return DummyFeed([])

    def fake_extract(url, source_name, feed_url, entry_arg, timeout, force_fetch=False):
        extracted.append((url, force_fetch))
        return {
            "text": f"body for {url}",
            "method": "source_parser:generic_article",
            "status": "ok",
            "error": "",
            "content_length": 20,
        }

    monkeypatch.setattr(rss_ingest, "update_bitable_record_fields", lambda *args, **kwargs: None)
    monkeypatch.setattr(rss_ingest, "fetch_feed", fake_fetch_feed)
    monkeypatch.setattr(rss_ingest, "extract_article_text", fake_extract)

    sources = [
        {
            "record_id": "techcrunch",
            "feed_url": "https://techcrunch.com/category/artificial-intelligence/feed/",
            "enabled": True,
            "name": "TechCrunch AI",
            "last_fetch_time": 0,
            "last_item_pub_time": 0,
            "consecutive_fail_count": 0,
            "item_id_strategy": "guid",
            "content_hash_algo": "md5",
            "failed_items": None,
        },
        {
            "record_id": "aihot-selected",
            "feed_url": "https://aihot.virxact.com/feed",
            "enabled": True,
            "name": "AI HOT 精选",
            "last_fetch_time": 0,
            "last_item_pub_time": 0,
            "consecutive_fail_count": 0,
            "item_id_strategy": "guid",
            "content_hash_algo": "md5",
            "failed_items": None,
        },
    ]

    queue, _, stats = split_sources_and_queue(sources, existing_keys=set(), tenant_token="t")

    assert [item["item_key"] for item in queue] == ["selected-only"]
    assert extracted == [("https://example.com/selected-ai-news", True)]
    assert stats["aihot_allowed"] == 1
    assert stats["aihot_allowed_selected"] == 1
    assert stats["aihot_skipped_enabled"] == 1


def test_split_sources_and_queue_accepts_html_watch_entries(monkeypatch):
    entry = {
        "id": "https://example.com/update-1",
        "guid": "https://example.com/update-1",
        "title": "DeepSeek API Update",
        "link": "https://example.com/update-1",
        "summary": "DeepSeek API Update",
    }

    def fake_fetch_html_watch(source, now_ms, timeout):
        return html_watch.HtmlWatchResult(
            status="ok",
            entries=[entry],
            watch_state={"etag": "abc"},
        )

    def fail_fetch_feed(*args, **kwargs):
        raise AssertionError("html_watch sources must not use RSS fetch_feed")

    monkeypatch.setattr(rss_ingest, "fetch_html_watch", fake_fetch_html_watch, raising=False)
    monkeypatch.setattr(rss_ingest, "fetch_feed", fail_fetch_feed)
    monkeypatch.setattr(rss_ingest, "update_bitable_record_fields", lambda *args, **kwargs: None)

    sources = [
        {
            "record_id": "source-1",
            "feed_url": "https://api-docs.deepseek.com/updates",
            "enabled": True,
            "type": "html_watch",
            "name": "DeepSeek Updates",
            "last_fetch_time": 0,
            "last_item_pub_time": 0,
            "consecutive_fail_count": 0,
            "item_id_strategy": "guid",
            "content_hash_algo": "md5",
            "failed_items": None,
            "watch_state": "",
        }
    ]

    queue, source_states, stats = split_sources_and_queue(sources, existing_keys=set(), tenant_token="t")

    assert [item["item_key"] for item in queue] == ["https://example.com/update-1"]
    assert queue[0]["article"]["source"] == "DeepSeek Updates"
    assert source_states["source-1"]["watch_state"] == {"etag": "abc"}
    assert stats["queue_total"] == 1


def test_source_update_retries_without_watch_state_when_field_rejected(monkeypatch):
    calls = []

    def fake_update(app_token, table_id, tenant_token, record_id, fields, timeout, retries):
        calls.append(fields)
        return False if rss_ingest.config.RSS_FIELD_WATCH_STATE in fields else True

    monkeypatch.setattr(rss_ingest, "update_bitable_record_fields", fake_update)

    ok = rss_ingest.update_source_record_fields(
        "tenant",
        "source-1",
        {
            rss_ingest.config.RSS_FIELD_STATUS: rss_ingest.config.STATUS_OK,
            rss_ingest.config.RSS_FIELD_WATCH_STATE: '{"etag":"abc"}',
        },
    )

    assert ok is True
    assert rss_ingest.config.RSS_FIELD_WATCH_STATE in calls[0]
    assert rss_ingest.config.RSS_FIELD_WATCH_STATE not in calls[1]


def test_split_sources_and_queue_fetches_sources_concurrently(monkeypatch):
    lock = threading.Lock()
    active = 0
    max_active = 0

    class DummyFeed:
        def __init__(self, key):
            self.entries = [
                {
                    "id": key,
                    "title": key,
                    "link": f"https://example.com/{key}",
                    "summary": "content",
                }
            ]

    def fake_fetch_feed(url, *args, **kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return DummyFeed(url.rsplit("/", 1)[-1])

    monkeypatch.setattr(rss_ingest, "update_bitable_record_fields", lambda *args, **kwargs: None)
    monkeypatch.setattr(rss_ingest, "fetch_feed", fake_fetch_feed)
    monkeypatch.setattr(rss_ingest.config, "RSS_FETCH_CONCURRENCY", 4, raising=False)

    sources = [
        {
            "record_id": f"source-{index}",
            "feed_url": f"https://example.com/rss-{index}",
            "enabled": True,
            "name": f"Source {index}",
            "last_fetch_time": 0,
            "last_item_pub_time": 0,
            "consecutive_fail_count": 0,
            "item_id_strategy": "guid",
            "content_hash_algo": "md5",
            "failed_items": None,
        }
        for index in range(4)
    ]

    queue, _, stats = split_sources_and_queue(sources, existing_keys=set(), tenant_token="t")

    assert max_active > 1
    assert [item["item_key"] for item in queue] == ["rss-0", "rss-1", "rss-2", "rss-3"]
    assert stats["queue_total"] == 4


def test_ollama_provider_uses_local_deepseek_cloud_model(monkeypatch):
    monkeypatch.setattr(rss_ingest.config, "OLLAMA_MODEL", "deepseek-v4-flash:cloud", raising=False)

    assert rss_ingest.normalize_provider_name("ollama") == "ollama"
    assert rss_ingest.provider_model_for_stage("ollama", "screen") == "deepseek-v4-flash:cloud"


def test_analyze_with_llm_falls_back_to_ark_after_ollama_failures(monkeypatch):
    calls = []
    payloads = []

    class DummyResponse:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        payloads.append(json)
        if url == "http://localhost:11434/v1/chat/completions":
            return DummyResponse(503, text="ollama unavailable")
        if url == "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions":
            return DummyResponse(
                200,
                {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action":"ingest","categories":["AI工具与自动化"],'
                                '"score":8.0,"reason":"保留","title_zh":"标题",'
                                '"brief_summary":"OpenAI 发布新模型。",'
                                '"keywords":[{"name":"OpenAI","type":"org"}],'
                                '"qa":[{"question":"q1","answer":"a1"},{"question":"q2","answer":"a2"},{"question":"q3","answer":"a3"}]}'
                            )
                        }
                    }]
                },
            )
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(rss_ingest.requests, "post", fake_post)
    monkeypatch.setattr(rss_ingest.time, "sleep", lambda *args, **kwargs: None)
    monkeypatch.setattr(rss_ingest, "ROOT_CAUSE_RECORDED", False, raising=False)
    monkeypatch.setattr(rss_ingest, "NOTIFY_TENANT_TOKEN", None, raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_PROVIDER", "ollama", raising=False)
    monkeypatch.setattr(rss_ingest.config, "SCREEN_PROVIDER", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "OLLAMA_BASE_URL", "http://localhost:11434/v1", raising=False)
    monkeypatch.setattr(rss_ingest.config, "OLLAMA_MODEL", "deepseek-v4-flash:cloud", raising=False)
    monkeypatch.setattr(rss_ingest.config, "OLLAMA_SCREEN_MODEL", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "OLLAMA_RETRIES", 2, raising=False)
    monkeypatch.setattr(rss_ingest.config, "OLLAMA_FALLBACK_MODEL", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "OLLAMA_FALLBACK_PROVIDER", "ark", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_API_KEY", "ark-key", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_MODEL", "deepseek-v4-flash", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_RETRIES", 3, raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_DISABLE_THINKING", True, raising=False)
    monkeypatch.setattr(
        rss_ingest,
        "load_local_prompt_sections",
        lambda path=None: {"keyword_blocklist": [], "screen_prompt": "screen prompt", "summarize_prompt": "summary prompt"},
        raising=False,
    )

    result = rss_ingest.analyze_with_llm(
        {"title": "t", "content": "c", "link": "https://example.com", "published": 0, "source": "src"}
    )

    ollama_calls = [url for url in calls if url == "http://localhost:11434/v1/chat/completions"]
    ark_calls = [url for url in calls if url == "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions"]

    assert result["action"] == "ingest"
    assert result["_provider_used"] == "ark"
    assert len(ollama_calls) == 2
    assert len(ark_calls) == 2
    assert payloads[0]["model"] == "deepseek-v4-flash:cloud"
    assert payloads[-1]["model"] == "deepseek-v4-flash"
    assert payloads[-1]["thinking"] == {"type": "disabled"}
    assert "response_format" not in payloads[-1]


def test_ark_retries_empty_content_parse_failure(monkeypatch):
    calls = []

    class DummyResponse:
        status_code = 200
        text = ""

        def __init__(self, content):
            self._content = content

        def json(self):
            return {"choices": [{"finish_reason": "stop", "message": {"content": self._content}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        if len(calls) == 1:
            return DummyResponse("")
        return DummyResponse('{"action":"pass","reason":"低价值","title_zh":"标题","summary":"摘要","keywords":[{"name":"OpenAI","type":"org"}]}')

    monkeypatch.setattr(rss_ingest.requests, "post", fake_post)
    monkeypatch.setattr(rss_ingest.time, "sleep", lambda *args, **kwargs: None)
    monkeypatch.setattr(rss_ingest, "notify_parse_error", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not notify")), raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_API_KEY", "ark-key", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_MODEL", "deepseek-v4-flash", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_RETRIES", 1, raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_PARSE_RETRIES", 2, raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_DISABLE_THINKING", True, raising=False)

    result = rss_ingest.analyze_with_ark_prompt(
        {"title": "t", "content": "c", "link": "https://example.com", "source": "src"},
        "screen prompt",
    )

    assert result["action"] == "pass"
    assert len(calls) == 2


def test_ark_round_robins_starting_key_and_fails_over(monkeypatch):
    authorizations = []

    class DummyResponse:
        def __init__(self, status_code=200, text=""):
            self.status_code = status_code
            self.text = text

        def json(self):
            return {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": '{"action":"pass","reason":"低价值"}'},
                }]
            }

    def fake_post(*args, headers=None, **kwargs):
        authorizations.append(headers["Authorization"])
        if len(authorizations) == 3:
            return DummyResponse(429, '{"error":{"message":"quota"}}')
        return DummyResponse()

    monkeypatch.setattr(rss_ingest, "_http_post", fake_post)
    monkeypatch.setattr(rss_ingest.config, "ARK_API_KEY", "ark-key-a", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_API_KEY_2", "ark-key-b", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_MODEL", "deepseek-v4-flash", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_RETRIES", 2, raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_PARSE_RETRIES", 1, raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_DISABLE_THINKING", True, raising=False)
    rss_ingest._PROVIDER_KEY_ROTATION_INDEX.clear()

    article = {"title": "t", "content": "c", "link": "https://example.com", "source": "src"}
    first = rss_ingest.analyze_with_ark_prompt(article, "screen prompt")
    second = rss_ingest.analyze_with_ark_prompt(article, "screen prompt")
    third = rss_ingest.analyze_with_ark_prompt(article, "screen prompt")

    assert first["action"] == "pass"
    assert second["action"] == "pass"
    assert third["action"] == "pass"
    assert authorizations == [
        "Bearer ark-key-a",
        "Bearer ark-key-b",
        "Bearer ark-key-a",
        "Bearer ark-key-b",
    ]


def test_ark_content_filter_empty_content_does_not_retry_or_notify(monkeypatch):
    calls = []

    class DummyResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"finish_reason": "content_filter", "message": {"content": ""}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        return DummyResponse()

    monkeypatch.setattr(rss_ingest.requests, "post", fake_post)
    monkeypatch.setattr(rss_ingest, "notify_parse_error", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not notify")), raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_API_KEY", "ark-key", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_MODEL", "deepseek-v4-flash", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_RETRIES", 1, raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_PARSE_RETRIES", 3, raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_DISABLE_THINKING", True, raising=False)

    result = rss_ingest.analyze_with_ark_prompt(
        {"title": "t", "content": "c", "link": "https://example.com", "source": "src"},
        "screen prompt",
    )

    assert result["categories"] == ["调用失败"]
    assert result["summary"] == "content_filter"
    assert len(calls) == 1


def test_deepseek_preserves_non_200_failure_detail(monkeypatch):
    class DummyResponse:
        status_code = 400
        text = '{"error":{"message":"invalid request"}}'

    monkeypatch.setattr(rss_ingest, "_http_post", lambda *args, **kwargs: DummyResponse())
    monkeypatch.setattr(rss_ingest.config, "DEEPSEEK_API_KEY", "deepseek-key", raising=False)
    monkeypatch.setattr(rss_ingest.config, "DEEPSEEK_BASE_URL", "https://api.deepseek.com", raising=False)
    monkeypatch.setattr(rss_ingest.config, "DEEPSEEK_MODEL", "deepseek-v4-flash", raising=False)
    monkeypatch.setattr(rss_ingest.config, "DEEPSEEK_RETRIES", 1, raising=False)

    result = rss_ingest.analyze_with_deepseek_prompt(
        {"title": "t", "content": "c", "link": "https://example.com", "source": "src"},
        "screen prompt",
    )

    assert result["categories"] == ["调用失败"]
    assert "HTTP 400" in result["summary"]
    assert "invalid request" in result["summary"]


def test_analyze_with_llm_uses_gemini_model_name_for_primary_gemini_provider(monkeypatch):
    calls = []

    class DummyResponse:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        return DummyResponse(
            200,
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": (
                                        '{"action":"ingest","categories":["AI工具与自动化"],'
                                        '"score":8.0,"reason":"保留","title_zh":"标题",'
                                        '"brief_summary":"OpenAI 发布新模型。",'
                                        '"keywords":[{"name":"OpenAI","type":"org"}],'
                                        '"qa":[{"question":"q1","answer":"a1"},{"question":"q2","answer":"a2"},{"question":"q3","answer":"a3"}]}'
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr(rss_ingest.requests, "post", fake_post)
    monkeypatch.setattr(rss_ingest.config, "LLM_PROVIDER", "gemini", raising=False)
    monkeypatch.setattr(rss_ingest.config, "SCREEN_PROVIDER", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "GEMINI_BACKEND", "developer", raising=False)
    monkeypatch.setattr(rss_ingest.config, "GEMINI_API_KEY", "gem-key", raising=False)
    monkeypatch.setattr(rss_ingest.config, "GEMINI_MODEL_NAME", "gemini-3.1-pro-preview", raising=False)
    monkeypatch.setattr(
        rss_ingest,
        "load_local_prompt_sections",
        lambda path=None: {"keyword_blocklist": [], "screen_prompt": "screen prompt", "summarize_prompt": "summary prompt"},
        raising=False,
    )

    result = rss_ingest.analyze_with_llm(
        {"title": "t", "content": "c", "link": "https://example.com", "published": 0, "source": "src"}
    )

    assert result["action"] == "ingest"
    assert result["_provider_used"] == "gemini"
    assert calls == [
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-pro-preview:generateContent",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-pro-preview:generateContent",
    ]


def test_analyze_with_llm_can_route_gemini_through_vertex(monkeypatch):
    calls = []
    headers_seen = []

    class DummyResponse:
        status_code = 200

        def json(self):
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": (
                                        '{"action":"ingest","categories":["AI工具与自动化"],'
                                        '"score":8.0,"reason":"保留","title_zh":"标题",'
                                        '"brief_summary":"OpenAI 发布新模型。",'
                                        '"keywords":[{"name":"OpenAI","type":"org"}],'
                                        '"qa":[{"question":"q1","answer":"a1"},{"question":"q2","answer":"a2"},{"question":"q3","answer":"a3"}]}'
                                    )
                                }
                            ]
                        }
                    }
                ]
            }

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        headers_seen.append(headers or {})
        return DummyResponse()

    monkeypatch.setattr(rss_ingest.requests, "post", fake_post)
    monkeypatch.setattr(rss_ingest, "google_adc_access_token", lambda: "vertex-token", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_PROVIDER", "gemini", raising=False)
    monkeypatch.setattr(rss_ingest.config, "SCREEN_PROVIDER", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "GEMINI_BACKEND", "vertex", raising=False)
    monkeypatch.setattr(rss_ingest.config, "GOOGLE_CLOUD_PROJECT", "project-93af2405-25ba-44b8-a3d", raising=False)
    monkeypatch.setattr(rss_ingest.config, "GOOGLE_CLOUD_LOCATION", "global", raising=False)
    monkeypatch.setattr(rss_ingest.config, "GOOGLE_VERTEX_MODEL", "gemini-3-flash-preview", raising=False)
    monkeypatch.setattr(rss_ingest.config, "GEMINI_API_KEY", "", raising=False)
    monkeypatch.setattr(
        rss_ingest,
        "load_local_prompt_sections",
        lambda path=None: {"keyword_blocklist": [], "screen_prompt": "screen prompt", "summarize_prompt": "summary prompt"},
        raising=False,
    )

    result = rss_ingest.analyze_with_llm(
        {"title": "t", "content": "c", "link": "https://example.com", "published": 0, "source": "src"}
    )

    assert result["action"] == "ingest"
    assert result["_provider_used"] == "gemini"
    assert calls == [
        "https://aiplatform.googleapis.com/v1/projects/project-93af2405-25ba-44b8-a3d/locations/global/publishers/google/models/gemini-3-flash-preview:generateContent",
        "https://aiplatform.googleapis.com/v1/projects/project-93af2405-25ba-44b8-a3d/locations/global/publishers/google/models/gemini-3-flash-preview:generateContent",
    ]
    assert all(headers.get("Authorization") == "Bearer vertex-token" for headers in headers_seen)


def test_gemini_payload_uses_large_output_and_minimal_thinking(monkeypatch):
    monkeypatch.setattr(rss_ingest.config, "GEMINI_MAX_OUTPUT_TOKENS", 65536, raising=False)
    monkeypatch.setattr(rss_ingest.config, "GEMINI_THINKING_LEVEL", "minimal", raising=False)

    payload = rss_ingest.build_gemini_payload("return json")

    assert payload["contents"][0]["role"] == "user"
    assert payload["generationConfig"]["responseMimeType"] == "application/json"
    assert payload["generationConfig"]["maxOutputTokens"] == 65536
    assert payload["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "minimal"


def test_analyze_article_retries_screen_when_category_invalid(monkeypatch):
    calls = []
    responses = [
        {
            "action": "ingest",
            "categories": ["AI产品"],
            "score": 8.0,
            "reason": "保留",
            "keywords": [{"name": "OpenAI", "type": "org"}],
            "title_zh": "标题",
            "brief_summary": "摘要",
        },
        {
            "action": "ingest",
            "categories": ["AI工具与自动化"],
            "score": 8.0,
            "reason": "保留",
            "keywords": [{"name": "OpenAI", "type": "org"}],
            "title_zh": "标题",
            "brief_summary": "摘要",
        },
        {
            "title_zh": "标题",
            "qa": qa_items(),
        },
    ]

    def fake_analyze(article, provider, system_prompt, model_name, **kwargs):
        calls.append(system_prompt)
        return responses.pop(0)

    monkeypatch.setattr(rss_ingest, "analyze_with_provider_prompt", fake_analyze)
    monkeypatch.setattr(rss_ingest.config, "SCREEN_VALIDATE_RETRIES", 3, raising=False)

    result = rss_ingest.analyze_article(
        {"title": "t", "content": "c", "link": "https://example.com", "published": 0, "source": "src"},
        {"keyword_blocklist": [], "screen_prompt": "screen prompt", "summarize_prompt": "summary prompt"},
        provider="deepseek",
    )

    assert result["action"] == "ingest"
    assert result["categories"] == ["AI工具与自动化"]
    assert result["_llm_meta"]["llm_request_count"] == 3
    assert calls[0] == "screen prompt"
    assert "上一轮输出未通过系统校验：invalid categories: AI产品" in calls[1]
    assert calls[2] == "summary prompt"


def test_analyze_article_retries_pass_when_summary_missing(monkeypatch):
    calls = []
    responses = [
        {
            "action": "pass",
            "reason": "命中规则2：通稿。",
            "title_zh": "某公司发布宣传稿",
            "keywords": [{"name": "某公司", "type": "org"}],
        },
        {
            "action": "pass",
            "reason": "命中规则2：通稿。",
            "title_zh": "某公司发布宣传稿",
            "summary": "某公司发布宣传稿，无实质信息。",
            "keywords": [{"name": "某公司", "type": "org"}],
        },
    ]

    def fake_analyze(article, provider, system_prompt, model_name, **kwargs):
        calls.append(system_prompt)
        return responses.pop(0)

    monkeypatch.setattr(rss_ingest, "analyze_with_provider_prompt", fake_analyze)
    monkeypatch.setattr(rss_ingest.config, "SCREEN_VALIDATE_RETRIES", 3, raising=False)

    result = rss_ingest.analyze_article(
        {"title": "t", "content": "c", "link": "https://example.com", "published": 0, "source": "src"},
        {"keyword_blocklist": [], "screen_prompt": "screen prompt", "summarize_prompt": "summary prompt"},
        provider="deepseek",
    )

    assert result["action"] == "pass"
    assert result["summary"] == "某公司发布宣传稿，无实质信息。"
    assert result["_llm_meta"]["llm_request_count"] == 2
    assert calls[0] == "screen prompt"
    assert "上一轮输出未通过系统校验：missing summary" in calls[1]


def test_analyze_article_retries_screen_when_keyword_type_invalid(monkeypatch):
    calls = []
    responses = [
        {
            "action": "ingest",
            "categories": ["AI工具与自动化"],
            "score": 8.0,
            "reason": "保留",
            "keywords": [{"name": "OpenAI", "type": "company"}],
            "title_zh": "标题",
            "brief_summary": "摘要",
        },
        {
            "action": "ingest",
            "categories": ["AI工具与自动化"],
            "score": 8.0,
            "reason": "保留",
            "keywords": [{"name": "OpenAI", "type": "org"}],
            "title_zh": "标题",
            "brief_summary": "摘要",
        },
        {
            "title_zh": "标题",
            "qa": qa_items(),
        },
    ]

    def fake_analyze(article, provider, system_prompt, model_name, **kwargs):
        calls.append(system_prompt)
        return responses.pop(0)

    monkeypatch.setattr(rss_ingest, "analyze_with_provider_prompt", fake_analyze)
    monkeypatch.setattr(rss_ingest.config, "SCREEN_VALIDATE_RETRIES", 3, raising=False)

    result = rss_ingest.analyze_article(
        {"title": "t", "content": "c", "link": "https://example.com", "published": 0, "source": "src"},
        {"keyword_blocklist": [], "screen_prompt": "screen prompt", "summarize_prompt": "summary prompt"},
        provider="deepseek",
    )

    assert result["action"] == "ingest"
    assert result["keywords"] == [{"name": "OpenAI", "type": "org"}]
    assert result["_llm_meta"]["llm_request_count"] == 3
    assert calls[0] == "screen prompt"
    assert "上一轮输出未通过系统校验：keyword type invalid: company" in calls[1]
    assert calls[2] == "summary prompt"


def test_analyze_with_llm_routes_gemini_through_local_prompt_sections(monkeypatch):
    captured = {}

    monkeypatch.setattr(rss_ingest.config, "LLM_PROVIDER", "gemini", raising=False)
    monkeypatch.setattr(
        rss_ingest,
        "load_local_prompt_sections",
        lambda path=None: {"keyword_blocklist": [], "screen_prompt": "screen prompt", "summarize_prompt": "summary prompt"},
        raising=False,
    )
    monkeypatch.setattr(
        rss_ingest,
        "analyze_article",
        lambda article, prompt_config, provider=None: captured.update(
            {"provider": provider, "prompt_config": prompt_config}
        )
        or {
            "action": "ingest",
            "categories": ["AI工具与自动化"],
            "score": 8.0,
            "title_zh": "标题",
            "qa": qa_items(),
        },
        raising=False,
    )

    result = rss_ingest.analyze_with_llm(
        {"title": "t", "content": "c", "link": "https://example.com", "published": 0, "source": "src"}
    )

    assert result["_provider_used"] == "gemini"
    assert captured["provider"] == "gemini"
    assert captured["prompt_config"]["screen_prompt"] == "screen prompt"


def test_module_no_longer_exposes_embedded_system_prompt():
    assert not hasattr(rss_ingest, "SYSTEM_PROMPT")


def test_run_llm_queue_counts_gemini_usage(monkeypatch):
    created = []
    analyses = [
        {
            "action": "ingest",
            "categories": ["AI工具与自动化"],
            "score": 8.0,
            "title_zh": "标题1",
            "qa": qa_items(),
            "_provider_used": "deepseek",
        },
        {
            "action": "ingest",
            "categories": ["AI工具与自动化"],
            "score": 8.0,
            "title_zh": "标题2",
            "qa": qa_items(),
            "_provider_used": "gemini",
        },
    ]

    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_llm",
        lambda article, prompt_config=None: analyses.pop(0),
    )
    monkeypatch.setattr(
        rss_ingest,
        "create_bitable_record_with_id",
        lambda app_token, table_id, tenant_token, fields, *args, **kwargs: created.append(fields) or (True, "rid1"),
    )
    monkeypatch.setattr(rss_ingest.config, "FEISHU_FILTERED_TABLE_ID", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_CONCURRENCY", 1)
    monkeypatch.setattr(rss_ingest.config, "PROGRESS_BAR_WIDTH", 10)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_MIN_SCORE", 6.0, raising=False)

    queue = [
        {
            "source_id": "source-1",
            "item_key": "item-1",
            "entry_ts_ms": 1,
            "article": {"title": "t1", "content": "c", "link": "https://example.com/1", "source": "src"},
        },
        {
            "source_id": "source-2",
            "item_key": "item-2",
            "entry_ts_ms": 2,
            "article": {"title": "t2", "content": "c", "link": "https://example.com/2", "source": "src"},
        },
    ]
    source_states = {
        "source-1": {"updated_failed_items": [], "now_ms": 123, "new_count": 0},
        "source-2": {"updated_failed_items": [], "now_ms": 123, "new_count": 0},
    }
    stats = {
        "llm_success": 0,
        "llm_failed": 0,
        "llm_filtered": 0,
        "feishu_create_failed": 0,
        "entries_processed": 0,
        "entries_new": 0,
    }

    existing_keys = set()

    rss_ingest.run_llm_queue(
        queue,
        source_states,
        "tenant",
        existing_keys=existing_keys,
        stats=stats,
        secondary_pending_items=[],
    )

    assert len(created) == 2
    assert stats["llm_gemini_used"] == 1
    assert all(fields[rss_ingest.config.NEWS_FIELD_SUMMARY] for fields in created)


def test_run_llm_queue_counts_prompt_filtered_items(monkeypatch):
    created = []

    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_llm",
        lambda article, prompt_config=None: {"action": "pass", "reason": "命中过滤规则"},
    )
    monkeypatch.setattr(
        rss_ingest,
        "create_bitable_record_with_id",
        lambda *args, **kwargs: created.append(True) or (True, "rid1"),
    )
    monkeypatch.setattr(rss_ingest.config, "FEISHU_FILTERED_TABLE_ID", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_CONCURRENCY", 1)
    monkeypatch.setattr(rss_ingest.config, "PROGRESS_BAR_WIDTH", 10)

    queue = [
        {
            "source_id": "source-1",
            "item_key": "item-1",
            "entry_ts_ms": 1,
            "article": {"title": "t", "link": "https://example.com", "source": "src"},
        }
    ]
    source_states = {
        "source-1": {
            "updated_failed_items": [],
            "now_ms": 123,
            "new_count": 0,
        }
    }
    stats = {
        "llm_success": 0,
        "llm_failed": 0,
        "llm_filtered": 0,
        "feishu_create_failed": 0,
        "entries_processed": 0,
        "entries_new": 0,
    }

    existing_keys = set()

    rss_ingest.run_llm_queue(
        queue,
        source_states,
        "tenant",
        existing_keys=existing_keys,
        stats=stats,
        secondary_pending_items=[],
    )

    assert created == []
    assert stats["llm_filtered"] == 1
    assert stats["llm_success"] == 0
    assert stats["entries_processed"] == 0


def test_run_llm_queue_ignores_cloudflare_vectorize_when_enabled(monkeypatch):
    created = []

    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_llm",
        lambda article, prompt_config=None: {
            "action": "ingest",
            "categories": ["AI工具与自动化"],
            "score": 8.0,
            "title_zh": "标题",
            "qa": qa_items(),
        },
    )
    monkeypatch.setattr(
        rss_ingest,
        "create_bitable_record_with_id",
        lambda *args, **kwargs: created.append(True) or (True, "rid1"),
    )
    monkeypatch.setattr(rss_ingest.config, "LLM_PROVIDER", "openai", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_MIN_SCORE", 6.0, raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_CONCURRENCY", 1)
    monkeypatch.setattr(rss_ingest.config, "PROGRESS_BAR_WIDTH", 10)

    queue = [
        {
            "source_id": "source-1",
            "item_key": "item-1",
            "entry_ts_ms": 1,
            "article": {"title": "t", "content": "c", "link": "https://example.com", "source": "src"},
        }
    ]
    source_states = {
        "source-1": {
            "updated_failed_items": [],
            "now_ms": 123,
            "new_count": 0,
        }
    }
    stats = {
        "llm_success": 0,
        "llm_failed": 0,
        "llm_filtered": 0,
        "feishu_create_failed": 0,
        "entries_processed": 0,
        "entries_new": 0,
    }
    existing_keys = set()

    rss_ingest.run_llm_queue(
        queue,
        source_states,
        "tenant",
        existing_keys=existing_keys,
        stats=stats,
        secondary_pending_items=[],
    )

    assert created == [True]
    assert not hasattr(rss_ingest, "cf_embed_text")
    assert not hasattr(rss_ingest, "vectorize_query")
    assert not hasattr(rss_ingest, "vectorize_upsert")
    assert stats["entries_new"] == 1


def test_load_local_prompt_sections_parses_keywords_and_two_prompts(tmp_path):
    prompt_file = tmp_path / "local-prompts.md"
    prompt_file.write_text(
        "\n".join(
            [
                "关键词过滤",
                "区块链",
                "空投",
                "",
                "提示词1：筛选、评分、标签",
                "screen prompt body",
                "",
                "提示词2：标题、摘要",
                "summary prompt body",
            ]
        ),
        encoding="utf-8",
    )

    parsed = rss_ingest.load_local_prompt_sections(prompt_file)

    assert parsed["keyword_blocklist"] == ["区块链", "空投"]
    assert parsed["screen_prompt"] == "screen prompt body"
    assert parsed["summarize_prompt"] == "summary prompt body"


def test_analyze_article_keyword_filter_skips_llm_calls(monkeypatch):
    called = []

    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_provider_prompt",
        lambda *args, **kwargs: called.append(True),
        raising=False,
    )

    result = rss_ingest.analyze_article(
        {
            "title": "区块链日报",
            "content": "今天聊空投策略",
            "link": "https://example.com",
            "source": "src",
        },
        {
            "keyword_blocklist": ["区块链", "空投"],
            "screen_prompt": "screen prompt",
            "summarize_prompt": "summary prompt",
        },
    )

    assert result["action"] == "pass"
    assert "区块链" in result["reason"]
    assert called == []


def test_analyze_article_can_override_screen_provider_without_changing_summary(monkeypatch):
    calls = []

    def fake_analyze(article, provider, system_prompt, model_name, **kwargs):
        calls.append((provider, system_prompt, model_name))
        if system_prompt == "screen prompt":
            return {
                "action": "ingest",
                "reason": "保留",
                "categories": ["AI工具与自动化"],
                "score": 8.0,
                "keywords": [{"name": "OpenAI", "type": "org"}],
                "title_zh": "屏幕标题",
                "brief_summary": "OpenAI 发布新模型。",
            }
        return {
            "qa": qa_items(),
        }

    monkeypatch.setattr(rss_ingest, "analyze_with_provider_prompt", fake_analyze)
    monkeypatch.setattr(rss_ingest.config, "LLM_PROVIDER", "deepseek", raising=False)
    monkeypatch.setattr(rss_ingest.config, "SCREEN_PROVIDER", "gemini", raising=False)
    monkeypatch.setattr(rss_ingest.config, "GEMINI_MODEL_NAME", "gemini-3.1-pro-preview", raising=False)
    monkeypatch.setattr(rss_ingest.config, "DEEPSEEK_MODEL", "deepseek-v4-flash", raising=False)

    result = rss_ingest.analyze_article(
        {
            "title": "AI 新闻",
            "content": "正文",
            "link": "https://example.com",
            "source": "src",
        },
        {
            "keyword_blocklist": [],
            "screen_prompt": "screen prompt",
            "summarize_prompt": "summary prompt",
        },
    )

    assert result["action"] == "ingest"
    assert result["_provider_used"] == "gemini"
    assert result["_summary_provider_used"] == "deepseek"
    assert calls == [
        ("gemini", "screen prompt", "gemini-3.1-pro-preview"),
        ("deepseek", "summary prompt", "deepseek-v4-flash"),
    ]


def test_run_llm_queue_persists_filtered_articles(monkeypatch):
    created = []

    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_llm",
        lambda article, prompt_config=None: {
            "action": "pass",
            "reason": "命中关键词过滤：区块链",
            "_llm_meta": {"keyword_filtered": True, "keyword_hit": "区块链"},
        },
        raising=False,
    )
    monkeypatch.setattr(
        rss_ingest,
        "create_bitable_record_with_id",
        lambda app_token, table_id, tenant_token, fields, *args, **kwargs: created.append(
            {"app_token": app_token, "table_id": table_id, "tenant_token": tenant_token, "fields": fields}
        )
        or (True, "rid-filter"),
    )
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_TOKEN", "app_token", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_FILTERED_TABLE_ID", "tbl_filtered", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(rss_ingest.config, "PROGRESS_BAR_WIDTH", 10, raising=False)

    queue = [
        {
            "source_id": "source-1",
            "item_key": "item-1",
            "entry_ts_ms": 1,
            "article": {
                "title": "区块链日报",
                "content": "<p>今天聊空投策略</p>",
                "link": "https://example.com",
                "source": "src",
            },
        }
    ]
    source_states = {
        "source-1": {
            "updated_failed_items": [],
            "now_ms": 123,
            "new_count": 0,
        }
    }
    stats = {
        "llm_success": 0,
        "llm_failed": 0,
        "llm_filtered": 0,
        "feishu_create_failed": 0,
        "entries_processed": 0,
        "entries_new": 0,
    }

    rss_ingest.run_llm_queue(
        queue,
        source_states,
        "tenant",
        existing_keys=set(),
        stats=stats,
        prompt_config={
            "keyword_blocklist": ["区块链"],
            "screen_prompt": "screen prompt",
            "summarize_prompt": "summary prompt",
        },
        secondary_pending_items=[],
    )

    assert stats["llm_filtered"] == 1
    assert created[0]["table_id"] == "tbl_filtered"
    assert created[0]["fields"][rss_ingest.config.FILTERED_FIELD_ITEM_KEY] == "item-1"
    assert "区块链" in created[0]["fields"][rss_ingest.config.FILTERED_FIELD_FILTER_REASON]


def test_run_llm_queue_skips_linux_do_filtered_table_and_keywords(monkeypatch):
    created = []

    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_llm",
        lambda article, prompt_config=None: {
            "action": "pass",
            "reason": "命中规则4：纯资源合集。",
            "summary": "课程资源目录。",
            "keywords": [{"name": "课程资源", "type": "topic"}],
        },
        raising=False,
    )
    monkeypatch.setattr(
        rss_ingest,
        "ensure_keyword_records",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Linux DO filtered noise should not write keywords")),
    )
    monkeypatch.setattr(
        rss_ingest,
        "create_bitable_record_with_id",
        lambda app_token, table_id, tenant_token, fields, *args, **kwargs: created.append(fields) or (True, "rid-filter"),
    )
    monkeypatch.setattr(rss_ingest.config, "FEISHU_FILTERED_TABLE_ID", "tbl_filtered", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(rss_ingest.config, "PROGRESS_BAR_WIDTH", 10, raising=False)

    queue = [
        {
            "source_id": "source-1",
            "item_key": "linux-do-item",
            "entry_ts_ms": 1,
            "article": {
                "title": "课程资源合集",
                "content": "<p>一堆课程目录</p>",
                "link": "https://linux.do/t/topic/123",
                "source": "LINUX DO",
            },
        }
    ]
    source_states = {
        "source-1": {
            "updated_failed_items": [],
            "now_ms": 123,
            "new_count": 0,
        }
    }
    stats = {
        "llm_success": 0,
        "llm_failed": 0,
        "llm_filtered": 0,
        "feishu_create_failed": 0,
        "entries_processed": 0,
        "entries_new": 0,
    }
    existing_keys = set()

    rss_ingest.run_llm_queue(
        queue,
        source_states,
        "tenant",
        existing_keys=existing_keys,
        stats=stats,
        prompt_config={
            "keyword_blocklist": [],
            "screen_prompt": "screen prompt",
            "summarize_prompt": "summary prompt",
        },
        secondary_pending_items=[],
    )

    assert created == []
    assert stats["llm_filtered"] == 1
    assert stats["filtered_skipped"] == 1
    assert "linux-do-item" in existing_keys


def test_run_llm_queue_uses_primary_provider_when_prompt_config_present_for_openai(monkeypatch):
    calls = []

    monkeypatch.setattr(
        rss_ingest,
        "analyze_article",
        lambda *args, **kwargs: calls.append("prompt") or {"action": "pass", "reason": "should not be used"},
        raising=False,
    )
    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_llm",
        lambda article, prompt_config=None: calls.append("llm")
        or {
            "action": "ingest",
            "categories": ["AI工具与自动化"],
            "score": 8.0,
            "title_zh": "标题",
            "qa": qa_items(),
            "_provider_used": "openai",
        },
        raising=False,
    )
    monkeypatch.setattr(
        rss_ingest,
        "create_bitable_record_with_id",
        lambda *args, **kwargs: (True, "rid1"),
    )
    monkeypatch.setattr(rss_ingest.config, "LLM_PROVIDER", "openai", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(rss_ingest.config, "PROGRESS_BAR_WIDTH", 10, raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_MIN_SCORE", 6.0, raising=False)
    monkeypatch.setattr(rss_ingest, "ENABLE_TEXT_DEDUP", False, raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_FILTERED_TABLE_ID", "", raising=False)

    queue = [
        {
            "source_id": "source-1",
            "item_key": "item-1",
            "entry_ts_ms": 1,
            "article": {"title": "t", "content": "c", "link": "https://example.com", "source": "src"},
        }
    ]
    source_states = {"source-1": {"updated_failed_items": [], "now_ms": 123, "new_count": 0}}
    stats = {
        "llm_success": 0,
        "llm_failed": 0,
        "llm_filtered": 0,
        "feishu_create_failed": 0,
        "entries_processed": 0,
        "entries_new": 0,
    }

    rss_ingest.run_llm_queue(
        queue,
        source_states,
        "tenant",
        existing_keys=set(),
        stats=stats,
        prompt_config={
            "keyword_blocklist": [],
            "screen_prompt": "screen prompt",
            "summarize_prompt": "summary prompt",
        },
        secondary_pending_items=[],
    )

    assert calls == ["llm"]
    assert stats["llm_success"] == 1


def test_run_llm_queue_dedups_before_summary(monkeypatch):
    analyze_include_summary = []
    filtered = []
    summary_calls = []

    class FakeDedupStore:
        def size(self):
            return 1

        def build_candidates_context(self, **kwargs):
            return (
                "C1: 旧标题\n  摘要: 旧摘要",
                {"C1": rss_ingest.DedupCandidate("old-key", "旧标题", "旧摘要", "OpenAI")},
            )

    monkeypatch.setattr(rss_ingest, "ENABLE_TEXT_DEDUP", True, raising=False)
    monkeypatch.setattr(rss_ingest, "load_dedup_store", lambda tenant_token: FakeDedupStore())
    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_llm",
        lambda article, prompt_config=None, include_summary=None: analyze_include_summary.append(
            include_summary
        )
        or {
            "action": "ingest",
            "categories": ["AI工具与自动化"],
            "score": 8.0,
            "reason": "保留",
            "title_zh": "新标题",
            "brief_summary": "新摘要",
            "keywords": [{"name": "OpenAI", "type": "org"}],
            "_provider_used": "openai",
            "_llm_meta": {"llm_request_count": 1},
        },
        raising=False,
    )
    monkeypatch.setattr(
        rss_ingest,
        "summarize_with_llm",
        lambda *args, **kwargs: summary_calls.append(True) or {"qa": qa_items()},
        raising=False,
    )
    monkeypatch.setattr(
        rss_ingest,
        "llm_dedup_check",
        lambda *args, **kwargs: {
            "matched_id": "C1",
            "matched_title": "旧标题",
            "shared_facts": ["同一版本", "同一金额"],
            "reason": "同一事件",
        },
    )
    monkeypatch.setattr(
        rss_ingest,
        "record_filtered_outcome",
        lambda *args, **kwargs: filtered.append(args[1]) or True,
    )
    monkeypatch.setattr(rss_ingest, "ensure_keyword_records", lambda *args, **kwargs: ["rec_kw"])
    monkeypatch.setattr(rss_ingest.config, "LLM_PROVIDER", "openai", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(rss_ingest.config, "PROGRESS_BAR_WIDTH", 10, raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_MIN_SCORE", 6.0, raising=False)

    stats = {
        "llm_success": 0,
        "llm_failed": 0,
        "llm_filtered": 0,
        "feishu_create_failed": 0,
        "entries_processed": 0,
        "entries_new": 0,
    }

    rss_ingest.run_llm_queue(
        [
            {
                "source_id": "source-1",
                "item_key": "item-1",
                "entry_ts_ms": 1,
                "article": {"title": "t", "content": "c", "link": "https://example.com", "source": "src"},
            }
        ],
        {"source-1": {"updated_failed_items": [], "now_ms": 123, "new_count": 0}},
        "tenant",
        existing_keys=set(),
        stats=stats,
        prompt_config={"keyword_blocklist": [], "screen_prompt": "screen prompt", "summarize_prompt": "summary prompt"},
    )

    assert analyze_include_summary == [False]
    assert summary_calls == []
    assert filtered
    assert stats["text_dedup_skipped"] == 1
    assert rss_ingest.get_llm_meta(filtered[0])["filter_method"] == "LLM文本去重"
    assert rss_ingest.get_llm_meta(filtered[0])["filter_table_reason"] == "相同新闻：旧标题\n摘要：旧摘要"


def test_run_llm_queue_does_not_count_low_score_items_as_new(monkeypatch):
    created = []

    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_llm",
        lambda article, prompt_config=None: {
            "action": "ingest",
            "categories": ["AI工具与自动化"],
            "score": 5.5,
            "title_zh": "标题",
            "qa": qa_items(),
            "_provider_used": "openai",
        },
        raising=False,
    )
    monkeypatch.setattr(
        rss_ingest,
        "create_bitable_record_with_id",
        lambda *args, **kwargs: created.append(True) or (True, "rid1"),
    )
    monkeypatch.setattr(rss_ingest.config, "LLM_PROVIDER", "openai", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(rss_ingest.config, "PROGRESS_BAR_WIDTH", 10, raising=False)
    monkeypatch.setattr(rss_ingest, "ENABLE_TEXT_DEDUP", False, raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_MIN_SCORE", 6.0, raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_FILTERED_TABLE_ID", "", raising=False)

    queue = [
        {
            "source_id": "source-1",
            "item_key": "item-1",
            "entry_ts_ms": 1,
            "article": {"title": "t", "content": "c", "link": "https://example.com", "source": "src"},
        }
    ]
    source_states = {"source-1": {"updated_failed_items": [], "now_ms": 123, "new_count": 0}}
    stats = {
        "llm_success": 0,
        "llm_failed": 0,
        "llm_filtered": 0,
        "feishu_create_failed": 0,
        "entries_processed": 0,
        "entries_new": 0,
    }

    rss_ingest.run_llm_queue(
        queue,
        source_states,
        "tenant",
        existing_keys=set(),
        stats=stats,
        secondary_pending_items=[],
    )

    assert created == []
    assert stats["llm_success"] == 1
    assert stats["entries_new"] == 0
    assert stats["entries_low_score"] == 1
    assert source_states["source-1"]["new_count"] == 0


def test_run_llm_queue_retries_news_create_without_keyword_multiselect(monkeypatch):
    created = []

    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_llm",
        lambda article, prompt_config=None: {
            "action": "ingest",
            "categories": ["AI工具与自动化"],
            "score": 8.0,
            "reason": "保留",
            "title_zh": "标题",
            "brief_summary": "摘要",
            "keywords": [{"name": "OpenAI", "type": "org"}],
            "qa": qa_items(),
            "_provider_used": "openai",
        },
        raising=False,
    )
    monkeypatch.setattr(rss_ingest, "ensure_keyword_records", lambda *args, **kwargs: ["rec_kw"])

    def fake_create(app_token, table_id, tenant_token, fields, *args, **kwargs):
        created.append(dict(fields))
        return (len(created) == 2, "rid-news" if len(created) == 2 else None)

    monkeypatch.setattr(rss_ingest, "create_bitable_record_with_id", fake_create)
    monkeypatch.setattr(rss_ingest.config, "LLM_PROVIDER", "openai", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(rss_ingest.config, "PROGRESS_BAR_WIDTH", 10, raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_MIN_SCORE", 6.0, raising=False)
    monkeypatch.setattr(rss_ingest, "ENABLE_TEXT_DEDUP", False, raising=False)

    stats = {
        "llm_success": 0,
        "llm_failed": 0,
        "llm_filtered": 0,
        "feishu_create_failed": 0,
        "entries_processed": 0,
        "entries_new": 0,
    }
    source_states = {"source-1": {"updated_failed_items": [], "now_ms": 123, "new_count": 0}}
    existing_keys = set()

    rss_ingest.run_llm_queue(
        [
            {
                "source_id": "source-1",
                "item_key": "item-1",
                "entry_ts_ms": 1000,
                "article": {"title": "t", "content": "c", "link": "https://example.com", "source": "src"},
            }
        ],
        source_states,
        "tenant",
        existing_keys=existing_keys,
        stats=stats,
        secondary_pending_items=[],
    )

    assert len(created) == 2
    assert rss_ingest.config.NEWS_FIELD_KEYWORDS in created[0]
    assert rss_ingest.config.NEWS_FIELD_KEYWORDS not in created[1]
    assert rss_ingest.config.NEWS_FIELD_KEYWORD_RECORDS in created[1]
    assert stats["feishu_create_failed"] == 0
    assert stats["entries_new"] == 1
    assert source_states["source-1"]["updated_failed_items"] == []
    assert "item-1" in existing_keys


def test_run_llm_queue_adds_news_create_failure_to_failed_items(monkeypatch):
    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_llm",
        lambda article, prompt_config=None: {
            "action": "ingest",
            "categories": ["AI工具与自动化"],
            "score": 8.0,
            "reason": "保留",
            "title_zh": "标题",
            "brief_summary": "摘要",
            "keywords": [{"name": "OpenAI", "type": "org"}],
            "qa": qa_items(),
            "_provider_used": "openai",
        },
        raising=False,
    )
    monkeypatch.setattr(rss_ingest, "ensure_keyword_records", lambda *args, **kwargs: ["rec_kw"])
    monkeypatch.setattr(rss_ingest, "create_bitable_record_with_id", lambda *args, **kwargs: (False, None))
    monkeypatch.setattr(rss_ingest.config, "LLM_PROVIDER", "openai", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(rss_ingest.config, "PROGRESS_BAR_WIDTH", 10, raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_MIN_SCORE", 6.0, raising=False)
    monkeypatch.setattr(rss_ingest, "ENABLE_TEXT_DEDUP", False, raising=False)

    stats = {
        "llm_success": 0,
        "llm_failed": 0,
        "llm_filtered": 0,
        "feishu_create_failed": 0,
        "entries_processed": 0,
        "entries_new": 0,
    }
    source_states = {"source-1": {"updated_failed_items": [], "now_ms": 123, "new_count": 0}}

    rss_ingest.run_llm_queue(
        [
            {
                "source_id": "source-1",
                "item_key": "item-1",
                "entry_ts_ms": 1000,
                "article": {"title": "t", "content": "c", "link": "https://example.com", "source": "src"},
            }
        ],
        source_states,
        "tenant",
        existing_keys=set(),
        stats=stats,
        secondary_pending_items=[],
    )

    failed_items = source_states["source-1"]["updated_failed_items"]
    assert stats["feishu_create_failed"] == 1
    assert stats["entries_new"] == 0
    assert failed_items[0]["item_key"] == "item-1"
    assert failed_items[0]["published_ms"] == 1000
    assert failed_items[0]["last_error"] == "news_create_failed"


def test_run_llm_queue_adds_filtered_create_failure_to_failed_items(monkeypatch):
    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_llm",
        lambda article, prompt_config=None: {
            "action": "pass",
            "reason": "命中过滤规则",
            "title_zh": "标题",
            "summary": "摘要",
            "keywords": [{"name": "OpenAI", "type": "org"}],
            "_provider_used": "openai",
        },
        raising=False,
    )
    monkeypatch.setattr(rss_ingest, "ensure_keyword_records", lambda *args, **kwargs: ["rec_kw"])
    monkeypatch.setattr(rss_ingest, "create_bitable_record_with_id", lambda *args, **kwargs: (False, None))
    monkeypatch.setattr(rss_ingest.config, "FEISHU_FILTERED_TABLE_ID", "tbl_filtered", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_PROVIDER", "openai", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(rss_ingest.config, "PROGRESS_BAR_WIDTH", 10, raising=False)
    monkeypatch.setattr(rss_ingest, "ENABLE_TEXT_DEDUP", False, raising=False)

    stats = {
        "llm_success": 0,
        "llm_failed": 0,
        "llm_filtered": 0,
        "feishu_create_failed": 0,
        "entries_processed": 0,
        "entries_new": 0,
    }
    source_states = {"source-1": {"updated_failed_items": [], "now_ms": 123, "new_count": 0}}
    existing_keys = set()

    rss_ingest.run_llm_queue(
        [
            {
                "source_id": "source-1",
                "item_key": "item-1",
                "entry_ts_ms": 1000,
                "article": {"title": "t", "content": "c", "link": "https://example.com", "source": "src"},
            }
        ],
        source_states,
        "tenant",
        existing_keys=existing_keys,
        stats=stats,
        secondary_pending_items=[],
    )

    failed_items = source_states["source-1"]["updated_failed_items"]
    assert stats["filtered_log_failed"] == 1
    assert failed_items[0]["item_key"] == "item-1"
    assert failed_items[0]["last_error"] == "filtered_create_failed"
    assert "item-1" not in existing_keys


def test_run_llm_queue_adds_unhandled_worker_exception_to_failed_items(monkeypatch):
    monkeypatch.setattr(
        rss_ingest,
        "_analyze_with_llm_compat",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider exploded")),
    )
    monkeypatch.setattr(rss_ingest, "ENABLE_TEXT_DEDUP", False, raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(rss_ingest.config, "PROGRESS_BAR_WIDTH", 10, raising=False)

    queue = [
        {
            "source_id": "source-1",
            "item_key": "item-crash",
            "entry_ts_ms": 1234,
            "article": {
                "title": "会触发异常的文章",
                "link": "https://example.com/crash",
                "content": "正文",
                "source": "src",
            },
        }
    ]
    source_states = {
        "source-1": {"updated_failed_items": [], "now_ms": 9999, "new_count": 0}
    }
    stats = {"llm_success": 0, "llm_failed": 0, "entries_processed": 0, "entries_new": 0}

    rss_ingest.run_llm_queue(queue, source_states, "tenant", set(), stats)

    assert stats["llm_failed"] == 1
    assert source_states["source-1"]["updated_failed_items"][0]["item_key"] == "item-crash"
    assert "worker_exception" in source_states["source-1"]["updated_failed_items"][0]["last_error"]


def test_compute_item_key_prefetch_since_uses_earliest_source_cursor(monkeypatch):
    monkeypatch.setattr(rss_ingest.config, "RSS_FETCH_LOOKBACK_MINUTES", 180, raising=False)
    now_ms = 2_000_000_000
    sources = [
        {"last_item_pub_time": 1_900_000_000, "last_fetch_time": 0},
        {"last_item_pub_time": 0, "last_fetch_time": 1_950_000_000},
    ]

    since_ms = rss_ingest.compute_item_key_prefetch_since_ms(sources, now_ms)

    assert since_ms == 1_900_000_000 - 180 * 60 * 1000


def test_prefetch_item_key_record_map_queries_complete_time_window(monkeypatch):
    calls = []

    def fake_list(*args, **kwargs):
        calls.append(kwargs)
        field_name = kwargs["filter_obj"]["conditions"][0]["field_name"]
        if field_name == "发布时间":
            return [{"record_id": "rec-pub", "fields": {"item_key": "key-pub"}}]
        return [
            {"record_id": "rec-created", "fields": {"item_key": "key-created"}},
            {"record_id": "rec-pub", "fields": {"item_key": "key-pub"}},
        ]

    monkeypatch.setattr(rss_ingest, "list_bitable_records", fake_list)
    monkeypatch.setattr(rss_ingest.config, "NEWS_ITEM_KEY_PREFETCH_MAX_PAGES", 50, raising=False)

    result = rss_ingest.prefetch_item_key_record_map(
        "table",
        "tenant",
        since_ms=123456,
        published_field="发布时间",
        created_field="创建时间",
    )

    assert result == {"key-pub": "rec-pub", "key-created": "rec-created"}
    assert [call["filter_obj"]["conditions"][0]["field_name"] for call in calls] == [
        "发布时间",
        "创建时间",
    ]
    assert all(call["max_pages"] == 50 for call in calls)
    assert all(call["allow_partial"] is False for call in calls)


def test_cap_source_cursor_for_failed_items_stops_before_earliest_failed_item():
    latest_pub_ms, latest_key = rss_ingest.cap_source_cursor_for_failed_items(
        3000,
        "newer-ok",
        [
            {"item_key": "failed-newer", "published_ms": 2500},
            {"item_key": "failed-older", "published_ms": 2000},
        ],
    )

    assert latest_pub_ms == 1999
    assert latest_key == ""


def test_parse_failed_items_accepts_feishu_rich_text_json():
    raw = [
        {
            "text": (
                '[{"item_key":"item-1","title":"Title","link":"https://example.com",'
                '"published_ms":1000,"fail_count":2,"last_error":"screen",'
                '"last_seen_ms":2000,"miss_count":0}]'
            ),
            "type": "text",
        }
    ]

    assert rss_ingest.parse_failed_items(raw) == [
        {
            "item_key": "item-1",
            "title": "Title",
            "link": "https://example.com",
            "published_ms": 1000,
            "fail_count": 2,
            "last_error": "screen",
            "last_seen_ms": 2000,
            "miss_count": 0,
        }
    ]


def test_run_llm_queue_persists_low_score_filtered_articles(monkeypatch):
    created = []

    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_llm",
        lambda article, prompt_config=None: {
            "action": "ingest",
            "categories": ["AI工具与自动化"],
            "score": 5.5,
            "reason": "信息增量不足，仅适合观察",
            "title_zh": "标题",
            "qa": qa_items(),
            "_provider_used": "openai",
        },
        raising=False,
    )
    monkeypatch.setattr(
        rss_ingest,
        "create_bitable_record_with_id",
        lambda app_token, table_id, tenant_token, fields, *args, **kwargs: created.append(
            {"app_token": app_token, "table_id": table_id, "tenant_token": tenant_token, "fields": fields}
        )
        or (True, "rid-filter"),
    )
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_TOKEN", "app_token", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_FILTERED_TABLE_ID", "tbl_filtered", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_PROVIDER", "openai", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(rss_ingest.config, "PROGRESS_BAR_WIDTH", 10, raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_MIN_SCORE", 6.0, raising=False)

    queue = [
        {
            "source_id": "source-1",
            "item_key": "item-1",
            "entry_ts_ms": 1,
            "article": {"title": "常规更新", "content": "正文", "link": "https://example.com", "source": "src"},
        }
    ]
    source_states = {"source-1": {"updated_failed_items": [], "now_ms": 123, "new_count": 0}}
    stats = {
        "llm_success": 0,
        "llm_failed": 0,
        "llm_filtered": 0,
        "feishu_create_failed": 0,
        "entries_processed": 0,
        "entries_new": 0,
    }
    existing_keys = set()

    rss_ingest.run_llm_queue(
        queue,
        source_states,
        "tenant",
        existing_keys=existing_keys,
        stats=stats,
        secondary_pending_items=[],
    )

    assert len(created) == 1
    assert created[0]["table_id"] == "tbl_filtered"
    assert created[0]["fields"][rss_ingest.config.FILTERED_FIELD_FILTER_METHOD] == "低分淘汰"
    assert "低于入库阈值" in created[0]["fields"][rss_ingest.config.FILTERED_FIELD_FILTER_REASON]
    assert "item-1" in created[0]["fields"][rss_ingest.config.FILTERED_FIELD_ITEM_KEY]
    assert stats["entries_low_score"] == 1
    assert stats["filtered_logged"] == 1
    assert "item-1" in existing_keys


def test_run_llm_queue_adds_filtered_items_to_existing_keys(monkeypatch):
    existing_keys = set()

    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_llm",
        lambda article, prompt_config=None: {
            "action": "pass",
            "reason": "命中关键词过滤：区块链",
        },
        raising=False,
    )
    monkeypatch.setattr(
        rss_ingest,
        "create_bitable_record_with_id",
        lambda *args, **kwargs: (True, "rid-filter"),
    )
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_TOKEN", "app_token", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_FILTERED_TABLE_ID", "tbl_filtered", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_PROVIDER", "deepseek", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(rss_ingest.config, "PROGRESS_BAR_WIDTH", 10, raising=False)

    queue = [
        {
            "source_id": "source-1",
            "item_key": "item-1",
            "entry_ts_ms": 1,
            "article": {"title": "区块链日报", "content": "正文", "link": "https://example.com", "source": "src"},
        }
    ]
    source_states = {"source-1": {"updated_failed_items": [], "now_ms": 123, "new_count": 0}}
    stats = {
        "llm_success": 0,
        "llm_failed": 0,
        "llm_filtered": 0,
        "feishu_create_failed": 0,
        "entries_processed": 0,
        "entries_new": 0,
    }

    rss_ingest.run_llm_queue(
        queue,
        source_states,
        "tenant",
        existing_keys=existing_keys,
        stats=stats,
        prompt_config={
            "keyword_blocklist": ["区块链"],
            "screen_prompt": "screen prompt",
            "summarize_prompt": "summary prompt",
        },
        secondary_pending_items=[],
    )

    assert "item-1" in existing_keys
    assert stats["llm_filtered"] == 1


def test_run_llm_queue_does_not_persist_vectorize_skipped_articles(monkeypatch):
    created = []

    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_llm",
        lambda article, prompt_config=None: {
            "action": "ingest",
            "categories": ["AI工具与自动化"],
            "score": 8.1,
            "reason": "具备一定参考价值",
            "title_zh": "标题",
            "qa": qa_items(),
            "_provider_used": "openai",
        },
        raising=False,
    )
    monkeypatch.setattr(
        rss_ingest,
        "create_bitable_record_with_id",
        lambda app_token, table_id, tenant_token, fields, *args, **kwargs: created.append(
            {"app_token": app_token, "table_id": table_id, "tenant_token": tenant_token, "fields": fields}
        )
        or (True, "rid-filter"),
    )
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_TOKEN", "app_token", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_FILTERED_TABLE_ID", "tbl_filtered", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_PROVIDER", "openai", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LLM_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(rss_ingest.config, "PROGRESS_BAR_WIDTH", 10, raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_MIN_SCORE", 6.0, raising=False)

    queue = [
        {
            "source_id": "source-1",
            "item_key": "item-1",
            "entry_ts_ms": 1,
            "article": {"title": "重复快讯", "content": "正文", "link": "https://example.com", "source": "src"},
        }
    ]
    source_states = {"source-1": {"updated_failed_items": [], "now_ms": 123, "new_count": 0}}
    stats = {
        "llm_success": 0,
        "llm_failed": 0,
        "llm_filtered": 0,
        "feishu_create_failed": 0,
        "entries_processed": 0,
        "entries_new": 0,
    }
    existing_keys = set()

    rss_ingest.run_llm_queue(
        queue,
        source_states,
        "tenant",
        existing_keys=existing_keys,
        stats=stats,
        secondary_pending_items=[],
    )

    assert len(created) == 1
    assert created[0]["table_id"] == rss_ingest.config.FEISHU_NEWS_TABLE_ID
    assert stats["filtered_logged"] == 0
    assert "item-1" in existing_keys


def test_prefetch_recent_item_keys_includes_filtered_table(monkeypatch):
    calls = []

    def fake_prefetch(table_id, tenant_token, app_token=None, **kwargs):
        calls.append(table_id)
        if table_id == "news_table":
            return {"news-key": "r1"}
        if table_id == "filtered_table":
            return {"filtered-key": "r2"}
        return {}

    monkeypatch.setattr(rss_ingest, "prefetch_item_key_record_map", fake_prefetch, raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_NEWS_TABLE_ID", "news_table", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_FILTERED_TABLE_ID", "filtered_table", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_TOKEN", "app_token", raising=False)

    keys = rss_ingest.prefetch_recent_item_keys("tenant")

    assert keys == {"news-key", "filtered-key"}
    assert calls == ["news_table", "filtered_table"]


def test_prefetch_recent_item_keys_with_retries_retries_whole_prefetch(monkeypatch):
    calls = []

    def flaky_prefetch(tenant_token):
        calls.append(tenant_token)
        if len(calls) == 1:
            raise RuntimeError("temporary feishu error")
        return {"existing-key"}

    monkeypatch.setattr(rss_ingest, "prefetch_recent_item_keys", flaky_prefetch, raising=False)
    monkeypatch.setattr(rss_ingest.config, "RSS_INGEST_ITEM_KEY_PREFETCH_ATTEMPTS", 2, raising=False)
    monkeypatch.setattr(rss_ingest.time, "sleep", lambda *_args, **_kwargs: None)

    keys = rss_ingest.prefetch_recent_item_keys_with_retries("tenant")

    assert keys == {"existing-key"}
    assert calls == ["tenant", "tenant"]


def test_main_aborts_when_item_key_prefetch_keeps_failing(monkeypatch):
    calls = []
    split_called = []

    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_ID", "app", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_SECRET", "secret", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_TOKEN", "app_token", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_NEWS_TABLE_ID", "news_table", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_RSS_TABLE_ID", "rss_table", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_KEYWORD_TABLE_ID", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ENABLE_SECONDARY_SYNC", False, raising=False)
    monkeypatch.setattr(rss_ingest.config, "RSS_INGEST_ITEM_KEY_PREFETCH_ATTEMPTS", 2, raising=False)
    monkeypatch.setattr(rss_ingest.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rss_ingest, "get_tenant_access_token", lambda *args, **kwargs: "tenant")
    monkeypatch.setattr(
        rss_ingest,
        "load_local_prompt_sections",
        lambda: {"keyword_name_blocklist": set(), "keyword_blocklist": set(), "path": "test"},
    )
    monkeypatch.setattr(rss_ingest, "list_bitable_records", lambda *args, **kwargs: [])

    def failing_prefetch(tenant_token, sources=None):
        calls.append(tenant_token)
        raise RuntimeError("temporary feishu error")

    def fake_split(*args, **kwargs):
        split_called.append(True)
        return [], {}, {"queue_total": 0, "sources_processed": 0, "sources_skipped": 0, "entries_fetched": 0}

    monkeypatch.setattr(rss_ingest, "prefetch_recent_item_keys", failing_prefetch, raising=False)
    monkeypatch.setattr(rss_ingest, "split_sources_and_queue", fake_split, raising=False)

    rc = rss_ingest.main()

    assert rc == 1
    assert calls == ["tenant", "tenant"]
    assert split_called == []


def test_main_returns_nonzero_when_required_config_missing(monkeypatch):
    notified = []

    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_ID", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_SECRET", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_TOKEN", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_NEWS_TABLE_ID", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_RSS_TABLE_ID", "", raising=False)
    monkeypatch.setattr(rss_ingest, "notify_config_missing", lambda detail: notified.append(detail))

    rc = rss_ingest.main()

    assert rc == 1
    assert notified


def test_main_returns_nonzero_when_feishu_auth_fails(monkeypatch):
    notified = []

    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_ID", "app", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_SECRET", "secret", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_TOKEN", "app_token", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_NEWS_TABLE_ID", "news_table", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_RSS_TABLE_ID", "rss_table", raising=False)
    monkeypatch.setattr(rss_ingest, "get_tenant_access_token", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("auth failed")))
    monkeypatch.setattr(rss_ingest, "notify_auth_failure", lambda service, detail: notified.append((service, detail)))

    rc = rss_ingest.main()

    assert rc == 1
    assert notified


def test_main_returns_nonzero_when_prompt_load_fails(monkeypatch):
    notified = []

    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_ID", "app", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_SECRET", "secret", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_TOKEN", "app_token", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_NEWS_TABLE_ID", "news_table", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_RSS_TABLE_ID", "rss_table", raising=False)
    monkeypatch.setattr(rss_ingest, "get_tenant_access_token", lambda *args, **kwargs: "tenant")
    monkeypatch.setattr(rss_ingest, "load_local_prompt_sections", lambda: (_ for _ in ()).throw(RuntimeError("prompt failed")))
    monkeypatch.setattr(rss_ingest, "notify_config_missing", lambda detail: notified.append(detail))

    rc = rss_ingest.main()

    assert rc == 1
    assert notified


def test_main_returns_nonzero_after_persisting_processing_failures(monkeypatch):
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_ID", "app", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_SECRET", "secret", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_TOKEN", "app-token", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_NEWS_TABLE_ID", "news", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_RSS_TABLE_ID", "rss", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_KEYWORD_TABLE_ID", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ENABLE_SECONDARY_SYNC", False, raising=False)
    monkeypatch.setattr(rss_ingest, "get_tenant_access_token", lambda *args: "tenant")
    monkeypatch.setattr(
        rss_ingest,
        "load_local_prompt_sections",
        lambda: {"keyword_name_blocklist": set(), "keyword_blocklist": set(), "path": "test"},
    )
    monkeypatch.setattr(
        rss_ingest,
        "list_bitable_records",
        lambda *args, **kwargs: [
            {"record_id": "source-1", "fields": {"enabled": True, "feed_url": "https://example.com/rss"}}
        ],
    )
    monkeypatch.setattr(rss_ingest, "prefetch_recent_item_keys_with_retries", lambda *args: set())
    monkeypatch.setattr(
        rss_ingest,
        "split_sources_and_queue",
        lambda *args, **kwargs: (
            [],
            {},
            {
                "queue_total": 0,
                "sources_processed": 0,
                "sources_skipped": 1,
                "sources_failed": 1,
                "entries_fetched": 0,
            },
        ),
    )

    assert rss_ingest.main() == 1


def test_main_tolerates_small_partial_source_failure(monkeypatch):
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_ID", "app", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_SECRET", "secret", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_TOKEN", "app-token", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_NEWS_TABLE_ID", "news", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_RSS_TABLE_ID", "rss", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_KEYWORD_TABLE_ID", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ENABLE_SECONDARY_SYNC", False, raising=False)
    monkeypatch.setattr(rss_ingest, "get_tenant_access_token", lambda *args: "tenant")
    monkeypatch.setattr(
        rss_ingest,
        "load_local_prompt_sections",
        lambda: {"keyword_name_blocklist": set(), "keyword_blocklist": set(), "path": "test"},
    )
    monkeypatch.setattr(
        rss_ingest,
        "list_bitable_records",
        lambda *args, **kwargs: [
            {"record_id": "source-1", "fields": {"enabled": True, "feed_url": "https://example.com/rss"}}
        ],
    )
    monkeypatch.setattr(rss_ingest, "prefetch_recent_item_keys_with_retries", lambda *args: set())
    monkeypatch.setattr(
        rss_ingest,
        "split_sources_and_queue",
        lambda *args, **kwargs: (
            [],
            {},
            {
                "queue_total": 0,
                "sources_processed": 99,
                "sources_skipped": 1,
                "sources_failed": 1,
                "entries_fetched": 0,
            },
        ),
    )

    assert rss_ingest.main() == 0


def test_main_tolerates_single_llm_item_deferred_for_retry(monkeypatch):
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_ID", "app", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_SECRET", "secret", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_TOKEN", "app-token", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_NEWS_TABLE_ID", "news", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_RSS_TABLE_ID", "rss", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_KEYWORD_TABLE_ID", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ENABLE_SECONDARY_SYNC", False, raising=False)
    monkeypatch.setattr(rss_ingest, "get_tenant_access_token", lambda *args: "tenant")
    monkeypatch.setattr(
        rss_ingest,
        "load_local_prompt_sections",
        lambda: {"keyword_name_blocklist": set(), "keyword_blocklist": set(), "path": "test"},
    )
    monkeypatch.setattr(
        rss_ingest,
        "list_bitable_records",
        lambda *args, **kwargs: [
            {"record_id": "source-1", "fields": {"enabled": True, "feed_url": "https://example.com/rss"}}
        ],
    )
    monkeypatch.setattr(rss_ingest, "prefetch_recent_item_keys_with_retries", lambda *args: set())
    monkeypatch.setattr(
        rss_ingest,
        "split_sources_and_queue",
        lambda *args, **kwargs: (
            [],
            {},
            {
                "queue_total": 20,
                "sources_processed": 116,
                "sources_skipped": 3,
                "sources_failed": 2,
                "entries_fetched": 5972,
            },
        ),
    )

    def defer_one_item(queue, source_states, tenant_token, existing_keys, stats, **kwargs):
        stats["llm_failed"] = 1

    monkeypatch.setattr(rss_ingest, "run_llm_queue", defer_one_item)

    assert rss_ingest.main() == 0


def test_main_returns_nonzero_when_every_queued_llm_item_fails(monkeypatch):
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_ID", "app", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_SECRET", "secret", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_TOKEN", "app-token", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_NEWS_TABLE_ID", "news", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_RSS_TABLE_ID", "rss", raising=False)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_KEYWORD_TABLE_ID", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ENABLE_SECONDARY_SYNC", False, raising=False)
    monkeypatch.setattr(rss_ingest, "get_tenant_access_token", lambda *args: "tenant")
    monkeypatch.setattr(
        rss_ingest,
        "load_local_prompt_sections",
        lambda: {"keyword_name_blocklist": set(), "keyword_blocklist": set(), "path": "test"},
    )
    monkeypatch.setattr(
        rss_ingest,
        "list_bitable_records",
        lambda *args, **kwargs: [
            {"record_id": "source-1", "fields": {"enabled": True, "feed_url": "https://example.com/rss"}}
        ],
    )
    monkeypatch.setattr(rss_ingest, "prefetch_recent_item_keys_with_retries", lambda *args: set())
    monkeypatch.setattr(
        rss_ingest,
        "split_sources_and_queue",
        lambda *args, **kwargs: (
            [],
            {},
            {
                "queue_total": 20,
                "sources_processed": 116,
                "sources_skipped": 3,
                "sources_failed": 2,
                "entries_fetched": 5972,
            },
        ),
    )

    def fail_every_item(queue, source_states, tenant_token, existing_keys, stats, **kwargs):
        stats["llm_failed"] = 20

    monkeypatch.setattr(rss_ingest, "run_llm_queue", fail_every_item)

    assert rss_ingest.main() == 1


def test_load_local_prompt_sections_resolves_relative_paths_from_base_dir(tmp_path, monkeypatch):
    keyword_file = tmp_path / "docs" / "local-keyword-blocklist.txt"
    screen_file = tmp_path / "docs" / "local-screen-prompt.md"
    summarize_file = tmp_path / "docs" / "local-summarize-prompt.md"
    addendum_file = tmp_path / "docs" / "local-screen-keywords-addendum.md"
    keyword_file.parent.mkdir(parents=True)
    keyword_file.write_text("# comment\n区块链\n- 空投\n", encoding="utf-8")
    screen_file.write_text("screen prompt body", encoding="utf-8")
    summarize_file.write_text("summary prompt body", encoding="utf-8")
    addendum_file.write_text("keywords addendum body", encoding="utf-8")

    monkeypatch.setattr(rss_ingest.config, "BASE_DIR", tmp_path, raising=False)
    monkeypatch.setattr(rss_ingest.config, "LOCAL_KEYWORD_BLOCKLIST_PATH", "docs/local-keyword-blocklist.txt", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LOCAL_SCREEN_PROMPT_PATH", "docs/local-screen-prompt.md", raising=False)
    monkeypatch.setattr(rss_ingest.config, "LOCAL_SUMMARIZE_PROMPT_PATH", "docs/local-summarize-prompt.md", raising=False)
    monkeypatch.setattr(
        rss_ingest.config,
        "LOCAL_SCREEN_KEYWORDS_ADDENDUM_PATH",
        "docs/local-screen-keywords-addendum.md",
        raising=False,
    )

    current = os.getcwd()
    os.chdir(str(tmp_path.parent))
    try:
        parsed = rss_ingest.load_local_prompt_sections()
    finally:
        os.chdir(current)

    assert parsed["keyword_path"] == str(keyword_file)
    assert parsed["screen_path"] == str(screen_file)
    assert parsed["summarize_path"] == str(summarize_file)
    assert parsed["keyword_blocklist"] == ["区块链", "空投"]
    assert parsed["screen_prompt"] == "screen prompt body\n\nkeywords addendum body"
    assert parsed["summarize_prompt"] == "summary prompt body"


def test_try_mark_root_cause_recorded_only_allows_first_claim(monkeypatch):
    monkeypatch.setattr(rss_ingest, "ROOT_CAUSE_RECORDED", False, raising=False)

    assert rss_ingest.try_mark_root_cause_recorded() is True
    assert rss_ingest.try_mark_root_cause_recorded() is False
