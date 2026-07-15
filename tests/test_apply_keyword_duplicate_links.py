import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

import config  # noqa: E402
from tools import apply_keyword_duplicate_links as apply_dup  # noqa: E402


def _candidate(old_id, old_name, target_id="", target_name="", *, reason="duplicate_alias_key", target_total=0):
    return {
        "old_record_id": old_id,
        "old_name": old_name,
        "old_type": "topic",
        "target_record_id": target_id,
        "target_name": target_name,
        "target_type": "topic",
        "reason": reason,
        "old_usage": {"total_count": 0, "linked_record_count": 0},
        "target_usage": {"total_count": target_total, "linked_record_count": target_total},
        "needs_relink": False,
        "linked_record_count": 0,
    }


def test_build_alias_plans_prefers_stronger_target_for_same_old_record():
    alias_to_plan, conflicts, skipped = apply_dup.build_alias_plans_from_audit(
        {
            "candidates": [
                _candidate("rec_ai_code_space", "AI 编程", "rec_ai_code", "AI编程", target_total=4),
                _candidate("rec_ai_code_space", "AI 编程", "rec_smart_code", "智能编码", target_total=1),
            ]
        }
    )

    assert alias_to_plan["rec_ai_code_space"]["main_record_id"] == "rec_ai_code"
    assert conflicts[0]["chosen_target_record_id"] == "rec_ai_code"
    assert skipped == []


def test_build_duplicate_link_updates_relinks_articles_and_marks_all_aliases():
    alias_to_plan = {
        "rec_old_linked": {
            "alias_record_id": "rec_old_linked",
            "alias_name": "NVIDIA",
            "main_record_id": "rec_target",
            "main_name": "英伟达",
            "type": "org",
        },
        "rec_old_unlinked": {
            "alias_record_id": "rec_old_unlinked",
            "alias_name": "Nvidia",
            "main_record_id": "rec_target",
            "main_name": "英伟达",
            "type": "org",
        },
    }
    keyword_records = [
        {"record_id": "rec_old_linked", "fields": {config.KEYWORD_FIELD_NOTE: "old note"}},
        {"record_id": "rec_old_unlinked", "fields": {}},
        {"record_id": "rec_target", "fields": {}},
    ]
    news_records = [
        {"record_id": "news_1", "fields": {config.NEWS_FIELD_KEYWORD_RECORDS: ["rec_old_linked", "rec_keep"]}},
    ]
    filtered_records = [
        {"record_id": "filtered_1", "fields": {config.FILTERED_FIELD_KEYWORD_RECORDS: ["rec_old_unlinked"]}},
    ]

    updates = apply_dup.build_duplicate_link_updates(alias_to_plan, keyword_records, news_records, filtered_records)

    assert updates["news_updates"] == [
        {"record_id": "news_1", "fields": {config.NEWS_FIELD_KEYWORD_RECORDS: ["rec_target", "rec_keep"]}},
    ]
    assert updates["filtered_updates"] == [
        {"record_id": "filtered_1", "fields": {config.FILTERED_FIELD_KEYWORD_RECORDS: ["rec_target"]}},
    ]
    assert updates["replacement_counts"] == {"rec_old_linked": 1, "rec_old_unlinked": 1}
    assert updates["keyword_note_updates"] == [
        {"record_id": "rec_old_linked", "fields": {config.KEYWORD_FIELD_NOTE: "old note\n[merged\u2192英伟达] rec_target"}},
        {"record_id": "rec_old_unlinked", "fields": {config.KEYWORD_FIELD_NOTE: "[merged\u2192英伟达] rec_target"}},
    ]
