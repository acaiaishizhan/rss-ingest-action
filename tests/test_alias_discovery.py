import os
import sys
import json
from pathlib import Path

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import alias_discovery
import config


def test_filter_low_frequency_keeps_single_use_keywords():
    entries = [
        alias_discovery.KeywordEntry(record_id="rec_one", canonical_name="GPT5.5", type="model"),
        alias_discovery.KeywordEntry(record_id="rec_two", canonical_name="GPT-5.5", type="model"),
    ]
    records = [
        {"record_id": "rec_one", "fields": {config.KEYWORD_FIELD_NEWS_COUNT: 1, config.KEYWORD_FIELD_FILTERED_COUNT: 0}},
        {"record_id": "rec_two", "fields": {config.KEYWORD_FIELD_NEWS_COUNT: 2, config.KEYWORD_FIELD_FILTERED_COUNT: 0}},
    ]

    result = alias_discovery.filter_low_frequency(entries, records)

    assert result == entries


def test_filter_entries_by_type_keeps_requested_types_only():
    entries = [
        alias_discovery.KeywordEntry(record_id="rec_model", canonical_name="GPT-4o", type="model"),
        alias_discovery.KeywordEntry(record_id="rec_org", canonical_name="OpenAI", type="org"),
        alias_discovery.KeywordEntry(record_id="rec_topic", canonical_name="AI Agent", type="topic"),
    ]

    result = alias_discovery.filter_entries_by_type(entries, "model, topic")

    assert [entry.record_id for entry in result] == ["rec_model", "rec_topic"]


def test_extract_llm_alias_groups_preserves_recall_uncertainty_fields():
    result = {
        "groups": [
            {
                "group": ["GPT5.5", "GPT-5.5"],
                "alias_type": "format_variant",
                "confidence": "medium",
                "why_might_match": "只差横杠",
                "why_might_not_match": "none",
            }
        ]
    }

    groups = alias_discovery.extract_llm_alias_groups(result, ["GPT5.5", "GPT-5.5"])

    assert groups == [
        {
            "group": ["GPT5.5", "GPT-5.5"],
            "reason": "只差横杠",
            "why_might_match": "只差横杠",
            "why_might_not_match": "none",
            "alias_type": "format_variant",
            "confidence": "medium",
        }
    ]


def test_stage2_veto_rejects_slash_combo_words():
    assert alias_discovery.stage2_veto_pair("ZhipuAI/GLM-5.1", "GLM-5.1", "model", "none") == "combo_word"


def test_deterministic_format_alias_pair_accepts_separator_variants():
    assert alias_discovery.deterministic_format_alias_pair("DeepSeek-R1", "DeepSeek R1")
    assert alias_discovery.deterministic_format_alias_pair("Qwen3.6-Plus", "Qwen3.6 Plus")


def test_deterministic_format_alias_pair_rejects_different_suffixes():
    assert not alias_discovery.deterministic_format_alias_pair("DeepSeek V4", "DeepSeekV4 Flash")


def test_stage2_veto_rejects_domain_alias_guess():
    assert alias_discovery.stage2_veto_pair("MiniMax", "minimaxi.com", "org", "none") == "domain_word"


def test_stage2_veto_allows_domain_format_variant():
    assert alias_discovery.stage2_veto_pair("linux.do", "LinuxDo", "org", "none") is None


def test_stage2_veto_rejects_subscription_tier_alias_guess():
    assert alias_discovery.stage2_veto_pair("GPT 会员", "GPT PLUS", "product", "none") == "tier_word"


def test_stage2_veto_rejects_uncertain_reason():
    assert alias_discovery.stage2_veto_pair("CVE-2024-Yikes", "NANOCLAW", "topic", "可能对应同一漏洞") == "uncertain_reason"


def test_stage2_veto_rejects_generic_ai_service_labels():
    assert alias_discovery.stage2_veto_pair("AI客服代理", "AI客服机器人", "product", "none") == "generic_ai_label"


def test_stage2_veto_allows_specific_ai_product_with_brand():
    assert alias_discovery.stage2_veto_pair("Bespoke AI 冰箱", "三星Bespoke AI冰箱", "product", "none") is None


