import json
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

import merge_keywords


def _keyword(record_id, name, type_="product", aliases=None, news_count=0, filtered_count=0):
    return merge_keywords.KeywordEntry(
        record_id=record_id,
        canonical_name=name,
        type=type_,
        aliases=aliases or [],
        news_count=news_count,
        filtered_count=filtered_count,
    )


def _candidate(keyword_id, name, type_="product"):
    return {
        "keyword_id": keyword_id,
        "name": name,
        "type": type_,
        "aliases": [],
        "news_count": 1,
        "filtered_count": 0,
        "last_seen_at": "2026-05-11T13:30:00+08:00",
        "sample_titles": [name],
    }


def test_compact_key_groups_spacing_case_and_punctuation_variants():
    assert merge_keywords.compact_keyword_key("Claude Code") == merge_keywords.compact_keyword_key("claudecode")
    assert merge_keywords.compact_keyword_key("GPT-5.5") == merge_keywords.compact_keyword_key("gpt5.5")
    assert merge_keywords.compact_keyword_key("LinuxDo") == merge_keywords.compact_keyword_key("linux.do")


def test_build_merge_suggestions_keeps_substring_matches_separate():
    suggestions = merge_keywords.build_merge_suggestions(
        [
            _keyword("rec_openai", "OpenAI", "org"),
            _keyword("rec_invest", "300亿美元注资OpenAI", "case"),
        ]
    )

    assert suggestions == []


def test_build_merge_suggestions_outputs_readable_main_and_duplicates():
    suggestions = merge_keywords.build_merge_suggestions(
        [
            _keyword("rec_a", "claudecode"),
            _keyword("rec_b", "Claude Code"),
            _keyword("rec_c", "Claude-Code"),
        ]
    )

    assert suggestions == [
        {
            "main": "Claude Code",
            "main_record_id": "rec_b",
            "merge_into_main": [
                {"name": "Claude-Code", "record_id": "rec_c"},
                {"name": "claudecode", "record_id": "rec_a"},
            ],
            "type": "product",
            "reason": "写法只差大小写、空格、横杠、点号或类似分隔符",
        }
    ]


def test_build_candidate_group_uses_all_keyword_ids():
    group = merge_keywords.build_candidate_group(
        "g_claude_code",
        [
            _keyword("kw_a", "claudecode"),
            _keyword("kw_b", "Claude Code"),
            _keyword("kw_c", "Claude-Code"),
        ],
    )

    assert group["group_id"] == "g_claude_code"
    assert [item["keyword_id"] for item in group["candidates"]] == ["kw_a", "kw_b", "kw_c"]
    assert group["candidates"][1]["name"] == "Claude Code"


def test_candidate_group_includes_usage_stats_and_samples():
    usage = {
        "kw_a": merge_keywords.KeywordUsage(
            news_count=2,
            filtered_count=1,
            last_seen_ms=1778457600000,
            samples=[
                (1778457600000, "Claude Code 发布更新"),
                (1778371200000, "Claude Code 发布更新"),
                (1778284800000, "开发者讨论 claudecode"),
            ],
        )
    }

    group = merge_keywords.build_candidate_group(
        "g_claude_code",
        [_keyword("kw_a", "Claude Code")],
        usage=usage,
        sample_limit=2,
    )

    candidate = group["candidates"][0]
    assert candidate["news_count"] == 2
    assert candidate["filtered_count"] == 1
    assert candidate["last_seen_at"]
    assert candidate["sample_titles"] == ["Claude Code 发布更新", "开发者讨论 claudecode"]


def test_build_heat_sample_text_combines_counts_last_seen_and_titles():
    stat = merge_keywords.KeywordUsage(
        news_count=3,
        filtered_count=2,
        last_seen_ms=1778457600000,
        samples=[
            (1778457600000, "标题 A"),
            (1778371200000, "标题 B"),
        ],
    )

    text = merge_keywords.build_heat_sample_text(stat, limit=1)

    assert "NEWS次数: 3" in text
    assert "FILTERED次数: 2" in text
    assert "最后出现:" in text
    assert "- 标题 A" in text
    assert "标题 B" not in text


def test_build_keyword_core_update_fields_uses_config_names():
    stat = merge_keywords.KeywordUsage(
        news_count=1,
        filtered_count=2,
        first_seen_ms=1778371200000,
        last_seen_ms=1778457600000,
    )

    fields = merge_keywords.build_keyword_core_update_fields(stat, sample_limit=3)

    assert fields["NEWS次数"] == 1
    assert fields["FILTERED次数"] == 2
    assert fields["首次出现"] == 1778371200000
    assert fields["最后出现"] == 1778457600000
    assert "NEWS次数: 1" in fields["热度样本"]


