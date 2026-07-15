import json
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

import config  # noqa: E402
import rss_ingest  # noqa: E402
from tools import backfill_keywords  # noqa: E402


def _spec():
    return backfill_keywords.TableSpec(
        name="NEWS",
        table_id="tbl_news",
        title_field=config.NEWS_FIELD_TITLE,
        summary_field=config.NEWS_FIELD_SUMMARY,
        full_content_field=config.NEWS_FIELD_FULL_CONTENT,
        published_field=config.NEWS_FIELD_PUBLISHED_MS,
        created_field=config.NEWS_FIELD_CREATED_TIME,
        keywords_field=config.NEWS_FIELD_KEYWORDS,
        keyword_records_field=config.NEWS_FIELD_KEYWORD_RECORDS,
    )


def test_record_first_seen_ms_prefers_published_time():
    fields = {
        config.NEWS_FIELD_PUBLISHED_MS: 1700000000000,
        config.NEWS_FIELD_CREATED_TIME: 1800000000000,
    }

    assert backfill_keywords.record_first_seen_ms(fields, _spec()) == 1700000000000


def test_record_first_seen_ms_falls_back_to_created_time():
    fields = {
        config.NEWS_FIELD_CREATED_TIME: 1800000000000,
    }

    assert backfill_keywords.record_first_seen_ms(fields, _spec()) == 1800000000000


def test_article_from_record_uses_created_time_when_published_missing():
    record = {
        "fields": {
            config.NEWS_FIELD_TITLE: "OpenAI news",
            config.NEWS_FIELD_CREATED_TIME: 1800000000000,
        }
    }

    article = backfill_keywords.article_from_record(record, _spec())

    assert article["published"] == 1800000000


def test_prefetch_keyword_index_full_skips_merged_and_blocked(monkeypatch):
    rss_ingest._KEYWORD_NAME_BLOCKLIST = {"gpt"}

    def fake_list_records(*args, **kwargs):
        return [
            {
                "record_id": "rec_merged",
                "fields": {
                    config.KEYWORD_FIELD_CANONICAL_NAME: "Codex",
                    config.KEYWORD_FIELD_TYPE: "product",
                    config.KEYWORD_FIELD_NOTE: "[merged→Codex] rec_active",
                },
            },
            {
                "record_id": "rec_active",
                "fields": {
                    config.KEYWORD_FIELD_CANONICAL_NAME: "Codex",
                    config.KEYWORD_FIELD_TYPE: "product",
                },
            },
            {
                "record_id": "rec_blocked",
                "fields": {
                    config.KEYWORD_FIELD_CANONICAL_NAME: "GPT",
                    config.KEYWORD_FIELD_TYPE: "model",
                },
            },
        ]

    monkeypatch.setattr(config, "FEISHU_KEYWORD_TABLE_ID", "tbl_keyword")
    monkeypatch.setattr(backfill_keywords, "list_bitable_records", fake_list_records)

    index = backfill_keywords.prefetch_keyword_index_full("tenant", max_pages=1)

    assert index["codex"].record_id == "rec_active"
    assert "gpt" not in index


def test_prefetch_keyword_index_full_prefers_hotter_active_duplicate(monkeypatch):
    def fake_list_records(*args, **kwargs):
        return [
            {
                "record_id": "rec_main",
                "fields": {
                    config.KEYWORD_FIELD_CANONICAL_NAME: "Codex App",
                    config.KEYWORD_FIELD_TYPE: "product",
                    config.KEYWORD_FIELD_NEWS_COUNT: 1,
                },
            },
            {
                "record_id": "rec_later_duplicate",
                "fields": {
                    config.KEYWORD_FIELD_CANONICAL_NAME: "Codex.app",
                    config.KEYWORD_FIELD_TYPE: "product",
                    config.KEYWORD_FIELD_NEWS_COUNT: 9,
                    config.KEYWORD_FIELD_FILTERED_COUNT: 1,
                },
            },
        ]

    monkeypatch.setattr(config, "FEISHU_KEYWORD_TABLE_ID", "tbl_keyword")
    monkeypatch.setattr(backfill_keywords, "list_bitable_records", fake_list_records)

    index = backfill_keywords.prefetch_keyword_index_full("tenant", max_pages=1)

    assert index["compact:codexapp"].record_id == "rec_later_duplicate"