def test_stage2_veto_rejects_plain_cjk_topic_phrases():
    assert alias_discovery.stage2_veto_pair("员工行为监控", "员工监控计划", "topic", "none") == "generic_cjk_topic"


def test_stage2_veto_allows_cross_language_topic_translation():
    assert alias_discovery.stage2_veto_pair("diarization", "说话人分离", "topic", "none") is None


def test_batch_verify_decision_rejects_accept_with_counterargument():
    result = {
        "decision": "accept",
        "is_alias": True,
        "positive_reason": "名称相似",
        "strongest_counterargument": "一个是基础产品，一个是 app 形态",
        "rejection_reason": "none",
    }

    assert alias_discovery.batch_verify_decision(result) == "reject"


def test_batch_verify_decision_rejects_uncertain_accept_reason():
    result = {
        "decision": "accept",
        "is_alias": True,
        "positive_reason": "两者可能对应同一个漏洞代号",
        "strongest_counterargument": "none",
        "rejection_reason": "none",
    }

    assert alias_discovery.batch_verify_decision(result) == "reject"


def test_batch_verify_prompt_mentions_product_variant_risk():
    prompt = alias_discovery.build_batch_verify_prompt("[]")

    assert "app/plugin/extension/client/desktop/windows/team/plus/pro" in prompt
    assert "基础产品名不要" in prompt
    assert "核心词+通用中文后缀" in prompt


def test_recall_prompt_mentions_core_suffix_completion_rule():
    with open("docs/local-alias-recall-prompt.md", encoding="utf-8") as f:
        prompt = f.read()

    assert "核心词补全规则" in prompt
    assert "不要只输出两个带后缀词" in prompt
    assert "漏掉 X 就是错误输出" in prompt


def test_extract_incremental_alias_groups_skips_non_object_items(monkeypatch):
    logs = []
    monkeypatch.setattr(alias_discovery, "log_progress", logs.append)
    result = {
        "mappings": [
            "bad llm item",
            {"new": "Open AI", "canonical": "OpenAI", "reason": "same org name spacing"},
        ]
    }
    history_entries = [
        alias_discovery.KeywordEntry(record_id="rec_old", canonical_name="OpenAI", type="org"),
    ]

    groups = alias_discovery.extract_incremental_alias_groups(result, ["Open AI"], history_entries)

    assert groups == [
        {
            "group": ["OpenAI", "Open AI"],
            "reason": "same org name spacing",
            "why_might_match": "same org name spacing",
            "why_might_not_match": "",
            "alias_type": "",
            "confidence": "",
        }
    ]
    assert logs == ["incremental parse skipped invalid_items=1"]


def test_incremental_alias_discovery_batches_new_names(monkeypatch, tmp_path):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "entries": [
                    {"record_id": "rec_old", "canonical_name": "GPT-5.5", "type": "model", "aliases": []}
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calls = []

    monkeypatch.setattr(alias_discovery, "get_tenant_access_token", lambda *args, **kwargs: "tenant")
    monkeypatch.setattr(
        alias_discovery,
        "fetch_recent_keyword_entries",
        lambda **kwargs: (
            [
                alias_discovery.KeywordEntry(record_id=f"rec{i}", canonical_name=f"GPT5.5-{i}", type="model")
                for i in range(5)
            ],
            [],
        ),
    )

    def fake_call(keyword_type, new_names, history_entries, provider, model):
        calls.append(list(new_names))
        return []

    monkeypatch.setattr(alias_discovery, "call_llm_for_incremental_alias_batch", fake_call)

    alias_discovery.run_three_stage(
        recall_prompt_path=Path("docs/local-alias-recall-prompt.md"),
        verify_prompt_path=Path("docs/local-alias-verify-prompt.md"),
        provider="gemini",
        model="gemini-3-flash-preview",
        page_size=500,
        max_pages=1,
        batch_size=2,
        type_filter="",
        keyword_snapshot_path=str(snapshot),
        incremental_since_ms=1,
    )

    assert [len(call) for call in calls] == [2, 2, 1]
