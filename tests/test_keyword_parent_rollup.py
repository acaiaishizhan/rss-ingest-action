import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

from tools import keyword_parent_rollup  # noqa: E402


def test_choose_shortest_parent_by_contains():
    entries = [
        keyword_parent_rollup.KeywordEntry("rec_codex", "Codex", "product", ""),
        keyword_parent_rollup.KeywordEntry("rec_codex_app", "Codex App", "product", ""),
        keyword_parent_rollup.KeywordEntry("rec_codex_cli", "codex cli", "product", ""),
    ]

    plans = keyword_parent_rollup.build_parent_plans(entries, generic_names=set())

    assert plans["rec_codex_app"].parent_id == "rec_codex"
    assert plans["rec_codex_cli"].parent_id == "rec_codex"


def test_generic_word_cannot_be_parent():
    entries = [
        keyword_parent_rollup.KeywordEntry("rec_ai", "AI", "topic", ""),
        keyword_parent_rollup.KeywordEntry("rec_ai_agent", "AI Agent", "product", ""),
    ]

    plans = keyword_parent_rollup.build_parent_plans(entries, generic_names={"ai"})

    assert "rec_ai_agent" not in plans


def test_product_can_use_hot_org_parent():
    entries = [
        keyword_parent_rollup.KeywordEntry("rec_google", "Google", "org", "", 100, 0),
        keyword_parent_rollup.KeywordEntry("rec_google_ai_studio", "Google AI Studio", "product", "", 4, 0),
    ]

    plans = keyword_parent_rollup.build_parent_plans(entries, generic_names=set())

    assert plans["rec_google_ai_studio"].parent_id == "rec_google"


def test_product_prefers_hotter_product_line_over_org_when_available():
    entries = [
        keyword_parent_rollup.KeywordEntry("rec_openai", "OpenAI", "org", "", 20, 0),
        keyword_parent_rollup.KeywordEntry("rec_codex", "Codex", "product", "", 80, 0),
        keyword_parent_rollup.KeywordEntry("rec_openai_codex_app", "OpenAI Codex App", "product", "", 2, 0),
    ]

    plans = keyword_parent_rollup.build_parent_plans(entries, generic_names=set())

    assert plans["rec_openai_codex_app"].parent_id == "rec_codex"


def test_policy_can_use_hot_org_parent():
    entries = [
        keyword_parent_rollup.KeywordEntry("rec_eu", "欧盟", "org", "", 80, 0),
        keyword_parent_rollup.KeywordEntry("rec_dma", "欧盟数字市场法案", "policy", "", 5, 0),
    ]

    plans = keyword_parent_rollup.build_parent_plans(entries, generic_names=set())

    assert plans["rec_dma"].parent_id == "rec_eu"


def test_eu_policy_can_parent_but_eu_bank_group_is_not_entity_child():
    entries = [
        keyword_parent_rollup.KeywordEntry("rec_eu", "欧盟", "org", "", 80, 0),
        keyword_parent_rollup.KeywordEntry("rec_ai_act", "欧盟AI法案", "policy", "", 5, 0),
        keyword_parent_rollup.KeywordEntry("rec_eu_bank", "欧盟银行", "org", "", 5, 0),
    ]

    plans = keyword_parent_rollup.build_parent_plans(entries, generic_names=set())

    assert plans["rec_ai_act"].parent_id == "rec_eu"
    assert "rec_eu_bank" not in plans


def test_short_cjk_non_org_word_cannot_be_parent():
    entries = [
        keyword_parent_rollup.KeywordEntry("rec_security", "安全", "topic", "", 80, 0),
        keyword_parent_rollup.KeywordEntry("rec_ai_security", "AI安全", "topic", "", 5, 0),
    ]

    plans = keyword_parent_rollup.build_parent_plans(entries, generic_names=set())

    assert "rec_ai_security" not in plans


def test_location_word_cannot_be_parent():
    entries = [
        keyword_parent_rollup.KeywordEntry("rec_uk", "英国", "org", "", 80, 0),
        keyword_parent_rollup.KeywordEntry("rec_uk_ai", "英国AI战略", "policy", "", 5, 0),
    ]

    plans = keyword_parent_rollup.build_parent_plans(entries, generic_names=set())

    assert "rec_uk_ai" not in plans