def test_parse_linked_record_ids_accepts_strings_and_record_objects():
    assert merge_keywords.parse_linked_record_ids(
        [
            "rec_a",
            {"record_id": "rec_b"},
            {"id": "rec_c"},
            {"text": "not a record id"},
            "bad",
            "rec_a",
        ]
    ) == ["rec_a", "rec_b", "rec_c"]


def test_parse_linked_record_ids_accepts_feishu_link_record_ids_object():
    assert merge_keywords.parse_linked_record_ids({"link_record_ids": ["rec_a", "rec_b", "rec_a"]}) == [
        "rec_a",
        "rec_b",
    ]


def test_add_keyword_usage_from_records_counts_news_and_filtered():
    usage = {}
    records = [
        {
            "fields": {
                "关键词记录": ["rec_a", {"record_id": "rec_b"}],
                "标题": {"text": "同一条新闻"},
                "发布时间": 1778457600000,
            }
        },
        {
            "fields": {
                "关键词记录": ["rec_a"],
                "标题": {"text": "另一条新闻"},
                "发布时间": 1778371200000,
            }
        },
    ]

    merge_keywords.add_keyword_usage_from_records(
        usage,
        records,
        kind="news",
        link_field="关键词记录",
        title_field="标题",
        published_field="发布时间",
    )
    merge_keywords.add_keyword_usage_from_records(
        usage,
        records[:1],
        kind="filtered",
        link_field="关键词记录",
        title_field="标题",
        published_field="发布时间",
    )

    assert usage["rec_a"].news_count == 2
    assert usage["rec_a"].filtered_count == 1
    assert usage["rec_a"].first_seen_ms == 1778371200000
    assert usage["rec_a"].last_seen_ms == 1778457600000
    assert usage["rec_b"].news_count == 1
    assert usage["rec_b"].filtered_count == 1
    assert usage["rec_b"].first_seen_ms == 1778457600000


def test_build_compact_candidate_groups_adds_only_real_name_variants():
    groups = merge_keywords.build_compact_candidate_groups(
        [
            _keyword("rec_a", "Claude Code"),
            _keyword("rec_b", "claudecode"),
            _keyword("rec_c", "Claude"),
        ]
    )

    assert len(groups) == 1
    assert groups[0]["group_id"] == "compact::product::claudecode"
    assert {item["keyword_id"] for item in groups[0]["candidates"]} == {"rec_a", "rec_b"}


def test_build_alias_candidate_groups_adds_translation_variants():
    groups = merge_keywords.build_alias_candidate_groups(
        [
            _keyword("rec_nvidia", "Nvidia", "org"),
            _keyword("rec_cn", "英伟达", "org"),
            _keyword("rec_other", "AMD", "org"),
        ],
        [
            {
                "group_id": "alias::org::nvidia",
                "type": "org",
                "names": ["NVIDIA", "Nvidia", "英伟达"],
            }
        ],
    )

    assert len(groups) == 1
    assert groups[0]["group_id"] == "alias::org::nvidia"
    assert groups[0]["candidate_reason"] == "命中本地常用译名/别名种子"
    assert {item["keyword_id"] for item in groups[0]["candidates"]} == {"rec_nvidia", "rec_cn"}


def test_build_alias_update_preview_appends_alias_to_main_record():
    payload = merge_keywords.build_alias_update_preview(
        [
            _keyword("rec_mcp", "MCP", "technology"),
            _keyword("rec_mcp_cn", "MCP协议", "technology"),
        ],
        [
            {
                "pair": ["MCP协议", "MCP"],
                "type": "technology",
                "recall_reason": "MCP协议是MCP的完整形式",
            }
        ],
        source_path="out/alias.json",
    )

    assert payload["mode"] == "alias-update-preview"
    assert payload["update_count"] == 1
    update = payload["updates"][0]
    assert update["canonical_record_id"] == "rec_mcp"
    assert update["canonical_name"] == "MCP"
    assert update["add_aliases"] == ["MCP协议"]
    assert update["new_aliases_text"] == "MCP协议"


