import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

from tools import audit_keyword_noise_llm  # noqa: E402


def test_select_candidates_prioritizes_obvious_noise():
    audit_keyword_noise_llm.rss_ingest._KEYWORD_NAME_BLOCKLIST = {"行业", "平台"}
    records = [
        {"record_id": "rec_claude", "keyword": "Claude", "type": "product", "note": "", "heat_30d": 10},
        {"record_id": "rec_industry", "keyword": "行业", "type": "topic", "note": "", "heat_30d": 0},
        {"record_id": "rec_platform", "keyword": "平台", "type": "topic", "note": "", "heat_30d": 0},
        {"record_id": "rec_settings", "keyword": "settings.json", "type": "technology", "note": "", "heat_30d": 0},
        {"record_id": "rec_manual", "keyword": "GPT", "type": "model", "note": "[manual] parent keyword", "heat_30d": 0},
    ]

    selected = audit_keyword_noise_llm.select_candidates(records, limit=2)

    assert [item["keyword"] for item in selected] == ["行业", "平台"]


def test_normalize_llm_items_fills_missing_records_as_review():
    batch = [
        {"record_id": "rec_dns", "keyword": "--dns", "type": "topic", "heat_30d": 0},
        {"record_id": "rec_ai", "keyword": "AI Agent", "type": "technology", "heat_30d": 3},
    ]

    items = audit_keyword_noise_llm.normalize_llm_items(
        {
            "items": [
                {
                    "record_id": "rec_dns",
                    "keyword": "--dns",
                    "decision": "block_auto",
                    "risk": "low",
                    "reason": "命令参数",
                }
            ]
        },
        batch,
    )

    assert items[0]["decision"] == "block_auto"
    assert items[1]["record_id"] == "rec_ai"
    assert items[1]["decision"] == "review"
    assert items[1]["risk"] == "high"


def test_normalize_llm_items_can_skip_missing_records_for_actionable_only():
    batch = [
        {"record_id": "rec_dns", "keyword": "--dns", "type": "topic", "heat_30d": 0},
        {"record_id": "rec_ai", "keyword": "AI Agent", "type": "technology", "heat_30d": 3},
    ]

    items = audit_keyword_noise_llm.normalize_llm_items(
        {
            "items": [
                {
                    "record_id": "rec_dns",
                    "keyword": "--dns",
                    "decision": "block_auto",
                    "risk": "low",
                    "reason": "命令参数",
                }
            ]
        },
        batch,
        fill_missing=False,
    )

    assert [item["record_id"] for item in items] == ["rec_dns"]


def test_filter_recent_records_uses_first_seen_ms():
    records = [
        {"record_id": "old", "keyword": "旧词", "first_seen_ms": 100},
        {"record_id": "new", "keyword": "新词", "first_seen_ms": 200},
        {"record_id": "missing", "keyword": "无时间"},
    ]

    assert [item["record_id"] for item in audit_keyword_noise_llm.filter_recent_records(records, 150)] == ["new"]


def test_build_batch_prompt_does_not_inject_dynamic_blocklist_examples():
    prompt = audit_keyword_noise_llm.build_batch_prompt(
        "固定规则",
        [{"record_id": "rec1", "keyword": "行业", "type": "topic"}],
    )

    assert "现有 exact-only 黑名单样例" not in prompt
    assert "待审查关键词" in prompt


def test_parse_first_json_object_allows_trailing_text():
    parsed = audit_keyword_noise_llm.parse_first_json_object(
        '{"items":[{"record_id":"rec1","decision":"block_auto"}]} trailing notes'
    )

    assert parsed == {"items": [{"record_id": "rec1", "decision": "block_auto"}]}


def test_normalize_llm_items_does_not_duplicate_record_across_action_buckets():
    batch = [
        {"record_id": "rec1", "keyword": "行业", "type": "topic", "heat_30d": 0},
    ]

    items = audit_keyword_noise_llm.normalize_llm_items(
        {
            "block_auto": [{"record_id": "rec1", "reason": "泛词"}],
            "review": [{"record_id": "rec1", "reason": "可能误伤"}],
        },
        batch,
        fill_missing=False,
    )

    assert len(items) == 1
    assert items[0]["record_id"] == "rec1"
    assert items[0]["decision"] == "block_auto"


def test_gemini_noise_audit_payload_uses_large_output_and_minimal_thinking():
    payload = audit_keyword_noise_llm.build_gemini_noise_audit_payload("审查这些关键词")

    assert payload["contents"][0]["role"] == "user"
    assert payload["generationConfig"]["maxOutputTokens"] == 65536
    assert payload["generationConfig"]["responseMimeType"] == "application/json"
    assert payload["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "minimal"


def test_noise_audit_batch_size_zero_means_single_full_batch():
    items = [{"record_id": "1"}, {"record_id": "2"}, {"record_id": "3"}]

    assert audit_keyword_noise_llm.chunks(items, 0) == [items]


def test_build_block_auto_delete_plan_skips_review_and_manual():
    audit_payload = {
        "items": [
            {"record_id": "rec_delete", "keyword": "行业", "decision": "block_auto"},
            {"record_id": "rec_review", "keyword": "AI 工厂", "decision": "review"},
            {"record_id": "rec_manual", "keyword": "平台", "decision": "block_auto"},
        ]
    }
    live_records = [
        {"record_id": "rec_delete", "fields": {"规范名": "行业", "备注": ""}},
        {"record_id": "rec_review", "fields": {"规范名": "AI 工厂", "备注": ""}},
        {"record_id": "rec_manual", "fields": {"规范名": "平台", "备注": "[manual] keep"}},
    ]

    plan = audit_keyword_noise_llm.build_block_auto_delete_plan(audit_payload, live_records)

    assert [item["record_id"] for item in plan["to_delete"]] == ["rec_delete"]
    assert [item["record_id"] for item in plan["skipped_manual"]] == ["rec_manual"]
    assert plan["review_count"] == 1