def test_short_cjk_org_parent_must_be_prefix():
    entries = [
        keyword_parent_rollup.KeywordEntry("rec_peking", "北大", "org", "", 80, 0),
        keyword_parent_rollup.KeywordEntry("rec_northeastern", "东北大学", "org", "", 5, 0),
        keyword_parent_rollup.KeywordEntry("rec_tencent", "腾讯", "org", "", 80, 0),
        keyword_parent_rollup.KeywordEntry("rec_yuanbao", "腾讯元宝", "product", "", 5, 0),
    ]

    plans = keyword_parent_rollup.build_parent_plans(entries, generic_names=set())

    assert "rec_northeastern" not in plans
    assert plans["rec_yuanbao"].parent_id == "rec_tencent"


def test_short_cjk_org_parent_does_not_capture_technology_homographs():
    entries = [
        keyword_parent_rollup.KeywordEntry("rec_byte", "字节", "org", "", 80, 0),
        keyword_parent_rollup.KeywordEntry("rec_bytecode", "字节码解释", "technology", "", 1, 0),
        keyword_parent_rollup.KeywordEntry("rec_tencent", "腾讯", "org", "", 80, 0),
        keyword_parent_rollup.KeywordEntry("rec_tencent_ai", "腾讯AI", "org", "", 1, 0),
    ]

    plans = keyword_parent_rollup.build_parent_plans(entries, generic_names=set())

    assert "rec_bytecode" not in plans
    assert plans["rec_tencent_ai"].parent_id == "rec_tencent"


def test_model_can_use_hot_product_parent_over_short_model_parent():
    entries = [
        keyword_parent_rollup.KeywordEntry("rec_claude", "Claude", "product", "", 70, 0),
        keyword_parent_rollup.KeywordEntry("rec_opus4", "Opus 4", "model", "", 12, 0),
        keyword_parent_rollup.KeywordEntry("rec_claude_opus", "Claude Opus 4.7", "model", "", 1, 0),
    ]

    plans = keyword_parent_rollup.build_parent_plans(entries, generic_names=set())

    assert plans["rec_claude_opus"].parent_id == "rec_claude"


def test_parent_must_not_be_colder_than_child_when_counts_exist():
    entries = [
        keyword_parent_rollup.KeywordEntry("rec_opus4", "Opus 4", "model", "", 2, 0),
        keyword_parent_rollup.KeywordEntry("rec_claude_opus", "Claude Opus 4.7", "model", "", 10, 0),
    ]

    plans = keyword_parent_rollup.build_parent_plans(entries, generic_names=set())

    assert "rec_claude_opus" not in plans


def test_parent_match_requires_latin_token_boundary():
    entries = [
        keyword_parent_rollup.KeywordEntry("rec_chat", "Chat", "product", ""),
        keyword_parent_rollup.KeywordEntry("rec_chatgpt", "ChatGPT", "product", ""),
        keyword_parent_rollup.KeywordEntry("rec_pilot", "PiLoT", "product", ""),
        keyword_parent_rollup.KeywordEntry("rec_copilot", "GitHub Copilot", "product", ""),
        keyword_parent_rollup.KeywordEntry("rec_intel", "Intel", "org", ""),
        keyword_parent_rollup.KeywordEntry("rec_intellect", "Prime Intellect", "org", ""),
    ]

    plans = keyword_parent_rollup.build_parent_plans(entries, generic_names=set())

    assert "rec_chatgpt" not in plans
    assert "rec_copilot" not in plans
    assert "rec_intellect" not in plans


def test_risky_standalone_word_cannot_be_parent():
    entries = [
        keyword_parent_rollup.KeywordEntry("rec_square", "Square", "org", ""),
        keyword_parent_rollup.KeywordEntry("rec_pershing", "Pershing Square", "org", ""),
        keyword_parent_rollup.KeywordEntry("rec_world", "World", "org", ""),
        keyword_parent_rollup.KeywordEntry("rec_world_labs", "World Labs", "org", ""),
        keyword_parent_rollup.KeywordEntry("rec_skills", "Skills", "product", ""),
        keyword_parent_rollup.KeywordEntry("rec_admin_skills", "Admin Skills", "product", ""),
    ]

    plans = keyword_parent_rollup.build_parent_plans(entries, generic_names=set())

    assert "rec_pershing" not in plans
    assert "rec_world_labs" not in plans
    assert "rec_admin_skills" not in plans


def test_multi_word_parent_is_ignored_when_not_in_allowlist():
    entries = [
        keyword_parent_rollup.KeywordEntry("rec_claude_code", "Claude Code", "product", ""),
        keyword_parent_rollup.KeywordEntry("rec_claude_code_agent", "Claude Code Agent", "product", ""),
    ]

    plans = keyword_parent_rollup.build_parent_plans(entries, generic_names=set())

    assert "rec_claude_code_agent" not in plans


