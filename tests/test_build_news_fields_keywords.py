import os
import sys
import threading
import json

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

import config  # noqa: E402
import rss_ingest  # noqa: E402
from rss_ingest import build_filtered_fields, build_news_fields  # noqa: E402


@pytest.fixture(autouse=True)
def _disable_keyword_snapshot_index_by_default(monkeypatch):
    monkeypatch.setattr(rss_ingest.config, "ENABLE_KEYWORD_SNAPSHOT_INDEX", False, raising=False)


def _article():
    return {
        "title": "Some Title",
        "link": "https://example.com/x",
        "content": "<p>Hello</p>",
        "source": "Example",
        "published_ts": 1_700_000_000.0,
    }


def _ingest_analysis(**overrides):
    data = {
        "action": "ingest",
        "reason": "保留",
        "categories": ["AI前沿资讯"],
        "score": 8.0,
        "title_zh": "中文标题",
        "qa": [
            {"q": "Q1", "a": "A1"},
            {"q": "Q2", "a": "A2"},
            {"q": "Q3", "a": "A3"},
        ],
        "keywords": [
            {"name": "OpenAI", "type": "org"},
            {"name": "ChatGPT", "type": "product"},
            {"name": "成人模式", "type": "topic"},
        ],
    }
    data.update(overrides)
    return data


def _filtered_analysis(**overrides):
    data = {
        "action": "pass",
        "reason": "命中规则2：通稿。",
        "keywords": [
            {"name": "某公司", "type": "org"},
        ],
        "_llm_meta": {"filter_method": "初筛过滤", "filter_reason": "命中规则2"},
    }
    data.update(overrides)
    return data


def test_build_news_fields_writes_keywords():
    fields = build_news_fields(_article(), _ingest_analysis(), "key-1")
    assert fields[config.NEWS_FIELD_KEYWORDS] == "OpenAI / ChatGPT / 成人模式"


def test_collect_article_image_urls_keeps_aipoju_images_only_for_aipoju_sources():
    entry = {
        "content": [
            {
                "value": '<p>正文</p><p><img src="https://breakout-1301344553.cos.ap-beijing.myqcloud.com/images/a.png"></p>'
            }
        ]
    }
    article = {
        "title": "[AI破局] 示例",
        "link": "https://aipoju.com/topic-details/123",
        "source": "AI赚钱频道 - 私有全文 RSS",
        "content": "正文",
    }

    urls = rss_ingest.collect_article_image_urls(article, entry=entry)

    assert urls == ["https://breakout-1301344553.cos.ap-beijing.myqcloud.com/images/a.png"]

    non_target = dict(article, title="普通源", link="https://example.com/123", source="Example")
    assert rss_ingest.collect_article_image_urls(non_target, entry=entry) == []


def test_collect_article_image_urls_fetches_x_media(monkeypatch):
    class DummyResponse:
        status_code = 200

        def json(self):
            return {
                "code": 200,
                "tweet": {
                    "media": {
                        "photos": [
                            {"url": "https://pbs.twimg.com/media/abc.jpg?name=orig"},
                            {"url": "https://pbs.twimg.com/media/abc.jpg?name=orig"},
                        ],
                        "videos": [
                            {"thumbnail_url": "https://pbs.twimg.com/ext_tw_video_thumb/vid.jpg"}
                        ],
                    }
                },
            }

    requested = []

    def fake_get(url, **kwargs):
        requested.append(url)
        return DummyResponse()

    monkeypatch.setattr(rss_ingest.requests, "get", fake_get)

    urls = rss_ingest.collect_article_image_urls(
        {
            "title": "tweet",
            "link": "https://x.com/alice/status/123456",
            "source": "Grok搜索-小道消息",
            "content": "正文",
        }
    )

    assert requested == ["https://api.fxtwitter.com/alice/status/123456"]
    assert urls == [
        "https://pbs.twimg.com/media/abc.jpg?name=orig",
        "https://pbs.twimg.com/ext_tw_video_thumb/vid.jpg",
    ]


def test_collect_article_image_urls_keeps_reddit_enclosures():
    entry = {
        "enclosures": [
            {"href": "https://i.redd.it/direct.jpg", "type": "image/jpeg"},
            {"url": "https://preview.redd.it/a.webp?width=800", "type": "image/webp"},
        ],
        "summary": '<img src="https://preview.redd.it/in-summary.png?width=640">',
    }
    article = {
        "title": "[Reddit] Codex workflow",
        "link": "https://www.reddit.com/r/codex/comments/1abcxyz/title/",
        "source": "Grok搜索 - Reddit Codex玩法",
        "content": "正文",
    }

    urls = rss_ingest.collect_article_image_urls(article, entry=entry)

    assert urls == [
        "https://i.redd.it/direct.jpg",
        "https://preview.redd.it/a.webp?width=800",
        "https://preview.redd.it/in-summary.png?width=640",
    ]


