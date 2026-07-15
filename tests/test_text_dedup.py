import os
import sys
import json

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

import rss_ingest
from rss_ingest import (
    DedupCandidateStore,
    dedup_keywords_match,
    _plain_cell_text,
    load_dedup_alias_snapshot,
    load_dedup_store,
    keyword_lists_overlap,
    dedup_match_has_sparse_keyword_overlap,
    llm_dedup_check,
    split_keyword_text,
)


def test_plain_cell_text_string():
    assert _plain_cell_text("hello") == "hello"


def test_plain_cell_text_dict():
    assert _plain_cell_text({"text": "hello", "link": "http://x"}) == "hello"


def test_plain_cell_text_list_of_dicts():
    assert _plain_cell_text([{"text": "a"}, {"text": "b"}]) == "ab"


def test_plain_cell_text_list_of_strings():
    assert _plain_cell_text(["a", "b"]) == "ab"


def test_plain_cell_text_none():
    assert _plain_cell_text(None) == ""


def test_plain_cell_text_mixed_list():
    assert _plain_cell_text([{"text": "a"}, "b", {"name": "c"}]) == "abc"


def test_dedup_store_add_and_size():
    store = DedupCandidateStore()
    assert store.size() == 0
    store.add("key-1", "标题1", "摘要1", "OpenAI")
    assert store.size() == 1
    store.add("key-2", "标题2", "摘要2", "Google")
    assert store.size() == 2


def test_dedup_store_remove():
    store = DedupCandidateStore()
    store.add("key-1", "标题1", "摘要1", "OpenAI")
    store.add("key-2", "标题2", "摘要2", "Google")
    store.remove("key-1")
    assert store.size() == 1


def test_dedup_store_build_candidates_text():
    store = DedupCandidateStore()
    store.add("key-1", "OpenAI 发布 GPT-5", "上下文 200 万 token", "OpenAI, GPT-5")
    store.add("key-2", "Google 推出 Gemini 3", "多模态能力增强", "Google, Gemini")

    text = store.build_candidates_text()
    assert "OpenAI 发布 GPT-5" in text
    assert "Google 推出 Gemini 3" in text
    assert "摘要:" in text
    assert "关键词:" not in text


def test_dedup_store_filters_candidates_by_keyword_overlap():
    store = DedupCandidateStore()
    store.add("key-1", "OpenAI 发布 GPT-5", "上下文 200 万 token", "OpenAI, GPT-5")
    store.add("key-2", "Google 推出 Gemini 3", "多模态能力增强", "Google, Gemini")

    text = store.build_candidates_text(keywords=["GPT-5"])

    assert "OpenAI 发布 GPT-5" in text
    assert "Google 推出 Gemini 3" not in text


def test_dedup_store_does_not_use_record_links_when_keyword_text_misses():
    store = DedupCandidateStore()
    store.add(
        "key-1",
        "Google 发布 Gemini 更新",
        "旧文使用英文关键词",
        "Google, Gemini",
        keyword_record_ids=["reckw_google", "reckw_gemini"],
    )
    store.add(
        "key-2",
        "OpenAI 发布模型更新",
        "无关旧文",
        "OpenAI",
        keyword_record_ids=["reckw_openai"],
    )

    text = store.build_candidates_text(
        keywords=["谷歌"],
        keyword_record_ids=["reckw_google"],
    )

    assert text == ""
    assert "OpenAI 发布模型更新" not in text


def test_dedup_store_uses_own_keyword_text_before_expanded_record_links():
    store = DedupCandidateStore()
    store.add(
        "key-1",
        "父关键词下的无关旧文",
        "只有父关键词记录相同",
        "OpenAI",
        keyword_record_ids=["reckw_openai", "reckw_ai_parent"],
    )
    store.add(
        "key-2",
        "Claude Code 发布更新",
        "自身关键词相同",
        "Claude Code",
        keyword_record_ids=["reckw_claude_code", "reckw_ai_parent"],
    )

    text = store.build_candidates_text(
        keywords=["Claude Code"],
        keyword_record_ids=["reckw_claude_code", "reckw_ai_parent"],
    )

    assert "Claude Code 发布更新" in text
    assert "父关键词下的无关旧文" not in text