def test_short_allowlisted_model_family_can_be_parent():
    entries = [
        keyword_parent_rollup.KeywordEntry("rec_gpt", "GPT", "model", "", 10, 0),
        keyword_parent_rollup.KeywordEntry("rec_gpt55", "GPT-5.5", "model", "", 1, 0),
        keyword_parent_rollup.KeywordEntry("rec_glm", "GLM", "model", "", 10, 0),
        keyword_parent_rollup.KeywordEntry("rec_glm46", "GLM-4.6", "model", "", 1, 0),
        keyword_parent_rollup.KeywordEntry("rec_kimi", "Kimi", "product", "", 10, 0),
        keyword_parent_rollup.KeywordEntry("rec_kimi_k2", "Kimi K2", "model", "", 1, 0),
        keyword_parent_rollup.KeywordEntry("rec_chatgpt", "ChatGPT", "product", "", 10, 0),
        keyword_parent_rollup.KeywordEntry("rec_chatgpt55", "ChatGPT 5.5", "model", "", 1, 0),
    ]

    plans = keyword_parent_rollup.build_parent_plans(entries, generic_names=set())

    assert plans["rec_gpt55"].parent_id == "rec_gpt"
    assert plans["rec_glm46"].parent_id == "rec_glm"
    assert plans["rec_kimi_k2"].parent_id == "rec_kimi"
    assert plans["rec_chatgpt55"].parent_id == "rec_chatgpt"


def test_specific_product_parent_beats_short_model_family_parent():
    entries = [
        keyword_parent_rollup.KeywordEntry("rec_codex", "Codex", "product", "", 3, 0),
        keyword_parent_rollup.KeywordEntry("rec_gpt", "GPT", "model", "", 30, 0),
        keyword_parent_rollup.KeywordEntry("rec_codex_gpt", "Codex GPT5.5", "model", "", 1, 0),
    ]

    plans = keyword_parent_rollup.build_parent_plans(entries, generic_names=set())

    assert plans["rec_codex_gpt"].parent_id == "rec_codex"


def test_total_heat_formula_description_uses_child_self_heat_not_child_total():
    formula = keyword_parent_rollup.total_heat_formula("24h", "自身24h")

    assert "自身24h" in formula
    assert "子关键词.24h" not in formula


def test_news_24h_rollup_uses_news_only_self_formula(monkeypatch):
    monkeypatch.setattr(keyword_parent_rollup, "field_by_name", lambda tenant_token: {})

    report = keyword_parent_rollup.ensure_rollup_fields("token", apply=False)

    assert "自身NEWS24h" in report["created"]
    assert "NEWS24h" in report["created"]
    assert keyword_parent_rollup.self_news_heat_formula("自身NEWS24h") == "SUM(相关新闻.24h)"
    assert "相关过滤记录" not in keyword_parent_rollup.self_news_heat_formula("自身NEWS24h")


def test_news_24h_apply_creates_total_without_child_back_field(monkeypatch):
    created = []

    monkeypatch.setattr(
        keyword_parent_rollup,
        "field_by_name",
        lambda tenant_token: {
            "父关键词": {"field_id": "fld_parent"},
            "归属关键词": {"field_id": "fld_owner"},
            "自身NEWS24h": {
                "field_id": "fld_self_news_24h",
                "property": {"formula_expression": "SUM(相关新闻.24h)"},
            },
        },
    )

    def fake_create_field(*args, **kwargs):
        created.append({"name": args[3], "property": kwargs.get("field_property")})
        return True, {}

    monkeypatch.setattr(keyword_parent_rollup, "create_bitable_field", fake_create_field)
    monkeypatch.setattr(keyword_parent_rollup, "update_bitable_field", lambda *args, **kwargs: (True, {}))

    report = keyword_parent_rollup.ensure_rollup_fields("token", apply=True)

    news_24h = next(item for item in created if item["name"] == "NEWS24h")
    assert news_24h["property"]["formula_expression"] == "SUM(相关新闻.24h)"
    assert "NEWS24h" in report["created"]
    assert not [item for item in report["errors"] if item.get("field") == "NEWS24h"]