def test_build_news_fields_writes_image_attachment_tokens():
    fields = build_news_fields(
        _article(),
        _ingest_analysis(),
        "key-img",
        image_file_tokens=["tok_a", "tok_b", "tok_a"],
    )

    assert fields[config.NEWS_FIELD_IMAGES] == [{"file_token": "tok_a"}, {"file_token": "tok_b"}]


def test_build_news_fields_truncates_oversized_full_content():
    article = _article()
    article["content"] = "x" * (rss_ingest.FEISHU_TEXT_CELL_SAFE_LIMIT + 1000)

    fields = build_news_fields(article, _ingest_analysis(), "key-long")

    full_content = fields[config.NEWS_FIELD_FULL_CONTENT]
    assert len(full_content) <= rss_ingest.FEISHU_TEXT_CELL_SAFE_LIMIT
    assert "[truncated " in full_content
    assert "Feishu cell limit" in full_content


def test_build_news_fields_writes_keyword_record_links():
    fields = build_news_fields(_article(), _ingest_analysis(), "key-1", keyword_record_ids=["rec_a", "rec_b", "rec_a"])
    assert fields[config.NEWS_FIELD_KEYWORD_RECORDS] == ["rec_a", "rec_b"]


def test_build_news_fields_keywords_empty_when_missing():
    analysis = _ingest_analysis()
    analysis.pop("keywords")
    fields = build_news_fields(_article(), analysis, "key-2")
    assert fields[config.NEWS_FIELD_KEYWORDS] == ""


def test_build_news_fields_skips_invalid_items():
    analysis = _ingest_analysis(
        keywords=[
            {"name": "", "type": "org"},
            {"name": "  ", "type": "org"},
            {"name": "OpenAI", "type": "org"},
            {"type": "org"},
            "not-a-dict",
        ]
    )
    fields = build_news_fields(_article(), analysis, "key-3")
    assert fields[config.NEWS_FIELD_KEYWORDS] == "OpenAI"


def test_build_news_fields_filters_metric_fact_keywords():
    analysis = _ingest_analysis(
        keywords=[
            {"name": "Anthropic", "type": "org"},
            {"name": "估值逼近万亿美元", "type": "topic"},
            {"name": "Q1营收增长20%", "type": "topic"},
        ]
    )
    fields = build_news_fields(_article(), analysis, "key-metric")
    assert fields[config.NEWS_FIELD_KEYWORDS] == "Anthropic"


def test_build_news_fields_writes_screen_summary_to_news_summary_field():
    analysis = _ingest_analysis(summary="OpenAI 发布 GPT-5，上下文窗口 200 万 token。")
    fields = build_news_fields(_article(), analysis, "key-bs-1")
    assert fields[config.NEWS_FIELD_BRIEF_SUMMARY] == "OpenAI 发布 GPT-5，上下文窗口 200 万 token。"


def test_build_news_fields_writes_score_field():
    fields = build_news_fields(_article(), _ingest_analysis(score=8.0), "key-score")
    assert fields[config.NEWS_FIELD_SCORE] == 8.0


def test_build_news_fields_omits_score_and_categories_when_screen_is_denoise_only():
    analysis = _ingest_analysis()
    analysis.pop("score")
    analysis.pop("categories")
    fields = build_news_fields(_article(), analysis, "key-denoise")
    assert config.NEWS_FIELD_SCORE not in fields
    assert config.NEWS_FIELD_CATEGORIES not in fields


def test_build_news_fields_omits_screen_summary_when_empty():
    fields = build_news_fields(_article(), _ingest_analysis(), "key-bs-2")
    assert config.NEWS_FIELD_BRIEF_SUMMARY not in fields


def test_validate_screen_preserves_summary():
    from rss_ingest import validate_screen_result
    result = validate_screen_result({
        "action": "ingest",
        "score": 7.5,
        "categories": ["AI前沿资讯"],
        "reason": "test",
        "keywords": [{"name": "OpenAI", "type": "org"}],
        "title_zh": "OpenAI 发布新模型",
        "summary": "OpenAI 发布新模型。",
    })
    assert result["summary"] == "OpenAI 发布新模型。"
    assert result["brief_summary"] == "OpenAI 发布新模型。"


def test_validate_screen_rejects_missing_summary():
    from rss_ingest import validate_screen_result
    with pytest.raises(ValueError, match="missing summary"):
        validate_screen_result({
            "action": "ingest",
            "score": 7.5,
            "categories": ["AI前沿资讯"],
            "reason": "test",
            "keywords": [{"name": "OpenAI", "type": "org"}],
            "title_zh": "OpenAI 发布新模型",
        })


