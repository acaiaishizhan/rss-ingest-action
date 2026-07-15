import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

import config  # noqa: E402
import rss_ingest  # noqa: E402
from tools import sync_keyword_expanded_links  # noqa: E402


def test_build_expanded_link_updates_adds_parent_and_owner():
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
    records = [
        {
            "record_id": "rec_news",
            "fields": {config.NEWS_FIELD_KEYWORD_RECORDS: ["rec_gpt5"]},
        }
    ]

    updates = sync_keyword_expanded_links.build_expanded_link_updates(
        records,
        config.NEWS_FIELD_KEYWORD_RECORDS,
        records_by_id,
    )

    assert updates == [
        {
            "record_id": "rec_news",
            "fields": {config.NEWS_FIELD_KEYWORD_RECORDS: ["rec_gpt5", "rec_gpt", "rec_openai"]},
        }
    ]


def test_build_expanded_link_updates_skips_when_already_expanded():
    records_by_id = {
        "rec_child": rss_ingest.KeywordRecord(
            record_id="rec_child",
            canonical_name="Child",
            type="model",
            parent_ids=["rec_parent"],
        ),
        "rec_parent": rss_ingest.KeywordRecord(record_id="rec_parent", canonical_name="Parent", type="model"),
    }

    updates = sync_keyword_expanded_links.build_expanded_link_updates(
        [
            {
                "record_id": "rec_news",
                "fields": {config.NEWS_FIELD_KEYWORD_RECORDS: ["rec_child", "rec_parent"]},
            }
        ],
        config.NEWS_FIELD_KEYWORD_RECORDS,
        records_by_id,
    )

    assert updates == []


def test_build_expanded_link_updates_does_not_add_missing_parent_records():
    records_by_id = {
        "rec_child": rss_ingest.KeywordRecord(
            record_id="rec_child",
            canonical_name="Child",
            type="model",
            parent_ids=["rec_merged_parent"],
        ),
    }

    updates = sync_keyword_expanded_links.build_expanded_link_updates(
        [
            {
                "record_id": "rec_news",
                "fields": {config.NEWS_FIELD_KEYWORD_RECORDS: ["rec_child"]},
            }
        ],
        config.NEWS_FIELD_KEYWORD_RECORDS,
        records_by_id,
    )

    assert updates == []


def test_apply_updates_uses_batch_update(monkeypatch):
    calls = []

    def fake_batch_update(app_token, table_id, tenant_token, records, timeout, retries):
        calls.append(("batch", table_id, records))
        return True, {"code": 0}

    def fake_single_update(*args, **kwargs):
        raise AssertionError("single update should not be called when batch succeeds")

    monkeypatch.setattr(sync_keyword_expanded_links, "batch_update_bitable_records", fake_batch_update)
    monkeypatch.setattr(sync_keyword_expanded_links, "update_bitable_record_fields", fake_single_update)

    updated, failed = sync_keyword_expanded_links.apply_updates(
        "tbl",
        "token",
        [
            {"record_id": "rec1", "fields": {"关键词记录": ["kw1"]}},
            {"record_id": "rec2", "fields": {"关键词记录": ["kw2"]}},
        ],
        sleep_seconds=0,
        batch_size=100,
    )

    assert updated == 2
    assert failed == []
    assert calls == [
        (
            "batch",
            "tbl",
            [
                {"record_id": "rec1", "fields": {"关键词记录": ["kw1"]}},
                {"record_id": "rec2", "fields": {"关键词记录": ["kw2"]}},
            ],
        )
    ]


def test_apply_updates_falls_back_to_single_records(monkeypatch):
    single_calls = []

    def fake_batch_update(*args, **kwargs):
        return False, {"code": 123, "msg": "bad batch"}

    def fake_single_update(app_token, table_id, tenant_token, record_id, fields, timeout, retries):
        single_calls.append(record_id)
        return record_id == "rec1"

    monkeypatch.setattr(sync_keyword_expanded_links, "batch_update_bitable_records", fake_batch_update)
    monkeypatch.setattr(sync_keyword_expanded_links, "update_bitable_record_fields", fake_single_update)

    updated, failed = sync_keyword_expanded_links.apply_updates(
        "tbl",
        "token",
        [
            {"record_id": "rec1", "fields": {"关键词记录": ["kw1"]}},
            {"record_id": "rec2", "fields": {"关键词记录": ["kw2"]}},
        ],
        sleep_seconds=0,
    )

    assert updated == 1
    assert single_calls == ["rec1", "rec2"]
    assert failed == [
        {
            "record_ids": ["rec2"],
            "error": "batch and single update returned false",
            "batch_error": {"code": 123, "msg": "bad batch"},
        }
    ]