def test_parent_and_owner_fields_use_duplex_links(monkeypatch):
    created = []

    monkeypatch.setattr(keyword_parent_rollup, "field_by_name", lambda tenant_token: {})

    def fake_create_field(*args, **kwargs):
        created.append({"args": args, "kwargs": kwargs})
        return True, {}

    monkeypatch.setattr(keyword_parent_rollup, "create_bitable_field", fake_create_field)

    report = keyword_parent_rollup.ensure_rollup_fields("token", apply=True)

    parent_call = next(item for item in created if item["args"][3] == keyword_parent_rollup.PARENT_FIELD)
    assert parent_call["args"][3] == keyword_parent_rollup.PARENT_FIELD
    assert parent_call["args"][4] == keyword_parent_rollup.LINK_FIELD_TYPE
    assert parent_call["kwargs"]["field_property"]["back_field_name"] == keyword_parent_rollup.CHILD_FIELD
    assert parent_call["kwargs"]["field_property"]["multiple"] is False
    owner_call = next(item for item in created if item["args"][3] == keyword_parent_rollup.OWNER_FIELD)
    assert owner_call["args"][4] == keyword_parent_rollup.LINK_FIELD_TYPE
    assert owner_call["kwargs"]["field_property"]["back_field_name"] == keyword_parent_rollup.OWNER_CHILD_FIELD
    assert owner_call["kwargs"]["field_property"]["multiple"] is True
    assert keyword_parent_rollup.PARENT_FIELD in report["created"]
    assert keyword_parent_rollup.OWNER_FIELD in report["created"]


def test_apply_parent_plans_overwrites_and_clears_stale_links(monkeypatch):
    batches = []

    monkeypatch.setattr(
        keyword_parent_rollup,
        "fetch_current_parent_links",
        lambda tenant_token: {"rec_child": ["old_parent"], "rec_stale": ["old_parent"]},
    )

    def fake_batch_update(*args, **kwargs):
        batches.extend(args[3])
        return True, {}

    monkeypatch.setattr(keyword_parent_rollup, "batch_update_bitable_records", fake_batch_update)

    report = keyword_parent_rollup.apply_parent_plans(
        "token",
        [{"child_id": "rec_child", "parent_id": "new_parent"}],
    )

    assert report == {"updated": 2, "failed": 0, "failed_records": []}
    assert {"record_id": "rec_child", "fields": {keyword_parent_rollup.PARENT_FIELD: ["new_parent"]}} in batches
    assert {"record_id": "rec_stale", "fields": {keyword_parent_rollup.PARENT_FIELD: []}} in batches


def test_apply_parent_plans_retries_batch_failures_one_by_one(monkeypatch):
    calls = []

    monkeypatch.setattr(keyword_parent_rollup, "fetch_current_parent_links", lambda tenant_token: {})

    def fake_batch_update(*args, **kwargs):
        records = args[3]
        calls.append(records)
        if len(records) > 1:
            return False, {}
        return (records[0]["record_id"] != "rec_bad"), {}

    monkeypatch.setattr(keyword_parent_rollup, "batch_update_bitable_records", fake_batch_update)

    report = keyword_parent_rollup.apply_parent_plans(
        "token",
        [
            {"child_id": "rec_good", "parent_id": "rec_parent"},
            {"child_id": "rec_bad", "parent_id": "rec_parent"},
        ],
    )

    assert report == {"updated": 1, "failed": 1, "failed_records": ["rec_bad"]}


def test_keyword_entries_from_snapshot(tmp_path):
    snapshot = tmp_path / "keyword_snapshot.json"
    snapshot.write_text(
        """
{
  "schema_version": 1,
  "entries": [
    {
      "record_id": "rec_llama",
      "canonical_name": "Llama",
      "type": "model",
      "aliases": [],
      "news_count": 2,
      "filtered_count": 1,
      "note": ""
    }
  ]
}
""".strip(),
        encoding="utf-8",
    )

    entries = keyword_parent_rollup.keyword_entries_from_snapshot(snapshot)

    assert entries == [
        keyword_parent_rollup.KeywordEntry(
            record_id="rec_llama",
            name="Llama",
            type="model",
            note="",
            news_count=2,
            filtered_count=1,
        )
    ]


def test_fast_parent_plan_matches_slow_parent_plan_for_mixed_entries():
    entries = [
        keyword_parent_rollup.KeywordEntry("rec_claude", "Claude", "product", "", news_count=20),
        keyword_parent_rollup.KeywordEntry("rec_opus", "Opus", "model", "", news_count=15),
        keyword_parent_rollup.KeywordEntry("rec_child", "Claude Opus 4.7", "model", "", news_count=1),
        keyword_parent_rollup.KeywordEntry("rec_ali", "阿里", "org", "", news_count=10),
        keyword_parent_rollup.KeywordEntry("rec_aliyun", "阿里云", "org", "", news_count=1),
    ]

    fast = keyword_parent_rollup.build_parent_plans(entries, generic_names=set())
    slow = keyword_parent_rollup.build_parent_plans_slow(entries, generic_names=set())

    assert fast == slow