def test_build_filtered_fields_writes_keywords():
    fields = build_filtered_fields(_article(), _filtered_analysis(), "key-4")
    assert fields[config.FILTERED_FIELD_KEYWORDS] == "某公司"


def test_build_filtered_fields_writes_keyword_record_links():
    fields = build_filtered_fields(_article(), _filtered_analysis(), "key-4", keyword_record_ids=["rec_x"])
    assert fields[config.FILTERED_FIELD_KEYWORD_RECORDS] == ["rec_x"]


def test_build_filtered_fields_keywords_empty_when_failed_analysis():
    # 模拟 build_failed_analysis 产物：reason 存在，但 keywords 字段缺失
    failed_analysis = {
        "action": "pass",
        "reason": "screen failed: missing keywords",
        "_llm_meta": {"filter_method": "LLM失败兜底", "filter_reason": "screen failed"},
    }
    fields = build_filtered_fields(_article(), failed_analysis, "key-5")
    assert fields[config.FILTERED_FIELD_KEYWORDS] == ""


def test_ensure_keyword_records_reuses_existing_alias(monkeypatch):
    monkeypatch.setattr(rss_ingest.config, "FEISHU_KEYWORD_TABLE_ID", "tbl_keyword", raising=False)
    monkeypatch.setattr(
        rss_ingest,
        "create_bitable_record_with_id",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not create")),
    )
    index = {
        "codex": rss_ingest.KeywordRecord(record_id="rec_codex", canonical_name="Codex", type="product"),
    }

    record_ids = rss_ingest.ensure_keyword_records(
        [{"name": "CodeX", "type": "product"}],
        "tenant-token",
        index,
        threading.Lock(),
    )

    assert record_ids == ["rec_codex"]


def test_ensure_keyword_records_expands_parent_and_owner_links(monkeypatch):
    monkeypatch.setattr(rss_ingest.config, "FEISHU_KEYWORD_TABLE_ID", "tbl_keyword", raising=False)
    monkeypatch.setattr(
        rss_ingest,
        "create_bitable_record_with_id",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not create")),
    )
    index = {
        "gpt-5": rss_ingest.KeywordRecord(
            record_id="rec_gpt5",
            canonical_name="GPT-5",
            type="model",
            parent_ids=["rec_gpt"],
            owner_ids=["rec_openai"],
        ),
        "gpt": rss_ingest.KeywordRecord(record_id="rec_gpt", canonical_name="GPT", type="model"),
        "openai": rss_ingest.KeywordRecord(record_id="rec_openai", canonical_name="OpenAI", type="org"),
    }

    record_ids = rss_ingest.ensure_keyword_records(
        [{"name": "GPT-5", "type": "model"}],
        "tenant-token",
        index,
        threading.Lock(),
    )

    assert record_ids == ["rec_gpt5", "rec_gpt", "rec_openai"]


def test_keyword_link_values_ignores_empty_feishu_placeholder():
    assert rss_ingest.keyword_link_values([
        {"record_ids": None, "table_id": "tbl_keyword", "text": None, "text_arr": [], "type": "text"}
    ]) == []


def test_original_keyword_record_ids_from_expanded_drops_implied_parent_and_owner():
    records_by_id = {
        "rec_gpt5": rss_ingest.KeywordRecord(
            record_id="rec_gpt5",
            canonical_name="GPT-5",
            type="model",
            parent_ids=["rec_gpt"],
            owner_ids=["rec_openai"],
        ),
        "rec_gpt": rss_ingest.KeywordRecord(record_id="rec_gpt", canonical_name="GPT", type="model"),
        "rec_openai": rss_ingest.KeywordRecord(record_id="rec_openai", canonical_name="OpenAI", type="org"),
    }

    ids = rss_ingest.original_keyword_record_ids_from_expanded(
        ["rec_gpt5", "rec_gpt", "rec_openai"],
        records_by_id,
    )

    assert ids == ["rec_gpt5"]


def test_keyword_record_ids_from_cell_accepts_list_wrapped_link_record_ids():
    ids = rss_ingest._keyword_record_ids_from_cell(
        [
            {"link_record_ids": ["rec_gpt", "rec_openai"]},
            {"record_id": "rec_extra"},
        ]
    )

    assert ids == ["rec_gpt", "rec_openai", "rec_extra"]


def test_keyword_alias_index_keys_include_compact_variant():
    keys = rss_ingest.keyword_alias_index_keys("Claude Code")

    assert "claude code" in keys
    assert "compact:claudecode" in keys