def test_build_alias_update_preview_skips_existing_alias():
    payload = merge_keywords.build_alias_update_preview(
        [
            _keyword("rec_mcp", "MCP", "technology", aliases=["MCP协议"]),
            _keyword("rec_mcp_cn", "MCP协议", "technology"),
        ],
        [{"pair": ["MCP协议", "MCP"], "type": "technology"}],
    )

    assert payload["update_count"] == 0
    assert payload["skipped"][0]["reason"] == "aliases_already_present"


def test_build_alias_update_preview_reports_missing_keyword_record():
    payload = merge_keywords.build_alias_update_preview(
        [_keyword("rec_mcp", "MCP", "technology")],
        [{"pair": ["MCP协议", "MCP"], "type": "technology"}],
    )

    assert payload["update_count"] == 0
    assert payload["skipped"][0]["missing"] == ["MCP协议"]


def test_build_alias_update_preview_merges_transitive_pairs_into_one_update():
    payload = merge_keywords.build_alias_update_preview(
        [
            _keyword("rec_a", "DeepSeek V4-Pro", "model"),
            _keyword("rec_b", "DeepSeekV4-Pro", "model"),
            _keyword("rec_c", "DeepSeek-v4pro", "model"),
        ],
        [
            {"pair": ["DeepSeek V4-Pro", "DeepSeekV4-Pro"], "type": "model"},
            {"pair": ["DeepSeekV4-Pro", "DeepSeek-v4pro"], "type": "model"},
        ],
    )

    assert payload["update_count"] == 1
    update = payload["updates"][0]
    assert update["canonical_name"] == "DeepSeek-v4pro"
    assert update["add_aliases"] == ["DeepSeek V4-Pro", "DeepSeekV4-Pro"]


def test_choose_alias_update_main_uses_usage_then_shorter_name():
    assert merge_keywords.choose_alias_update_main(
        [
            _keyword("rec_harness_arch", "Harness架构", "technology", news_count=4),
            _keyword("rec_harness", "Harness", "technology", news_count=5),
            _keyword("rec_harness_layer", "Harness层", "technology", news_count=1),
        ]
    ).canonical_name == "Harness"

    assert merge_keywords.choose_alias_update_main(
        [
            _keyword("rec_harness_arch", "Harness架构", "technology", news_count=5),
            _keyword("rec_harness", "Harness", "technology", news_count=5),
        ]
    ).canonical_name == "Harness"


def test_build_alias_update_apply_records_writes_new_alias_text_only():
    records = merge_keywords.build_alias_update_apply_records(
        {
            "mode": "alias-update-preview",
            "updates": [
                {
                    "canonical_record_id": "rec_harness",
                    "canonical_name": "Harness",
                    "new_aliases": ["Harness层", "Harness架构", "Harness层"],
                }
            ],
        }
    )

    assert records == [
        {
            "record_id": "rec_harness",
            "fields": {"归一项": "Harness层\nHarness架构"},
        }
    ]