def test_dedup_store_falls_back_to_keyword_text_without_keyword_records():
    store = DedupCandidateStore()
    store.add("key-1", "OpenAI 发布 GPT-5", "", "OpenAI, GPT-5")
    store.add("key-2", "Google 推出 Gemini 3", "", "Google, Gemini")

    text = store.build_candidates_text(
        keywords=["GPT-5"],
        keyword_record_ids=["reckw_gpt5"],
    )

    assert "OpenAI 发布 GPT-5" in text
    assert "Google 推出 Gemini 3" not in text


def test_dedup_store_filters_candidates_by_alias_snapshot(tmp_path):
    alias_path = tmp_path / "dedup-alias-groups.json"
    alias_path.write_text(
        json.dumps(
            [
                {
                    "group_id": "alias::org::nvidia",
                    "type": "org",
                    "names": ["NVIDIA", "Nvidia", "英伟达"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    snapshot = load_dedup_alias_snapshot(str(alias_path))
    store = DedupCandidateStore(alias_snapshot=snapshot)
    store.add("key-1", "NVIDIA 发布新芯片", "", "NVIDIA")
    store.add("key-2", "Google 推出 Gemini 3", "", "Google, Gemini")

    text = store.build_candidates_text(keywords=["英伟达"])

    assert "NVIDIA 发布新芯片" in text
    assert "Google 推出 Gemini 3" not in text


def test_dedup_store_alias_snapshot_checks_each_candidate_independently(tmp_path):
    alias_path = tmp_path / "dedup-alias-groups.json"
    alias_path.write_text(
        json.dumps(
            [
                {
                    "group_id": "alias::org::nvidia",
                    "names": ["NVIDIA", "英伟达"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    snapshot = load_dedup_alias_snapshot(str(alias_path))
    store = DedupCandidateStore(alias_snapshot=snapshot)
    store.add("key-1", "Blackwell 架构更新", "", "Blackwell")
    store.add("key-2", "NVIDIA 发布 B200", "", "NVIDIA, B200")

    text = store.build_candidates_text(keywords=["英伟达", "Blackwell"])

    assert "Blackwell 架构更新" in text
    assert "NVIDIA 发布 B200" in text


def test_load_dedup_alias_snapshot_supports_canonical_and_aliases(tmp_path):
    alias_path = tmp_path / "dedup-alias-groups.json"
    alias_path.write_text(
        json.dumps(
            {
                "version": "v1",
                "groups": [
                    {
                        "canonical_id": "alias::org::nvidia",
                        "canonical": "NVIDIA",
                        "aliases": ["英伟达", "Nvidia"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    snapshot = load_dedup_alias_snapshot(str(alias_path))

    assert snapshot.version == "v1"
    assert keyword_lists_overlap(["英伟达"], ["NVIDIA"], alias_snapshot=snapshot)


def test_load_dedup_store_reads_keyword_record_ids_for_legacy_no_keyword_query(monkeypatch):
    monkeypatch.setattr(rss_ingest, "ENABLE_TEXT_DEDUP", True)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_NEWS_TABLE_ID", "tbl_news")
    monkeypatch.setattr(rss_ingest, "load_dedup_alias_snapshot", lambda: None)
    monkeypatch.setattr(
        rss_ingest,
        "list_bitable_records",
        lambda *args, **kwargs: [
            {
                "fields": {
                    rss_ingest.config.NEWS_FIELD_PUBLISHED_MS: 4102444800000,
                    rss_ingest.config.NEWS_FIELD_TITLE: "Google 发布 Gemini 更新",
                    rss_ingest.config.NEWS_FIELD_BRIEF_SUMMARY: "旧文摘要",
                    rss_ingest.config.NEWS_FIELD_ITEM_KEY: "old-google",
                    rss_ingest.config.NEWS_FIELD_KEYWORDS: ["Google"],
                    rss_ingest.config.NEWS_FIELD_KEYWORD_RECORDS: {
                        "link_record_ids": ["reckw_google", "reckw_gemini"],
                    },
                }
            }
        ],
    )

    store = load_dedup_store("tenant")

    text = store.build_candidates_text(
        keywords=None,
        keyword_record_ids=["reckw_google"],
    )
    assert "Google 发布 Gemini 更新" in text
    assert "reckw_google" not in text


def test_split_keyword_text_reads_slash_separated_display_keywords():
    assert split_keyword_text("OpenAI / GPT-5 / ChatGPT") == ["OpenAI", "GPT-5", "ChatGPT"]
    assert split_keyword_text("Google / Google I/O 2026") == ["Google", "Google I/O 2026"]
    assert split_keyword_text("Vercel AI SDK / Node.js 22 / @ai-sdk/provider") == [
        "Vercel AI SDK",
        "Node.js 22",
        "@ai-sdk/provider",
    ]


def test_split_keyword_text_reads_feishu_text_runs():
    assert split_keyword_text([{"text": "Google / Google I/O 2026", "type": "text"}]) == [
        "Google",
        "Google I/O 2026",
    ]


def test_load_dedup_store_limits_news_prefetch_pages(monkeypatch):
    monkeypatch.setattr(rss_ingest, "ENABLE_TEXT_DEDUP", True)
    monkeypatch.setattr(rss_ingest, "TEXT_DEDUP_PREFETCH_MAX_PAGES", 2)
    monkeypatch.setattr(rss_ingest.config, "FEISHU_NEWS_TABLE_ID", "tbl_news")
    monkeypatch.setattr(rss_ingest, "load_dedup_alias_snapshot", lambda: None)
    captured = {}

    def fake_list_bitable_records(*args, **kwargs):
        captured["page_size"] = kwargs.get("page_size")
        captured["max_pages"] = kwargs.get("max_pages")
        captured["allow_partial"] = kwargs.get("allow_partial")
        return []

    monkeypatch.setattr(rss_ingest, "list_bitable_records", fake_list_bitable_records)

    store = load_dedup_store("tenant")

    assert store.size() == 0
    assert captured == {"page_size": 500, "max_pages": 2, "allow_partial": True}


def test_dedup_store_does_not_alias_without_snapshot():
    store = DedupCandidateStore()
    store.add("key-1", "NVIDIA 发布新芯片", "", "NVIDIA")

    assert store.build_candidates_text(keywords=["英伟达"]) == ""


def test_dedup_store_keyword_filter_supports_containment():
    store = DedupCandidateStore()
    store.add("key-1", "OpenAI 推出 Codex Chrome 扩展", "", "Codex, Chrome扩展")
    store.add("key-2", "Google 推出 Gemini 3", "", "Google, Gemini")

    text = store.build_candidates_text(keywords=["Codex for Chrome"])

    assert "OpenAI 推出 Codex Chrome 扩展" in text
    assert "Google 推出 Gemini 3" not in text


def test_dedup_store_keyword_filter_empty_keywords_returns_no_candidates():
    store = DedupCandidateStore()
    store.add("key-1", "OpenAI 发布 GPT-5", "", "OpenAI")

    assert store.build_candidates_text(keywords=[]) == ""


def test_dedup_store_excludes_item_key():
    store = DedupCandidateStore()
    store.add("key-1", "标题1", "", "")
    store.add("key-2", "标题2", "", "")

    text = store.build_candidates_text(exclude_item_key="key-1")
    assert "标题1" not in text
    assert "标题2" in text


def test_dedup_store_build_candidates_context_maps_candidate_ids():
    store = DedupCandidateStore()
    store.add("key-1", "旧标题", "旧摘要", "OpenAI")

    text, candidate_by_id = store.build_candidates_context(keywords=["OpenAI"])

    assert "C1: 旧标题" in text
    assert candidate_by_id["C1"].title == "旧标题"
    assert candidate_by_id["C1"].summary == "旧摘要"


def test_llm_dedup_check_returns_none_on_empty_candidates():
    result = llm_dedup_check("标题", "摘要", "关键词", "")
    assert result is None


def test_dedup_keywords_match_exact_and_containment():
    assert dedup_keywords_match("Codex for Chrome", "Codex")
    assert dedup_keywords_match("自我复制", "AI自我复制")
    assert not dedup_keywords_match("OpenAI", "Google")


def test_keyword_lists_overlap():
    assert keyword_lists_overlap(["Codex for Chrome"], ["Codex", "Chrome扩展"])
    assert not keyword_lists_overlap(["Vapi"], ["OpenAI"])


def test_dedup_match_has_sparse_keyword_overlap_blocks_platform_mismatch():
    assert dedup_match_has_sparse_keyword_overlap(
        ["Grok", "OpenClaw", "AI代理集成"],
        ["xAI", "Grok", "Hermes Agent"],
    )
    assert not dedup_match_has_sparse_keyword_overlap(
        ["Grok", "OpenClaw", "AI代理集成"],
        ["Grok", "OpenClaw"],
    )


def test_llm_dedup_check_ignores_contradictory_duplicate(monkeypatch):
    monkeypatch.setattr(rss_ingest, "_load_dedup_prompt", lambda: "prompt")
    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_provider_prompt",
        lambda *args, **kwargs: {
            "is_duplicate": True,
            "matched_id": "C1",
            "matched_title": "旧文章",
            "shared_facts": [],
            "reason": "新文章与C1无关，因此不重复。",
        },
    )

    result = llm_dedup_check("新文章", "摘要", "关键词", "C1: 旧文章")

    assert result is None


def test_llm_dedup_check_ignores_caveated_duplicate(monkeypatch):
    monkeypatch.setattr(rss_ingest, "_load_dedup_prompt", lambda: "prompt")
    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_provider_prompt",
        lambda *args, **kwargs: {
            "is_duplicate": True,
            "matched_id": "C1",
            "matched_title": "旧文章",
            "shared_facts": ["同一会议", "同一产品线"],
            "reason": "虽功能不同，但同属同一产品线。",
        },
    )

    result = llm_dedup_check("新文章", "摘要", "关键词", "C1: 旧文章")

    assert result is None


def test_llm_dedup_check_ignores_invalid_matched_id(monkeypatch):
    monkeypatch.setattr(rss_ingest, "_load_dedup_prompt", lambda: "prompt")
    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_provider_prompt",
        lambda *args, **kwargs: {
            "is_duplicate": True,
            "matched_id": "old-1",
            "reason": "同一主体同一事件。",
        },
    )

    result = llm_dedup_check("新文章", "摘要", "关键词", "C1: 旧文章")

    assert result is None


def test_llm_dedup_check_ignores_duplicate_without_two_shared_facts(monkeypatch):
    monkeypatch.setattr(rss_ingest, "_load_dedup_prompt", lambda: "prompt")
    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_provider_prompt",
        lambda *args, **kwargs: {
            "is_duplicate": True,
            "matched_id": "C1",
            "matched_title": "旧文章",
            "shared_facts": ["同一公司"],
            "reason": "主体相同，属于同一新闻。",
        },
    )

    result = llm_dedup_check("新文章", "摘要", "关键词", "C1: 旧文章")

    assert result is None


def test_llm_dedup_check_keeps_non_contradictory_duplicate(monkeypatch):
    monkeypatch.setattr(rss_ingest, "_load_dedup_prompt", lambda: "prompt")
    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_provider_prompt",
        lambda *args, **kwargs: {
            "is_duplicate": True,
            "matched_id": "C1",
            "matched_title": "旧文章",
            "shared_facts": ["同一产品", "同一融资金额"],
            "reason": "同一主体同一融资事件。",
        },
    )

    result = llm_dedup_check("新文章", "摘要", "关键词", "C1: 旧文章")

    assert result == {
        "matched_id": "C1",
        "matched_title": "旧文章",
        "shared_facts": ["同一产品", "同一融资金额"],
        "reason": "同一主体同一融资事件。",
    }


def test_llm_dedup_check_defaults_to_deepseek_provider(monkeypatch):
    captured = {}
    monkeypatch.setattr(rss_ingest, "_load_dedup_prompt", lambda: "prompt")
    monkeypatch.setattr(rss_ingest.config, "LLM_PROVIDER", "gemini", raising=False)
    monkeypatch.setattr(rss_ingest.config, "TEXT_DEDUP_PROVIDER", "deepseek", raising=False)
    monkeypatch.setattr(rss_ingest.config, "DEEPSEEK_MODEL", "deepseek-chat", raising=False)

    def fake_analyze(article, provider, system_prompt, model_name, **kwargs):
        captured["provider"] = provider
        captured["model"] = model_name
        captured["suppress_notify"] = kwargs.get("suppress_notify")
        return {"is_duplicate": False}

    monkeypatch.setattr(rss_ingest, "analyze_with_provider_prompt", fake_analyze)

    result = llm_dedup_check("新文章", "摘要", "关键词", "C1: 旧文章")

    assert result is None
    assert captured == {"provider": "deepseek", "model": "deepseek-chat", "suppress_notify": True}


def test_llm_dedup_check_suppresses_failed_analysis_notifications(monkeypatch):
    captured = {}
    audited = {}
    monkeypatch.setattr(rss_ingest, "_load_dedup_prompt", lambda: "prompt")
    monkeypatch.setattr(rss_ingest.config, "TEXT_DEDUP_PROVIDER", "ark", raising=False)
    monkeypatch.setattr(rss_ingest.config, "ARK_MODEL", "deepseek-v4-flash", raising=False)

    def fake_analyze(article, provider, system_prompt, model_name, **kwargs):
        captured["provider"] = provider
        captured["model"] = model_name
        captured["suppress_notify"] = kwargs.get("suppress_notify")
        return {"categories": ["调用失败"], "summary": "empty json"}

    def fake_audit(provider, title, reason):
        audited["provider"] = provider
        audited["title"] = title
        audited["reason"] = reason

    monkeypatch.setattr(rss_ingest, "analyze_with_provider_prompt", fake_analyze)
    monkeypatch.setattr(rss_ingest, "audit_text_dedup_failure", fake_audit)

    result = llm_dedup_check("新文章", "摘要", "关键词", "C1: 旧文章")

    assert result is None
    assert captured == {"provider": "ark", "model": "deepseek-v4-flash", "suppress_notify": True}
    assert audited == {"provider": "ark", "title": "新文章", "reason": "empty json"}


def test_llm_dedup_check_does_not_send_keywords_to_llm(monkeypatch):
    captured = {}
    monkeypatch.setattr(rss_ingest, "_load_dedup_prompt", lambda: "prompt")

    def fake_analyze(article, *args, **kwargs):
        captured["content"] = article["content"]
        return {"is_duplicate": False}

    monkeypatch.setattr(rss_ingest, "analyze_with_provider_prompt", fake_analyze)

    llm_dedup_check(
        "新文章",
        "摘要",
        "OpenAI, GPT-5",
        "C1: 旧文章\n  摘要: 旧摘要\n  关键词: OpenAI, GPT-5",
    )

    assert "关键词:" not in captured["content"]
    assert "OpenAI, GPT-5" not in captured["content"]


def test_dedup_store_empty_summary_and_keywords():
    store = DedupCandidateStore()
    store.add("key-1", "只有标题", "", "")
    text = store.build_candidates_text()
    assert "只有标题" in text
    assert "摘要:" not in text
    assert "关键词:" not in text