def test_ensure_keyword_records_reuses_compact_existing_alias(monkeypatch):
    monkeypatch.setattr(rss_ingest.config, "FEISHU_KEYWORD_TABLE_ID", "tbl_keyword", raising=False)
    monkeypatch.setattr(
        rss_ingest,
        "create_bitable_record_with_id",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not create")),
    )
    index = {
        "compact:claudecode": rss_ingest.KeywordRecord(
            record_id="rec_claude_code",
            canonical_name="Claude Code",
            type="product",
        ),
    }

    record_ids = rss_ingest.ensure_keyword_records(
        [{"name": "claudecode", "type": "product"}],
        "tenant-token",
        index,
        threading.Lock(),
    )

    assert record_ids == ["rec_claude_code"]


def test_prefetch_keyword_index_skips_merged_keyword_records(monkeypatch):
    monkeypatch.setattr(rss_ingest.config, "FEISHU_KEYWORD_TABLE_ID", "tbl_keyword", raising=False)
    captured = {}

    def fake_list_records(*args, **kwargs):
        captured["max_pages"] = kwargs.get("max_pages")
        return [
            {
                "record_id": "rec_google",
                "fields": {
                    config.KEYWORD_FIELD_CANONICAL_NAME: "Google",
                    config.KEYWORD_FIELD_TYPE: "org",
                    config.KEYWORD_FIELD_ALIASES: "谷歌",
                },
            },
            {
                "record_id": "rec_old",
                "fields": {
                    config.KEYWORD_FIELD_CANONICAL_NAME: "谷歌",
                    config.KEYWORD_FIELD_TYPE: "org",
                    config.KEYWORD_FIELD_NOTE: "[merged→Google] rec_google",
                },
            },
            {
                "record_id": "rec_old_alt_marker",
                "fields": {
                    config.KEYWORD_FIELD_CANONICAL_NAME: "Google旧别名",
                    config.KEYWORD_FIELD_TYPE: "org",
                    config.KEYWORD_FIELD_NOTE: "[merged∪Google] rec_google",
                },
            },
        ]

    monkeypatch.setattr(rss_ingest, "list_bitable_records", fake_list_records)

    index = rss_ingest.prefetch_keyword_index("tenant-token")

    assert index["google"].record_id == "rec_google"
    assert index["谷歌"].record_id == "rec_google"
    assert "google旧别名" not in index
    assert captured["max_pages"] == 50


def test_prefetch_keyword_index_prefers_hotter_active_duplicate(monkeypatch):
    monkeypatch.setattr(rss_ingest.config, "FEISHU_KEYWORD_TABLE_ID", "tbl_keyword", raising=False)

    def fake_list_records(*args, **kwargs):
        return [
            {
                "record_id": "rec_main",
                "fields": {
                    config.KEYWORD_FIELD_CANONICAL_NAME: "Claude Code",
                    config.KEYWORD_FIELD_TYPE: "product",
                    config.KEYWORD_FIELD_NEWS_COUNT: 1,
                },
            },
            {
                "record_id": "rec_later_duplicate",
                "fields": {
                    config.KEYWORD_FIELD_CANONICAL_NAME: "claudecode",
                    config.KEYWORD_FIELD_TYPE: "product",
                    config.KEYWORD_FIELD_NEWS_COUNT: 20,
                    config.KEYWORD_FIELD_FILTERED_COUNT: 3,
                },
            },
        ]

    monkeypatch.setattr(rss_ingest, "list_bitable_records", fake_list_records)

    index = rss_ingest.prefetch_keyword_index("tenant-token")

    assert index["compact:claudecode"].record_id == "rec_later_duplicate"


