import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import rss_ingest


def _new_stats():
    return {
        "secondary_sync_ok": 0,
        "secondary_sync_failed": 0,
    }


def test_prefetch_item_key_record_map_extracts_mapping(monkeypatch):
    monkeypatch.setattr(
        rss_ingest,
        "list_bitable_records",
        lambda *args, **kwargs: [
            {"record_id": "rid1", "fields": {rss_ingest.config.NEWS_FIELD_ITEM_KEY: "k1"}},
            {"record_id": "rid2", "fields": {rss_ingest.config.NEWS_FIELD_ITEM_KEY: "k2"}},
            {"record_id": "", "fields": {rss_ingest.config.NEWS_FIELD_ITEM_KEY: "k3"}},
        ],
    )
    out = rss_ingest.prefetch_item_key_record_map("tb_sync", "tenant")
    assert out == {"k1": "rid1", "k2": "rid2"}


def test_sync_secondary_records_updates_and_creates(monkeypatch):
    updated = []
    created = []

    def fake_update(*args, **kwargs):
        updated.append(args[3])
        return True

    def fake_create(*args, **kwargs):
        created.append(True)
        return True, "rid2"

    monkeypatch.setattr(rss_ingest, "update_bitable_record_fields", fake_update)
    monkeypatch.setattr(rss_ingest, "create_bitable_record_with_id", fake_create)

    pending = [
        {"item_key": "k1", "fields": {"a": 1}},
        {"item_key": "k2", "fields": {"a": 2}},
        {"item_key": "k1", "fields": {"a": 3}},  # duplicate key should be ignored
    ]
    record_map = {"k1": "rid1"}
    stats = _new_stats()
    rss_ingest.sync_secondary_records(pending, "tenant", "tb_sync", record_map, stats)

    assert updated == ["rid1"]
    assert len(created) == 1
    assert record_map["k2"] == "rid2"
    assert stats["secondary_sync_ok"] == 2
    assert stats["secondary_sync_failed"] == 0


def test_prefetch_item_key_record_map_uses_given_app_token(monkeypatch):
    called = {}

    def fake_list_bitable_records(app_token, *args, **kwargs):
        called["app_token"] = app_token
        return []

    monkeypatch.setattr(rss_ingest, "list_bitable_records", fake_list_bitable_records)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_TOKEN", "main_app")
    rss_ingest.prefetch_item_key_record_map("tb_sync", "tenant", app_token="sync_app")
    assert called["app_token"] == "sync_app"


def test_sync_secondary_records_use_sync_app_token(monkeypatch):
    called = {"create": [], "update": []}

    def fake_create(app_token, *args, **kwargs):
        called["create"].append(app_token)
        return True, "rid_new"

    def fake_update(app_token, *args, **kwargs):
        called["update"].append(app_token)
        return True

    monkeypatch.setattr(rss_ingest, "create_bitable_record_with_id", fake_create)
    monkeypatch.setattr(rss_ingest, "update_bitable_record_fields", fake_update)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_APP_TOKEN", "main_app")
    monkeypatch.setattr(rss_ingest.config, "FEISHU_SYNC_APP_TOKEN", "sync_app", raising=False)

    stats = _new_stats()
    record_map = {}
    rss_ingest.sync_secondary_records(
        [{"item_key": "k1", "fields": {"f": 1}}],
        "tenant",
        "tb_sync",
        record_map,
        stats,
    )

    assert called["create"] == ["sync_app"]
    assert called["update"] == []


def test_build_secondary_sync_fields_maps_news_summaries(monkeypatch):
    monkeypatch.setattr(rss_ingest.config, "FEISHU_SYNC_FIELD_SUMMARY", "总结", raising=False)
    fields = {
        rss_ingest.config.NEWS_FIELD_TITLE: "标题",
        rss_ingest.config.NEWS_FIELD_SUMMARY: "QA总结",
        rss_ingest.config.NEWS_FIELD_BRIEF_SUMMARY: "事实摘要",
        rss_ingest.config.NEWS_FIELD_KEYWORD_RECORDS: ["rec_kw"],
        rss_ingest.config.NEWS_FIELD_KEYWORDS: "OpenAI",
    }

    out = rss_ingest.build_secondary_sync_fields(fields)

    assert out[rss_ingest.config.NEWS_FIELD_TITLE] == "标题"
    assert out["总结"] == "事实摘要"
    assert rss_ingest.config.NEWS_FIELD_SUMMARY not in out
    assert rss_ingest.config.NEWS_FIELD_BRIEF_SUMMARY not in out
    assert rss_ingest.config.NEWS_FIELD_KEYWORD_RECORDS not in out
    assert out[rss_ingest.config.NEWS_FIELD_KEYWORDS] == "OpenAI"