def test_fetch_candidates_skips_existing_links_unless_rebuild(monkeypatch):
    spec = _spec()
    records = [
        {
            "record_id": "rec_linked",
            "fields": {
                config.NEWS_FIELD_PUBLISHED_MS: 1800000000000,
                config.NEWS_FIELD_KEYWORD_RECORDS: ["kw1"],
            },
        },
        {
            "record_id": "rec_empty",
            "fields": {config.NEWS_FIELD_PUBLISHED_MS: 1800000000001},
        },
    ]

    monkeypatch.setattr(backfill_keywords, "fetch_window_records", lambda *args, **kwargs: records)

    normal = backfill_keywords.fetch_candidates(
        spec, "tenant", 1700000000000, None, 10, 200, 1, include_existing=False
    )
    rebuild = backfill_keywords.fetch_candidates(
        spec, "tenant", 1700000000000, None, 10, 200, 1, include_existing=True
    )

    assert [item["record_id"] for item in normal] == ["rec_empty"]
    assert [item["record_id"] for item in rebuild] == ["rec_linked", "rec_empty"]


def test_fetch_window_records_uses_created_time_when_published_missing(monkeypatch):
    spec = _spec()
    calls = []

    def fake_list_records(*args, **kwargs):
        calls.append(kwargs["sort"][0]["field_name"])
        if kwargs["sort"][0]["field_name"] == config.NEWS_FIELD_PUBLISHED_MS:
            return []
        return [
            {
                "record_id": "rec_created",
                "fields": {config.NEWS_FIELD_CREATED_TIME: 1800000000000},
            }
        ]

    monkeypatch.setattr(backfill_keywords, "list_bitable_records", fake_list_records)

    records = backfill_keywords.fetch_window_records(spec, "tenant", 1700000000000, None, 10, 200, 1)

    assert calls == [config.NEWS_FIELD_PUBLISHED_MS, config.NEWS_FIELD_CREATED_TIME]
    assert [item["record_id"] for item in records] == ["rec_created"]


def test_fetch_record_id_candidates_reads_ids_directly_without_time_window(monkeypatch):
    spec = _spec()

    def fake_get_record(table_id, record_id, tenant_token):
        return {
            "record_id": record_id,
            "fields": {
                config.NEWS_FIELD_PUBLISHED_MS: 1,
            },
        }

    monkeypatch.setattr(backfill_keywords, "get_bitable_record", fake_get_record)

    records = backfill_keywords.fetch_record_id_candidates(
        spec,
        "tenant",
        {"NEWS": {"rec_old"}},
        include_existing=True,
        limit=10,
    )

    assert [item["record_id"] for item in records] == ["rec_old"]


def test_run_converts_since_and_before_dates_to_window(monkeypatch, tmp_path):
    spec = _spec()
    captured = {}

    def fake_fetch_candidates(spec_arg, tenant, since_ms, before_ms, limit, page_size, max_pages, include_existing):
        captured["since_ms"] = since_ms
        captured["before_ms"] = before_ms
        captured["include_existing"] = include_existing
        return []

    monkeypatch.setattr(backfill_keywords, "required_config_errors", lambda dry_run: [])
    monkeypatch.setattr(backfill_keywords, "get_tenant_access_token", lambda *args: "tenant")
    monkeypatch.setattr(backfill_keywords, "load_screen_prompt", lambda: "prompt")
    monkeypatch.setattr(backfill_keywords, "table_specs", lambda: [spec])
    monkeypatch.setattr(backfill_keywords, "fetch_candidates", fake_fetch_candidates)
    monkeypatch.setattr(backfill_keywords, "process_candidates", lambda *args, **kwargs: None)

    args = backfill_keywords.parse_args(
        [
            "--since-date",
            "2026-05-01",
            "--before-date",
            "2026-05-03",
            "--state-path",
            str(tmp_path / "state.json"),
        ]
    )

    assert backfill_keywords.run(args) == 0
    assert captured == {
        "since_ms": 1777564800000,
        "before_ms": 1777737600000,
        "include_existing": False,
    }


def test_run_limits_candidates_to_record_ids_file(monkeypatch, tmp_path):
    spec = _spec()
    record_ids_path = tmp_path / "record_ids.json"
    record_ids_path.write_text(json.dumps({"NEWS": ["rec_keep"]}), encoding="utf-8")
    captured = {}

    def fake_fetch_record_id_candidates(*args, **kwargs):
        return [
            {"record_id": "rec_keep", "fields": {config.NEWS_FIELD_PUBLISHED_MS: 1800000000001}},
        ]

    def fake_process(candidates, *args, **kwargs):
        captured["record_ids"] = [item["record_id"] for item in candidates]

    monkeypatch.setattr(backfill_keywords, "required_config_errors", lambda dry_run: [])
    monkeypatch.setattr(backfill_keywords, "get_tenant_access_token", lambda *args: "tenant")
    monkeypatch.setattr(backfill_keywords, "load_screen_prompt", lambda: "prompt")
    monkeypatch.setattr(backfill_keywords, "table_specs", lambda: [spec])
    monkeypatch.setattr(backfill_keywords, "fetch_record_id_candidates", fake_fetch_record_id_candidates)
    monkeypatch.setattr(backfill_keywords, "process_candidates", fake_process)

    args = backfill_keywords.parse_args(
        [
            "--record-ids",
            str(record_ids_path),
            "--state-path",
            str(tmp_path / "state.json"),
        ]
    )

    assert backfill_keywords.run(args) == 0
    assert captured["record_ids"] == ["rec_keep"]


