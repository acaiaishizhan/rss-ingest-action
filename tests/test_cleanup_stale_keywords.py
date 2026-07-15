import os
import sys
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

from tools import cleanup_stale_keywords  # noqa: E402


def test_stale_cleanup_protects_recent_zero_heat_keywords():
    now_ms = int(time.time() * 1000)
    old_ms = now_ms - 90 * 24 * 3600 * 1000
    records = [
        {
            "record_id": "rec_new_keyword",
            "fields": {"规范名": "并发测试新词", "类型": "topic", "首次出现": now_ms, "30d": 0, "备注": ""},
        },
        {
            "record_id": "rec_old_keyword",
            "fields": {"规范名": "旧垃圾词", "类型": "topic", "首次出现": old_ms, "30d": 0, "备注": ""},
        },
        {
            "record_id": "rec_manual_keyword",
            "fields": {"规范名": "手动保留词", "类型": "topic", "首次出现": old_ms, "30d": 0, "备注": "manual"},
        },
    ]

    candidates = cleanup_stale_keywords.build_delete_candidates(records, min_age_hours=48)

    assert [item["record_id"] for item in candidates] == ["rec_old_keyword"]


def test_stale_cleanup_skips_missing_and_invalid_first_seen_zero_heat_keywords():
    old_ms = int(time.time() * 1000) - 90 * 24 * 3600 * 1000
    records = [
        {
            "record_id": "rec_missing_first_seen",
            "fields": {"规范名": "缺时间词", "类型": "topic", "30d": 0, "备注": ""},
        },
        {
            "record_id": "rec_bad_first_seen",
            "fields": {"规范名": "坏时间词", "类型": "topic", "首次出现": "not-a-time", "30d": 0, "备注": ""},
        },
        {
            "record_id": "rec_old_keyword",
            "fields": {"规范名": "旧垃圾词", "类型": "topic", "首次出现": old_ms, "30d": 0, "备注": ""},
        },
        {
            "record_id": "rec_manual_keyword",
            "fields": {"规范名": "手动保留词", "类型": "topic", "首次出现": old_ms, "30d": 0, "备注": "manual"},
        },
    ]

    candidates = cleanup_stale_keywords.build_delete_candidates(records, min_age_hours=48)
    missing_first_seen = cleanup_stale_keywords.build_missing_first_seen_records(records)

    assert [item["record_id"] for item in candidates] == ["rec_old_keyword"]
    assert [item["record_id"] for item in missing_first_seen] == ["rec_missing_first_seen", "rec_bad_first_seen"]


def test_stale_cleanup_can_disable_recent_keyword_protection():
    now_ms = int(time.time() * 1000)
    records = [
        {
            "record_id": "rec_new_keyword",
            "fields": {"规范名": "并发测试新词", "类型": "topic", "首次出现": now_ms, "30d": 0, "备注": ""},
        },
    ]

    candidates = cleanup_stale_keywords.build_delete_candidates(records, min_age_hours=0)

    assert [item["record_id"] for item in candidates] == ["rec_new_keyword"]


def test_stale_cleanup_excludes_protected_record_ids():
    old_ms = int(time.time() * 1000) - 90 * 24 * 3600 * 1000
    records = [
        {
            "record_id": "rec_merge_target",
            "fields": {"规范名": "合并目标", "类型": "topic", "首次出现": old_ms, "30d": 0, "备注": ""},
        },
        {
            "record_id": "rec_old_keyword",
            "fields": {"规范名": "旧垃圾词", "类型": "topic", "首次出现": old_ms, "30d": 0, "备注": ""},
        },
    ]

    candidates = cleanup_stale_keywords.build_delete_candidates(
        records,
        min_age_hours=48,
        exclude_record_ids={"rec_merge_target"},
    )

    assert [item["record_id"] for item in candidates] == ["rec_old_keyword"]