def test_prefetch_keyword_index_for_ingest_refreshes_stale_runtime_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(rss_ingest.config, "FEISHU_KEYWORD_TABLE_ID", "tbl_keyword", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ENABLE_KEYWORD_SNAPSHOT_INDEX", True, raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_RUNTIME_SNAPSHOT_PATH", str(tmp_path / "runtime.json"), raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_PATH", str(tmp_path / "missing-local.json"), raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_URL", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_GIT_REF", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_GIT_PATH", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_MAX_AGE_HOURS", 24, raising=False)
    (tmp_path / "runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "generated_at": "2000-01-01T00:00:00",
                "source": "stale-runtime",
                "entry_count": 1001,
                "entries": [
                    {
                        "record_id": "rec_stale",
                        "canonical_name": "WSJ",
                        "type": "org",
                    },
                    *[
                        {"record_id": f"rec_pad_{index}", "canonical_name": f"Pad {index}", "type": "topic"}
                        for index in range(1000)
                    ],
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fake_list_records(*args, **kwargs):
        return [
            {
                "record_id": "rec_live",
                "fields": {
                    config.KEYWORD_FIELD_CANONICAL_NAME: "WSJ",
                    config.KEYWORD_FIELD_TYPE: "org",
                    config.KEYWORD_FIELD_NEWS_COUNT: 3,
                },
            }
        ]

    monkeypatch.setattr(rss_ingest, "list_bitable_records", fake_list_records)

    index = rss_ingest.prefetch_keyword_index_for_ingest("tenant-token")

    assert index["wsj"].record_id == "rec_live"


def test_prefetch_keyword_index_skips_blocklisted_keyword_records_and_aliases(monkeypatch):
    monkeypatch.setattr(rss_ingest.config, "FEISHU_KEYWORD_TABLE_ID", "tbl_keyword", raising=False)
    monkeypatch.setattr(rss_ingest, "_KEYWORD_NAME_BLOCKLIST", {"技术"}, raising=False)

    def fake_list_records(*args, **kwargs):
        return [
            {
                "record_id": "rec_blocked",
                "fields": {
                    config.KEYWORD_FIELD_CANONICAL_NAME: "技术",
                    config.KEYWORD_FIELD_TYPE: "technology",
                    config.KEYWORD_FIELD_ALIASES: "OpenTech",
                },
            },
            {
                "record_id": "rec_openai",
                "fields": {
                    config.KEYWORD_FIELD_CANONICAL_NAME: "OpenAI",
                    config.KEYWORD_FIELD_TYPE: "org",
                    config.KEYWORD_FIELD_ALIASES: "技术",
                },
            },
        ]

    monkeypatch.setattr(rss_ingest, "list_bitable_records", fake_list_records)

    index = rss_ingest.prefetch_keyword_index("tenant-token")

    assert index["openai"].record_id == "rec_openai"
    assert "技术" not in index
    assert "opentech" not in index


def test_build_keyword_index_from_snapshot_preserves_parent_and_owner_links():
    payload = {
        "schema_version": 2,
        "entries": [
            {
                "record_id": "rec_parent",
                "canonical_name": "OpenAI",
                "type": "org",
                "aliases": ["开放人工智能"],
                "news_count": 20,
                "filtered_count": 1,
                "note": "",
                "parent_ids": [],
                "owner_ids": [],
            },
            {
                "record_id": "rec_child",
                "canonical_name": "GPT-5",
                "type": "model",
                "aliases": ["gpt5"],
                "news_count": 3,
                "filtered_count": 0,
                "note": "",
                "parent_ids": ["rec_parent"],
                "owner_ids": ["rec_owner"],
            },
            {
                "record_id": "rec_owner",
                "canonical_name": "AI模型",
                "type": "technology",
                "aliases": [],
                "news_count": 1,
                "filtered_count": 0,
                "note": "",
                "parent_ids": [],
                "owner_ids": [],
            },
        ],
    }

    index = rss_ingest.build_keyword_index_from_snapshot_payload(payload)

    assert index["gpt5"].record_id == "rec_child"
    assert index["compact:gpt5"].record_id == "rec_child"
    assert index["gpt5"].parent_ids == ["rec_parent"]
    assert index["gpt5"].owner_ids == ["rec_owner"]
    assert rss_ingest.expand_keyword_record_ids(["rec_child"], index) == [
        "rec_child",
        "rec_parent",
        "rec_owner",
    ]


def test_prefetch_keyword_index_for_ingest_uses_runtime_snapshot_without_live_fetch(monkeypatch, tmp_path):
    snapshot_path = tmp_path / "keyword_runtime.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "generated_at": "2026-05-21T04:00:00",
                "entries": [
                    {
                        "record_id": "rec_openai",
                        "canonical_name": "OpenAI",
                        "type": "org",
                        "aliases": ["开放人工智能"],
                        "news_count": 10,
                        "filtered_count": 0,
                        "note": "",
                        "parent_ids": [],
                        "owner_ids": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(rss_ingest.config, "FEISHU_KEYWORD_TABLE_ID", "tbl_keyword", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ENABLE_KEYWORD_SNAPSHOT_INDEX", True, raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_URL", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_GIT_REF", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_MIN_ENTRIES", 1, raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_MAX_AGE_HOURS", 0, raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_RUNTIME_SNAPSHOT_PATH", str(snapshot_path), raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_PATH", str(tmp_path / "missing.json"), raising=False)
    monkeypatch.setattr(
        rss_ingest,
        "prefetch_keyword_index",
        lambda tenant_token: (_ for _ in ()).throw(AssertionError("live keyword table should not be fetched")),
    )

    index = rss_ingest.prefetch_keyword_index_for_ingest("tenant-token")

    assert index["openai"].record_id == "rec_openai"
    assert index["开放人工智能"].record_id == "rec_openai"


def test_prefetch_keyword_index_for_ingest_ignores_tiny_runtime_snapshot(monkeypatch, tmp_path):
    snapshot_path = tmp_path / "keyword_runtime.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "generated_at": "2026-05-21T04:00:00",
                "entries": [
                    {
                        "record_id": "rec_tiny",
                        "canonical_name": "Tiny",
                        "type": "org",
                        "aliases": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(rss_ingest.config, "FEISHU_KEYWORD_TABLE_ID", "tbl_keyword", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ENABLE_KEYWORD_SNAPSHOT_INDEX", True, raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_URL", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_GIT_REF", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_RUNTIME_SNAPSHOT_PATH", str(snapshot_path), raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_PATH", str(tmp_path / "missing.json"), raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_MIN_ENTRIES", 2, raising=False)
    monkeypatch.setattr(
        rss_ingest,
        "prefetch_keyword_index",
        lambda tenant_token: {
            "openai": rss_ingest.KeywordRecord(
                record_id="rec_live",
                canonical_name="OpenAI",
                type="org",
            )
        },
    )

    index = rss_ingest.prefetch_keyword_index_for_ingest("tenant-token")

    assert "tiny" not in index
    assert index["openai"].record_id == "rec_live"


def test_load_keyword_snapshot_payload_for_ingest_reads_git_snapshot(monkeypatch, tmp_path):
    payload = {
        "schema_version": 2,
        "generated_at": "2026-05-21T04:00:00",
        "entries": [
            {
                "record_id": "rec_openai",
                "canonical_name": "OpenAI",
                "type": "org",
                "aliases": [],
            }
        ],
    }

    class FakeCompleted:
        def __init__(self, stdout=""):
            self.stdout = stdout

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["git", "fetch"]:
            return FakeCompleted()
        if command[:2] == ["git", "show"]:
            return FakeCompleted(stdout=json.dumps(payload))
        raise AssertionError(command)

    monkeypatch.setattr(rss_ingest.subprocess, "run", fake_run)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_URL", "", raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_GIT_REF", "origin/main", raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_GIT_PATH", "data/keyword_snapshot.json", raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_GIT_FETCH", True, raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_GIT_FETCH_INTERVAL_MIN", 60, raising=False)
    monkeypatch.setattr(
        rss_ingest.config,
        "KEYWORD_SNAPSHOT_GIT_FETCH_STAMP_PATH",
        str(tmp_path / "git-fetch.stamp"),
        raising=False,
    )
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_MIN_ENTRIES", 1, raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_MAX_AGE_HOURS", 0, raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_RUNTIME_SNAPSHOT_PATH", str(tmp_path / "runtime.json"), raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_PATH", str(tmp_path / "missing.json"), raising=False)

    loaded = rss_ingest.load_keyword_snapshot_payload_for_ingest()

    assert loaded == payload
    assert calls[0][:3] == ["git", "fetch", "--quiet"]
    assert calls[1] == ["git", "show", "origin/main:data/keyword_snapshot.json"]


def test_git_keyword_snapshot_skips_fetch_while_stamp_is_fresh(monkeypatch, tmp_path):
    payload = {"schema_version": 2, "entries": []}
    stamp_path = tmp_path / "git-fetch.stamp"
    stamp_path.write_text("ok\n", encoding="utf-8")
    now = stamp_path.stat().st_mtime + 30
    calls = []

    class FakeCompleted:
        stdout = json.dumps(payload)

    def fake_run(command, **kwargs):
        calls.append(command)
        return FakeCompleted()

    monkeypatch.setattr(rss_ingest.subprocess, "run", fake_run)
    monkeypatch.setattr(rss_ingest.time, "time", lambda: now)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_GIT_REF", "origin/main", raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_GIT_PATH", "data/keyword_snapshot.json", raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_GIT_FETCH", True, raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_GIT_FETCH_INTERVAL_MIN", 60, raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_GIT_FETCH_STAMP_PATH", str(stamp_path), raising=False)

    loaded = rss_ingest._load_keyword_snapshot_payload_from_git()

    assert loaded == payload
    assert calls == [["git", "show", "origin/main:data/keyword_snapshot.json"]]


def test_git_keyword_snapshot_fetches_when_stamp_is_stale(monkeypatch, tmp_path):
    payload = {"schema_version": 2, "entries": []}
    stamp_path = tmp_path / "git-fetch.stamp"
    stamp_path.write_text("old\n", encoding="utf-8")
    now = stamp_path.stat().st_mtime + 3601
    calls = []

    class FakeCompleted:
        stdout = json.dumps(payload)

    def fake_run(command, **kwargs):
        calls.append(command)
        return FakeCompleted()

    monkeypatch.setattr(rss_ingest.subprocess, "run", fake_run)
    monkeypatch.setattr(rss_ingest.time, "time", lambda: now)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_GIT_REF", "origin/main", raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_GIT_PATH", "data/keyword_snapshot.json", raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_GIT_FETCH", True, raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_GIT_FETCH_INTERVAL_MIN", 60, raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_SNAPSHOT_GIT_FETCH_STAMP_PATH", str(stamp_path), raising=False)

    loaded = rss_ingest._load_keyword_snapshot_payload_from_git()

    assert loaded == payload
    assert calls[0][:3] == ["git", "fetch", "--quiet"]
    assert calls[1] == ["git", "show", "origin/main:data/keyword_snapshot.json"]
    assert stamp_path.stat().st_mtime > now - 60


def test_ensure_keyword_records_persists_created_keyword_to_runtime_snapshot(monkeypatch, tmp_path):
    snapshot_path = tmp_path / "keyword_runtime.json"
    monkeypatch.setattr(rss_ingest.config, "FEISHU_KEYWORD_TABLE_ID", "tbl_keyword", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ENABLE_KEYWORD_SNAPSHOT_INDEX", True, raising=False)
    monkeypatch.setattr(rss_ingest.config, "KEYWORD_RUNTIME_SNAPSHOT_PATH", str(snapshot_path), raising=False)
    monkeypatch.setattr(rss_ingest.time, "time", lambda: 1_700_000_000.0)

    def fake_create(app_token, table_id, tenant_token, fields, timeout, retries):
        return True, "rec_new"

    monkeypatch.setattr(rss_ingest, "create_bitable_record_with_id", fake_create)
    monkeypatch.setattr(rss_ingest, "lookup_keyword_record_by_name", lambda *args: None)

    record_ids = rss_ingest.ensure_keyword_records(
        [{"name": "NewModel", "type": "model"}],
        "tenant-token",
        {},
        threading.Lock(),
    )

    assert record_ids == ["rec_new"]
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["entries"] == [
        {
            "record_id": "rec_new",
            "canonical_name": "NewModel",
            "type": "model",
            "aliases": [],
            "news_count": 0,
            "filtered_count": 0,
            "note": "",
            "parent_ids": [],
            "owner_ids": [],
        }
    ]


def test_ensure_keyword_records_creates_missing_keyword(monkeypatch):
    monkeypatch.setattr(rss_ingest.config, "FEISHU_KEYWORD_TABLE_ID", "tbl_keyword", raising=False)
    captured = {}

    def fake_create(app_token, table_id, tenant_token, fields, timeout, retries):
        captured["table_id"] = table_id
        captured["fields"] = fields
        return True, "rec_new"

    monkeypatch.setattr(rss_ingest, "create_bitable_record_with_id", fake_create)
    monkeypatch.setattr(rss_ingest, "lookup_keyword_record_by_name", lambda *args: None)
    monkeypatch.setattr(rss_ingest.time, "time", lambda: 1700000000.0)
    index = {}

    record_ids = rss_ingest.ensure_keyword_records(
        [{"name": "OpenAI", "type": "org"}],
        "tenant-token",
        index,
        threading.Lock(),
        first_seen_ms=1701234567000,
    )

    assert record_ids == ["rec_new"]
    assert captured["table_id"] == "tbl_keyword"
    assert captured["fields"][config.KEYWORD_FIELD_CANONICAL_NAME] == "OpenAI"
    assert captured["fields"][config.KEYWORD_FIELD_TYPE] == "org"
    assert config.KEYWORD_FIELD_ALIASES not in captured["fields"]
    assert captured["fields"][config.KEYWORD_FIELD_FIRST_SEEN] == 1701234567000
    assert index["openai"].record_id == "rec_new"


def test_ensure_keyword_records_probes_live_table_before_create(monkeypatch):
    monkeypatch.setattr(rss_ingest.config, "FEISHU_KEYWORD_TABLE_ID", "tbl_keyword", raising=False)
    live_record = rss_ingest.KeywordRecord(
        record_id="rec_live",
        canonical_name="Claude Code",
        type="product",
        aliases=["claudecode"],
    )
    monkeypatch.setattr(rss_ingest, "lookup_keyword_record_by_name", lambda *args: live_record)
    monkeypatch.setattr(
        rss_ingest,
        "create_bitable_record_with_id",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should reuse live record")),
    )
    index = {}

    record_ids = rss_ingest.ensure_keyword_records(
        [{"name": "claudecode", "type": "product"}],
        "tenant-token",
        index,
        threading.Lock(),
    )

    assert record_ids == ["rec_live"]
    assert index["compact:claudecode"].record_id == "rec_live"


def test_keyword_names_from_analysis_filters_blocklisted(monkeypatch):
    monkeypatch.setattr(rss_ingest, "_KEYWORD_NAME_BLOCKLIST", {"ai", "人工智能", "技术"})
    analysis = {
        "keywords": [
            {"name": "AI", "type": "topic"},
            {"name": "OpenAI", "type": "org"},
            {"name": "人工智能", "type": "topic"},
            {"name": "AI Agent", "type": "product"},
        ],
    }
    names = rss_ingest.keyword_names_from_analysis(analysis)
    assert names == ["OpenAI", "AI Agent"]


def test_keyword_names_blocklist_is_exact_only(monkeypatch):
    monkeypatch.setattr(rss_ingest, "_KEYWORD_NAME_BLOCKLIST", {"芯片", "模型"})
    analysis = {
        "keywords": [
            {"name": "芯片", "type": "hardware"},
            {"name": "AI 芯片", "type": "hardware"},
            {"name": "模型", "type": "topic"},
            {"name": "GPT-5", "type": "model"},
        ],
    }
    names = rss_ingest.keyword_names_from_analysis(analysis)
    assert names == ["AI 芯片", "GPT-5"]


def test_keyword_names_country_and_action_blocklist_is_exact_only(monkeypatch):
    monkeypatch.setattr(rss_ingest, "_KEYWORD_NAME_BLOCKLIST", {"美国", "融资", "报告"})
    analysis = {
        "keywords": [
            {"name": "美国", "type": "topic"},
            {"name": "美国出口管制", "type": "policy"},
            {"name": "融资", "type": "topic"},
            {"name": "Anthropic融资传闻", "type": "case"},
            {"name": "报告", "type": "topic"},
            {"name": "AI安全报告", "type": "case"},
        ],
    }
    names = rss_ingest.keyword_names_from_analysis(analysis)
    assert names == ["美国出口管制", "Anthropic融资传闻", "AI安全报告"]


def test_build_filtered_fields_uses_title_zh_and_summary():
    analysis = {
        "action": "pass",
        "reason": "命中规则2：通稿。",
        "title_zh": "某公司发布新产品",
        "summary": "某公司宣布推出新AI工具，无实质技术细节。",
        "keywords": [{"name": "某公司", "type": "org"}],
        "_llm_meta": {"filter_method": "初筛过滤", "filter_reason": "命中规则2"},
    }
    fields = build_filtered_fields(_article(), analysis, "key-6")
    assert fields[config.FILTERED_FIELD_TITLE]["text"] == "某公司发布新产品"
    assert fields[config.FILTERED_FIELD_SUMMARY] == "某公司宣布推出新AI工具，无实质技术细节。"


def test_build_filtered_fields_falls_back_to_original_title():
    analysis = {
        "action": "pass",
        "reason": "命中规则2：通稿。",
        "keywords": [{"name": "某公司", "type": "org"}],
        "_llm_meta": {"filter_method": "初筛过滤"},
    }
    fields = build_filtered_fields(_article(), analysis, "key-7")
    assert fields[config.FILTERED_FIELD_TITLE]["text"] == "Some Title"
    assert fields[config.FILTERED_FIELD_SUMMARY] == "Hello"


def test_build_filtered_fields_uses_brief_summary_when_summary_empty():
    analysis = {
        "action": "ingest",
        "reason": "LLM文本去重",
        "brief_summary": "OpenAI 发布新模型。",
        "keywords": [{"name": "OpenAI", "type": "org"}],
        "_llm_meta": {"filter_method": "LLM文本去重"},
    }
    fields = build_filtered_fields(_article(), analysis, "key-brief")
    assert fields[config.FILTERED_FIELD_SUMMARY] == "OpenAI 发布新模型。"


def test_build_filtered_fields_uses_table_reason_not_internal_reason():
    analysis = {
        "action": "ingest",
        "reason": "LLM文本去重：内部判重理由",
        "brief_summary": "新摘要",
        "keywords": [{"name": "OpenAI", "type": "org"}],
        "_llm_meta": {
            "filter_method": "LLM文本去重",
            "filter_reason": "LLM文本去重：内部判重理由",
            "filter_table_reason": "相同新闻：旧标题\n摘要：旧摘要",
        },
    }

    fields = build_filtered_fields(_article(), analysis, "key-dedup")

    assert fields[config.FILTERED_FIELD_FILTER_REASON] == "相同新闻：旧标题\n摘要：旧摘要"


def test_ensure_keyword_records_skips_blocklisted(monkeypatch):
    monkeypatch.setattr(rss_ingest.config, "FEISHU_KEYWORD_TABLE_ID", "tbl_keyword", raising=False)
    monkeypatch.setattr(rss_ingest, "_KEYWORD_NAME_BLOCKLIST", {"ai", "技术"})
    monkeypatch.setattr(
        rss_ingest,
        "create_bitable_record_with_id",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not create blocked keyword")),
    )
    index = {}

    record_ids = rss_ingest.ensure_keyword_records(
        [{"name": "AI", "type": "topic"}, {"name": "技术", "type": "topic"}],
        "tenant-token",
        index,
        threading.Lock(),
    )

    assert record_ids == []
    assert len(index) == 0