def test_state_path_skips_successful_records_and_records_new_success(monkeypatch, tmp_path):
    spec = _spec()
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"processed": {"NEWS": ["rec_done"]}}), encoding="utf-8")

    monkeypatch.setattr(backfill_keywords, "required_config_errors", lambda dry_run: [])
    monkeypatch.setattr(backfill_keywords, "get_tenant_access_token", lambda *args: "tenant")
    monkeypatch.setattr(backfill_keywords, "load_screen_prompt", lambda: "prompt")
    monkeypatch.setattr(backfill_keywords, "table_specs", lambda: [spec])
    monkeypatch.setattr(backfill_keywords, "prefetch_keyword_index_full", lambda *args: {})
    monkeypatch.setattr(
        backfill_keywords,
        "fetch_candidates",
        lambda *args, **kwargs: [
            {"record_id": "rec_done", "fields": {config.NEWS_FIELD_PUBLISHED_MS: 1800000000000}},
            {"record_id": "rec_new", "fields": {config.NEWS_FIELD_PUBLISHED_MS: 1800000000001}},
        ],
    )
    monkeypatch.setattr(
        backfill_keywords,
        "analyze_candidate",
        lambda record, spec, *args: backfill_keywords.AnalyzeResult(
            spec=spec,
            record=record,
            record_id=record["record_id"],
            title="title",
            names=["Codex"],
            keywords=[{"name": "Codex", "type": "product"}],
        ),
    )
    monkeypatch.setattr(backfill_keywords, "keyword_ids_for_result", lambda *args: ["kw_codex"])
    monkeypatch.setattr(backfill_keywords.time, "sleep", lambda seconds: None)

    captured_updates = []

    def fake_batch_update(app_token, table_id, tenant_token, records, timeout, retries):
        captured_updates.extend(records)
        return True, {}

    monkeypatch.setattr(backfill_keywords, "batch_update_bitable_records", fake_batch_update)

    args = backfill_keywords.parse_args(
        [
            "--apply",
            "--state-path",
            str(state_path),
            "--llm-concurrency",
            "1",
        ]
    )

    assert backfill_keywords.run(args) == 0
    assert [item["record_id"] for item in captured_updates] == ["rec_new"]
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "processed": {"NEWS": ["rec_done", "rec_new"]}
    }


def test_rebuild_existing_does_not_clear_empty_keywords_without_clear_when_empty(monkeypatch):
    spec = _spec()

    monkeypatch.setattr(
        backfill_keywords,
        "analyze_candidate",
        lambda record, spec, *args: backfill_keywords.AnalyzeResult(
            spec=spec,
            record=record,
            record_id=record["record_id"],
            title="title",
            names=[],
            keywords=[],
        ),
    )
    monkeypatch.setattr(backfill_keywords, "batch_update_bitable_records", lambda *args: (_ for _ in ()).throw(AssertionError("should not clear")))
    stats = backfill_keywords.BackfillStats()

    backfill_keywords.process_candidates(
        [{"record_id": "rec_existing", "fields": {config.NEWS_FIELD_PUBLISHED_MS: 1800000000000}}],
        spec,
        "tenant",
        "prompt",
        "provider",
        "model",
        False,
        1,
        1,
        {},
        backfill_keywords.threading.Lock(),
        stats,
        rebuild_existing=True,
        clear_when_empty=False,
    )

    assert stats.skipped == 1
    assert stats.updated == 0