def test_alias_update_apply_returns_nonzero_when_feishu_write_fails(monkeypatch, tmp_path):
    preview_path = tmp_path / "alias-preview.json"
    output_path = tmp_path / "alias-apply.json"
    preview_path.write_text(
        json.dumps(
            {
                "mode": "alias-update-preview",
                "updates": [
                    {
                        "canonical_record_id": "rec_harness",
                        "canonical_name": "Harness",
                        "new_aliases": ["Harness层"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(merge_keywords, "get_tenant_access_token", lambda *args, **kwargs: "tenant")
    monkeypatch.setattr(
        merge_keywords,
        "batch_update_bitable_records",
        lambda *args, **kwargs: (False, {"error": "write failed"}),
    )

    rc = merge_keywords.main(["--alias-update-apply", str(preview_path), "--output", str(output_path)])

    assert rc == 2
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["failed"]


def test_build_candidate_groups_includes_compact_and_alias_groups():
    groups = merge_keywords.build_candidate_groups(
        [
            _keyword("rec_claude", "Claude Code", "product"),
            _keyword("rec_claudecode", "claudecode", "product"),
            _keyword("rec_trump_a", "特朗普", "person"),
            _keyword("rec_trump_b", "川普", "person"),
        ],
        alias_seed_groups=[
            {
                "group_id": "alias::person::trump",
                "type": "person",
                "names": ["特朗普", "川普"],
            }
        ],
    )

    assert {group["group_id"] for group in groups} == {
        "compact::product::claudecode",
        "alias::person::trump",
    }


def test_validate_llm_result_accepts_all_or_nothing_merge():
    group = {
        "group_id": "g_claude_code",
        "candidates": [
            _candidate("kw_a", "Claude Code"),
            _candidate("kw_b", "claudecode"),
        ],
    }
    result = {
        "group_id": "g_claude_code",
        "decision": "merge",
        "confidence": 0.98,
        "risk": "low",
        "canonical_id": "kw_a",
        "items": [
            {"keyword_id": "kw_a", "name": "Claude Code", "action": "canonical", "reason": "规范写法"},
            {"keyword_id": "kw_b", "name": "claudecode", "action": "merge_to_canonical", "reason": "空格差异"},
        ],
        "merge_ids": ["kw_b"],
        "skip_ids": [],
        "force_skip_reason": "",
    }

    ok, error = merge_keywords.validate_llm_result(group, result)

    assert ok is True
    assert error == ""


def test_validate_llm_result_rejects_partial_merge_group():
    group = {
        "group_id": "g_mixed",
        "candidates": [
            _candidate("kw_a", "Claude"),
            _candidate("kw_b", "Claude Code"),
            _candidate("kw_c", "claudecode"),
        ],
    }
    result = {
        "group_id": "g_mixed",
        "decision": "merge",
        "confidence": 0.98,
        "risk": "low",
        "canonical_id": "kw_b",
        "items": [
            {"keyword_id": "kw_a", "name": "Claude", "action": "skip", "reason": "上位词"},
            {"keyword_id": "kw_b", "name": "Claude Code", "action": "canonical", "reason": "规范写法"},
            {"keyword_id": "kw_c", "name": "claudecode", "action": "merge_to_canonical", "reason": "空格差异"},
        ],
        "merge_ids": ["kw_c"],
        "skip_ids": ["kw_a"],
        "force_skip_reason": "",
    }

    ok, error = merge_keywords.validate_llm_result(group, result)

    assert ok is False
    assert "skip_ids must be empty" in error


def test_validate_llm_result_rejects_cross_type_merge():
    group = {
        "group_id": "g_cross_type",
        "candidates": [
            _candidate("kw_a", "OpenAI", "org"),
            _candidate("kw_b", "ChatGPT", "product"),
        ],
    }
    result = {
        "group_id": "g_cross_type",
        "decision": "merge",
        "confidence": 0.99,
        "risk": "low",
        "canonical_id": "kw_a",
        "items": [
            {"keyword_id": "kw_a", "name": "OpenAI", "action": "canonical", "reason": "公司"},
            {"keyword_id": "kw_b", "name": "ChatGPT", "action": "merge_to_canonical", "reason": "产品"},
        ],
        "merge_ids": ["kw_b"],
        "skip_ids": [],
        "force_skip_reason": "",
    }

    ok, error = merge_keywords.validate_llm_result(group, result)

    assert ok is False
    assert "type mismatch" in error


def test_validate_llm_result_rejects_dirty_skip_result():
    group = {
        "group_id": "g_skip",
        "candidates": [
            _candidate("kw_a", "Claude"),
            _candidate("kw_b", "Claude Code"),
        ],
    }
    result = {
        "group_id": "g_skip",
        "decision": "skip",
        "confidence": 0.99,
        "risk": "high",
        "canonical_id": "kw_a",
        "items": [
            {"keyword_id": "kw_a", "name": "Claude", "action": "canonical", "reason": "泛称"},
            {"keyword_id": "kw_b", "name": "Claude Code", "action": "skip", "reason": "具体工具"},
        ],
        "merge_ids": [],
        "skip_ids": ["kw_b"],
        "force_skip_reason": "上位词",
    }

    ok, error = merge_keywords.validate_llm_result(group, result)

    assert ok is False
    assert "skip canonical_id must be empty" in error


def test_validate_llm_result_rejects_duplicate_items():
    group = {
        "group_id": "g_duplicate",
        "candidates": [
            _candidate("kw_a", "Claude Code"),
            _candidate("kw_b", "claudecode"),
        ],
    }
    result = {
        "group_id": "g_duplicate",
        "decision": "skip",
        "confidence": 0.99,
        "risk": "high",
        "canonical_id": "",
        "items": [
            {"keyword_id": "kw_a", "name": "Claude Code", "action": "skip", "reason": "样本不足"},
            {"keyword_id": "kw_a", "name": "Claude Code", "action": "skip", "reason": "重复项"},
            {"keyword_id": "kw_b", "name": "claudecode", "action": "skip", "reason": "样本不足"},
        ],
        "merge_ids": [],
        "skip_ids": ["kw_a", "kw_b"],
        "force_skip_reason": "样本不足",
    }

    ok, error = merge_keywords.validate_llm_result(group, result)

    assert ok is False
    assert "items must appear exactly once" in error


def test_llm_results_are_consistent_when_canonical_and_merge_ids_match():
    first = {
        "decision": "merge",
        "confidence": 0.98,
        "risk": "low",
        "canonical_id": "kw_a",
        "merge_ids": ["kw_b", "kw_c"],
    }
    second = {
        "decision": "merge",
        "confidence": 0.97,
        "risk": "low",
        "canonical_id": "kw_a",
        "merge_ids": ["kw_c", "kw_b"],
    }

    assert merge_keywords.llm_results_consistent(first, second) is True


def test_llm_results_are_consistent_when_only_canonical_choice_differs():
    first = {
        "decision": "merge",
        "confidence": 0.98,
        "risk": "low",
        "canonical_id": "kw_a",
        "merge_ids": ["kw_b", "kw_c"],
    }
    second = {
        "decision": "merge",
        "confidence": 0.98,
        "risk": "low",
        "canonical_id": "kw_b",
        "merge_ids": ["kw_a", "kw_c"],
    }

    assert merge_keywords.llm_results_consistent(first, second) is True


def test_skip_results_are_consistent_even_when_risk_wording_differs():
    first = {
        "decision": "skip",
        "confidence": 0.95,
        "risk": "medium",
        "canonical_id": "",
        "merge_ids": [],
    }
    second = {
        "decision": "skip",
        "confidence": 0.96,
        "risk": "high",
        "canonical_id": "",
        "merge_ids": [],
    }

    assert merge_keywords.llm_results_consistent(first, second) is True


def test_build_llm_article_puts_group_json_in_content():
    group = {
        "group_id": "g_claude_code",
        "candidates": [
            _candidate("kw_a", "Claude Code"),
            _candidate("kw_b", "claudecode"),
        ],
    }

    article = merge_keywords.build_llm_article(group)

    assert article["title"] == "keyword merge group g_claude_code"
    assert '"group_id": "g_claude_code"' in article["content"]
    assert '"name": "Claude Code"' in article["content"]


def test_auto_merge_ready_requires_valid_consistent_low_risk_results():
    group = {
        "group_id": "g_claude_code",
        "candidates": [
            _candidate("kw_a", "Claude Code"),
            _candidate("kw_b", "claudecode"),
        ],
    }
    first = {
        "group_id": "g_claude_code",
        "decision": "merge",
        "confidence": 0.98,
        "risk": "low",
        "canonical_id": "kw_a",
        "items": [
            {"keyword_id": "kw_a", "name": "Claude Code", "action": "canonical", "reason": "规范写法"},
            {"keyword_id": "kw_b", "name": "claudecode", "action": "merge_to_canonical", "reason": "空格差异"},
        ],
        "merge_ids": ["kw_b"],
        "skip_ids": [],
        "force_skip_reason": "",
    }
    second = dict(first, confidence=0.97)

    ready, error = merge_keywords.auto_merge_ready(group, first, second)

    assert ready is True
    assert error == ""


def test_deterministic_merge_plan_prefers_readable_name():
    group = {
        "group_id": "g_claude_code",
        "candidates": [
            _candidate("kw_a", "claudecode"),
            _candidate("kw_b", "Claude Code"),
        ],
    }

    plan = merge_keywords.deterministic_merge_plan(group)

    assert plan == {"canonical_id": "kw_b", "merge_ids": ["kw_a"]}


def test_auto_merge_ready_rejects_invalid_second_run():
    group = {
        "group_id": "g_claude_code",
        "candidates": [
            _candidate("kw_a", "Claude Code"),
            _candidate("kw_b", "claudecode"),
        ],
    }
    first = {
        "group_id": "g_claude_code",
        "decision": "merge",
        "confidence": 0.98,
        "risk": "low",
        "canonical_id": "kw_a",
        "items": [
            {"keyword_id": "kw_a", "name": "Claude Code", "action": "canonical", "reason": "规范写法"},
            {"keyword_id": "kw_b", "name": "claudecode", "action": "merge_to_canonical", "reason": "空格差异"},
        ],
        "merge_ids": ["kw_b"],
        "skip_ids": [],
        "force_skip_reason": "",
    }
    second = dict(first, risk="medium")

    ready, error = merge_keywords.auto_merge_ready(group, first, second)

    assert ready is False
    assert "second invalid" in error