def test_clear_when_empty_clears_old_fields_during_rebuild(monkeypatch):
    spec = _spec()
    captured = {}

    monkeypatch.setattr(
        backfill_keywords,
        "analyze_candidate",
        lambda record, spec, *args: backfill_keywords.AnalyzeResult(
            spec=spec,
            record=record,
            record_id=record["record_id"],
            title="title",
            names=[],
            keywords=[],
        ),
    )

    def fake_batch_update(app_token, table_id, tenant_token, records, timeout, retries):
        captured["records"] = records
        return True, {}

    monkeypatch.setattr(backfill_keywords, "batch_update_bitable_records", fake_batch_update)
    monkeypatch.setattr(backfill_keywords.time, "sleep", lambda seconds: None)
    stats = backfill_keywords.BackfillStats()

    backfill_keywords.process_candidates(
        [{"record_id": "rec_existing", "fields": {config.NEWS_FIELD_PUBLISHED_MS: 1800000000000}}],
        spec,
        "tenant",
        "prompt",
        "provider",
        "model",
        False,
        1,
        1,
        {},
        backfill_keywords.threading.Lock(),
        stats,
        rebuild_existing=True,
        clear_when_empty=True,
    )

    assert captured["records"] == [
        {
            "record_id": "rec_existing",
            "fields": {
                config.NEWS_FIELD_KEYWORD_RECORDS: [],
                config.NEWS_FIELD_KEYWORDS: "",
            },
        }
    ]
    assert stats.updated == 1


def test_keyword_name_sync_keeps_visible_keywords_original_when_links_are_expanded(monkeypatch):
    spec = _spec()
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
    captured = {}

    def fake_flush(updates, tenant_token, stats):
        captured["updates"] = updates
        stats.updated += len(updates)
        return [(item.spec.name, item.record_id) for item in updates]

    monkeypatch.setattr(backfill_keywords, "flush_updates", fake_flush)
    stats = backfill_keywords.BackfillStats()

    backfill_keywords.process_keyword_name_sync(
        [
            {
                "record_id": "rec_news",
                "fields": {
                    config.NEWS_FIELD_KEYWORD_RECORDS: ["rec_gpt5", "rec_gpt", "rec_openai"],
                    config.NEWS_FIELD_KEYWORDS: "GPT-5 / GPT / OpenAI",
                },
            }
        ],
        spec,
        "tenant",
        records_by_id,
        stats,
    )

    assert captured["updates"][0].keyword_names == ["GPT-5"]


def test_keyword_names_from_fields_splits_text_display_keywords():
    spec = _spec()

    names = backfill_keywords.keyword_names_from_fields(
        {spec.keywords_field: "OpenAI / GPT-5 / ChatGPT"},
        spec.keywords_field,
    )

    assert names == ["OpenAI", "GPT-5", "ChatGPT"]


def test_has_keyword_records_ignores_empty_feishu_placeholder():
    spec = _spec()

    fields = {
        spec.keyword_records_field: [
            {"record_ids": None, "table_id": "tbl_keyword", "text": None, "text_arr": [], "type": "text"}
        ]
    }

    assert backfill_keywords.has_keyword_records(fields, spec.keyword_records_field) is False
    assert backfill_keywords.keyword_record_ids_from_fields(fields, spec.keyword_records_field) == []


def test_flush_updates_can_clear_keyword_fields_for_rebuild(monkeypatch):
    spec = _spec()
    captured = {}

    def fake_batch_update(app_token, table_id, tenant_token, records, timeout, retries):
        captured["records"] = records
        return True, {}

    monkeypatch.setattr(backfill_keywords, "batch_update_bitable_records", fake_batch_update)
    monkeypatch.setattr(backfill_keywords.time, "sleep", lambda seconds: None)
    stats = backfill_keywords.BackfillStats()

    backfill_keywords.flush_updates(
        [
            backfill_keywords.PendingUpdate(
                spec=spec,
                record_id="rec1",
                keyword_record_ids=[],
                keyword_names=[],
                overwrite_empty=True,
            )
        ],
        "tenant",
        stats,
    )

    assert captured["records"] == [
        {
            "record_id": "rec1",
            "fields": {
                config.NEWS_FIELD_KEYWORD_RECORDS: [],
                config.NEWS_FIELD_KEYWORDS: "",
            },
        }
    ]
    assert stats.updated == 1


def test_validate_or_repair_screen_result_keeps_keywords_when_categories_invalid():
    analysis = backfill_keywords.validate_or_repair_screen_result(
        {
            "action": "ingest",
            "reason": "relevant",
            "categories": ["硬件"],
            "score": 8,
            "brief_summary": "硬件新闻。",
            "keywords": [{"name": "HBM", "type": "hardware"}],
        }
    )

    assert analysis["keywords"] == [{"name": "HBM", "type": "hardware"}]


def test_validate_or_repair_screen_result_allows_empty_keywords_for_backfill():
    analysis = backfill_keywords.validate_or_repair_screen_result(
        {
            "action": "pass",
            "reason": "no specific keyword",
            "summary": "无有效关键词。",
            "keywords": [],
        }
    )

    assert analysis["keywords"] == []
