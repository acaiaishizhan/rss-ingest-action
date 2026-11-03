import json

import pytest

import rss_ingest


ARTICLE = {
    "title": "测试标题",
    "content": "测试正文",
    "link": "https://example.com/test",
    "source": "test",
}


def _prompts():
    return {
        "keyword_blocklist": [],
        "triage_prompt": "triage prompt",
        "screen_prompt": "content prompt",
        "summarize_prompt": "fallback prompt",
    }


def _qa():
    return [
        {"question": "问题1", "answer": "回答1"},
        {"question": "问题2", "answer": "回答2"},
        {"question": "问题3", "answer": "回答3"},
    ]


def _content(qa_count=3):
    return {
        "action": "ingest",
        "categories": ["教程方法"],
        "score": 8.0,
        "reason": "模拟保留理由",
        "title_zh": "模拟标题",
        "summary": "模拟摘要，不代表生产正文",
        "keywords": [{"name": "工具A", "type": "product"}],
        "qa": _qa()[:qa_count],
    }


@pytest.mark.parametrize("qa_count", [1, 2])
@pytest.mark.parametrize("verdict", ["keep", "uncertain"])
@pytest.mark.parametrize("retries", [1, 3])
def test_staged_short_qa_filters_only_after_existing_retries(monkeypatch, qa_count, verdict, retries):
    calls = []
    responses = [
        {"verdict": verdict, "score": 6.2, "evidence": "模拟证据", "reason": "模拟初筛"},
        *[_content(qa_count) for _ in range(retries)],
    ]

    def fake_analyze(article, provider, system_prompt, model_name, **kwargs):
        calls.append(system_prompt)
        return responses.pop(0)

    monkeypatch.setattr(rss_ingest.config, "SCREEN_VALIDATE_RETRIES", retries)
    monkeypatch.setattr(rss_ingest, "analyze_with_provider_prompt", fake_analyze)
    result = rss_ingest.analyze_article(ARTICLE, _prompts(), provider="ark", include_summary=False)

    assert not responses
    assert len(calls) == retries + 1
    assert all("qa must contain at least 3 items" in prompt for prompt in calls[2:])
    assert result["action"] == "pass"
    assert not rss_ingest.has_failed_categories(result)
    assert result["_llm_meta"]["llm_request_count"] == retries + 1
    assert result["_llm_meta"]["triage_verdict"] == verdict
    fields = rss_ingest.build_filtered_fields(ARTICLE, result, "short-qa")
    assert fields[rss_ingest.config.FILTERED_FIELD_FILTER_METHOD] == "终审过滤"
    reason = fields[rss_ingest.config.FILTERED_FIELD_FILTER_REASON]
    assert f"{retries} 次" in reason
    assert f"最后输出 {qa_count} 组" in reason
    assert "qa must contain at least 3 items" in reason


@pytest.mark.parametrize("field,value,error", [
    ("action", "invalid", "invalid action"),
    ("action", "pass", "triage keep cannot be overridden"),
    ("reason", "", "missing reason"),
    ("title_zh", "", "missing title_zh"),
    ("summary", "", "missing summary"),
    ("keywords", [], "keywords count out of range"),
    ("categories", [], "missing categories"),
    ("categories", ["invalid"], "invalid categories"),
    ("score", None, "missing score"),
    ("score", 11, "invalid score"),
])
def test_staged_short_qa_does_not_hide_other_validation_errors(monkeypatch, field, value, error):
    invalid = {**_content(1), field: value}
    responses = [
        {"verdict": "keep", "score": 6.2, "evidence": "模拟证据", "reason": "模拟初筛"},
        _content(1),
        invalid,
        invalid,
    ]
    monkeypatch.setattr(rss_ingest.config, "SCREEN_VALIDATE_RETRIES", 3)
    monkeypatch.setattr(rss_ingest, "analyze_with_provider_prompt", lambda *a, **k: responses.pop(0))

    result = rss_ingest.analyze_article(ARTICLE, _prompts(), provider="ark", include_summary=False)

    assert not responses
    assert rss_ingest.has_failed_categories(result)
    assert result["summary"].startswith(f"content: {error}")
    assert rss_ingest.get_analysis_action(result) != "pass"
    assert result["_llm_meta"]["llm_request_count"] == 4


@pytest.mark.parametrize("scenario", [
    "one_qa", "two_qa", "normal", "retry_one_success", "retry_two_success",
    "other_validation", "api_failure", "json_failure", "filtered_write_failure",
])
def test_short_qa_single_item_run_uses_existing_outcomes(monkeypatch, capsys, scenario):
    # Exercise real staged validation, queue handling, filtered persistence and run exit.
    # Only external reads/writes and provider responses are replaced by local fakes.
    short_count = 2 if scenario in {"two_qa", "retry_two_success"} else 1
    content_responses = [_content(short_count) for _ in range(3)]
    if scenario == "normal":
        content_responses = [_content()]
    elif scenario in {"retry_one_success", "retry_two_success"}:
        content_responses = [_content(short_count), _content()]
    elif scenario == "other_validation":
        content_responses = [{**_content(1), "score": None} for _ in range(3)]
    elif scenario in {"api_failure", "json_failure"}:
        reason = "timeout" if scenario == "api_failure" else "invalid JSON"
        content_responses = [_content(1), rss_ingest.build_failed_analysis(reason)]
    responses = [
        {"verdict": "keep", "score": 6.2, "evidence": "模拟证据", "reason": "模拟初筛"},
        *content_responses,
    ]
    for name, value in {
        "FEISHU_APP_ID": "app", "FEISHU_APP_SECRET": "test-secret",
        "FEISHU_APP_TOKEN": "app-token", "FEISHU_NEWS_TABLE_ID": "news",
        "FEISHU_RSS_TABLE_ID": "rss", "FEISHU_FILTERED_TABLE_ID": "filtered",
        "FEISHU_KEYWORD_TABLE_ID": "", "ENABLE_SECONDARY_SYNC": False,
        "SCREEN_VALIDATE_RETRIES": 3, "LLM_CONCURRENCY": 1, "LLM_PROVIDER": "ark",
        "RSS_SOURCE_OVERRIDE_FILE": "", "RSS_SOURCE_MODE": "all", "FEISHU_MIN_SCORE": 6.0,
    }.items():
        monkeypatch.setattr(rss_ingest.config, name, value)
    monkeypatch.delenv("SOPILOT_BATCH_ID", raising=False)
    monkeypatch.delenv("RSS_DURABLE_WRITES", raising=False)
    monkeypatch.setenv("RSS_INGEST_LANE", "rss")
    monkeypatch.setattr(rss_ingest, "ENABLE_TEXT_DEDUP", False)
    monkeypatch.setattr(rss_ingest, "analyze_with_provider_prompt", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr(rss_ingest, "get_tenant_access_token", lambda *a: "tenant")
    monkeypatch.setattr(rss_ingest, "load_local_prompt_sections", _prompts)
    monkeypatch.setattr(rss_ingest, "list_bitable_records", lambda *a, **k: [])
    existing_keys = set()
    monkeypatch.setattr(rss_ingest, "prefetch_recent_item_keys_with_retries", lambda *a: existing_keys)
    monkeypatch.setattr(rss_ingest, "ensure_keyword_records", lambda *a, **k: [])
    monkeypatch.setattr(rss_ingest, "upload_article_images_for_attachment", lambda *a, **k: [])

    now_ms = 1_800_000_000_000
    failed_items = []
    rss_ingest.upsert_failed_item(failed_items, "short-qa", now_ms - 1000,
                                 ARTICLE["title"], ARTICLE["link"], "old failure", now_ms - 1000)
    state = {
        "source": {"record_id": "source-1", "name": "fixture"},
        "updated_failed_items": failed_items, "retired_failed_items": [],
        "now_ms": now_ms, "new_count": 0, "latest_pub_ms": 0, "latest_key": "",
    }
    queue = [{"source_id": "source-1", "item_key": "short-qa", "entry_ts_ms": now_ms - 1000,
              "article": dict(ARTICLE), "from_failed": True}]
    monkeypatch.setattr(rss_ingest, "split_sources_and_queue", lambda *a, **k: (
        queue, {"source-1": state}, {"queue_total": 1, "sources_processed": 1,
                                   "sources_skipped": 0, "sources_failed": 0, "entries_fetched": 1},
    ))
    updates = []
    monkeypatch.setattr(rss_ingest, "update_source_record_fields",
                        lambda token, source_id, fields: updates.append(fields) or True)
    created = []

    def fake_create(app, table, token, fields, *args, **kwargs):
        created.append((table, fields))
        return (False, None) if scenario == "filtered_write_failure" else (True, "record-1")

    monkeypatch.setattr(rss_ingest, "create_bitable_record_with_id", fake_create)
    code = rss_ingest.main()
    output = capsys.readouterr().out
    assert not responses
    failed = scenario in {"other_validation", "api_failure", "json_failure"}
    assert code == (1 if failed else 0)
    assert f"llm_failed={int(failed)}" in output
    assert len(updates) == 1
    saved_failures = json.loads(updates[0][rss_ingest.config.RSS_FIELD_FAILED_ITEMS])
    if failed or scenario == "filtered_write_failure":
        assert len(saved_failures) == 1
        assert saved_failures[0]["fail_count"] == 2
        assert "short-qa" not in existing_keys
        if scenario == "filtered_write_failure":
            assert saved_failures[0]["last_error"] == "filtered_create_failed"
        else:
            assert not created
            expected = {"other_validation": "content: missing score",
                        "api_failure": "timeout", "json_failure": "invalid JSON"}[scenario]
            assert saved_failures[0]["last_error"] == expected
    else:
        assert failed_items == []
        assert saved_failures == []
        assert "short-qa" in existing_keys
    if not failed:
        if scenario == "filtered_write_failure":
            # Existing optional-field write fallback still runs before saving the failure.
            assert len(created) >= 1
            assert all(table == "filtered" for table, fields in created)
        else:
            assert len(created) == 1
        table, fields = created[0]
        if scenario in {"normal", "retry_one_success", "retry_two_success"}:
            assert table == "news"
            assert fields[rss_ingest.config.NEWS_FIELD_SUMMARY] == rss_ingest.build_summary(qa=_qa())
            assert "written=1" in output
        else:
            assert table == "filtered"
            assert fields[rss_ingest.config.FILTERED_FIELD_FILTER_METHOD] == "终审过滤"
            assert "qa must contain at least 3 items" in fields[rss_ingest.config.FILTERED_FIELD_FILTER_REASON]
            assert "llm_filtered=1" in output


def test_staged_keep_cannot_be_overridden(monkeypatch):
    calls = []
    responses = [
        {"verdict": "keep", "score": 6.2, "evidence": "工具能力变化", "reason": "明确命中"},
        {
            "action": "pass",
            "reason": "内容太薄",
            "title_zh": "测试标题",
            "summary": "测试摘要",
            "keywords": [],
        },
        {
            "action": "ingest",
            "categories": ["教程方法"],
            "score": 4.5,
            "reason": "初筛 keep 不可推翻",
            "title_zh": "测试标题",
            "summary": "测试摘要",
            "keywords": [{"name": "工具A", "type": "product"}],
            "qa": _qa(),
        },
    ]

    def fake_analyze(article, provider, system_prompt, model_name, **kwargs):
        calls.append(system_prompt)
        return responses.pop(0)

    monkeypatch.setattr(rss_ingest, "analyze_with_provider_prompt", fake_analyze)
    result = rss_ingest.analyze_article(ARTICLE, _prompts(), provider="deepseek", include_summary=False)

    assert result["action"] == "ingest"
    assert result["score"] == 4.5
    assert result["_llm_meta"]["triage_verdict"] == "keep"
    assert result["_llm_meta"]["llm_request_count"] == 3
    assert "initial_verdict: keep" in calls[1]
    assert "triage keep cannot be overridden" in calls[2]


def test_staged_uncertain_can_pass(monkeypatch):
    responses = [
        {"verdict": "uncertain", "score": 4.0, "evidence": "只公布融资金额", "reason": "可能只是资本信号"},
        {
            "action": "pass",
            "reason": "纯资本/治理/规模，未命中六类救回",
            "title_zh": "某AI公司完成融资",
            "summary": "某AI公司完成新一轮融资。",
            "keywords": [{"name": "某AI公司", "type": "org"}],
        },
    ]
    monkeypatch.setattr(
        rss_ingest,
        "analyze_with_provider_prompt",
        lambda *args, **kwargs: responses.pop(0),
    )
    result = rss_ingest.analyze_article(ARTICLE, _prompts(), provider="deepseek", include_summary=False)

    assert result["action"] == "pass"
    assert result["_llm_meta"]["triage_verdict"] == "uncertain"
    assert result["_llm_meta"]["filter_method"] == "终审过滤"
    assert result["_llm_meta"]["llm_request_count"] == 2
    fields = rss_ingest.build_filtered_fields(ARTICLE, result, "k1")
    assert fields[rss_ingest.config.FILTERED_FIELD_FILTER_METHOD] == "终审过滤"
    assert fields[rss_ingest.config.FILTERED_FIELD_FILTER_REASON] == "纯资本/治理/规模，未命中六类救回"


def test_staged_filter_stops_after_triage(monkeypatch):
    calls = []

    def fake_analyze(*args, **kwargs):
        calls.append(args[2])
        return {"verdict": "filter", "score": 1.0, "evidence": "只有一句感谢", "reason": "真正空内容"}

    monkeypatch.setattr(rss_ingest, "analyze_with_provider_prompt", fake_analyze)
    result = rss_ingest.analyze_article(ARTICLE, _prompts(), provider="deepseek", include_summary=False)

    assert result["action"] == "pass"
    assert result["_llm_meta"]["triage_verdict"] == "filter"
    assert result["_llm_meta"]["llm_request_count"] == 1
    assert calls == ["triage prompt"]


def test_staged_low_triage_score_stops_before_content(monkeypatch):
    calls = []

    def fake_analyze(*args, **kwargs):
        calls.append(args[2])
        return {
            "verdict": "uncertain",
            "score": 3.7,
            "evidence": "只有活动议题",
            "reason": "评分停在 S2 噪音档",
        }

    monkeypatch.setattr(rss_ingest, "analyze_with_provider_prompt", fake_analyze)
    monkeypatch.setattr(rss_ingest.config, "ENABLE_TRIAGE_SCORE_GATE", True, raising=False)
    monkeypatch.setattr(rss_ingest.config, "TRIAGE_MIN_SCORE", 3.8, raising=False)
    result = rss_ingest.analyze_article(ARTICLE, _prompts(), provider="deepseek", include_summary=False)

    assert result["action"] == "pass"
    assert result["score"] == 3.7
    assert result["_llm_meta"]["triage_verdict"] == "uncertain"
    assert result["_llm_meta"]["triage_score"] == 3.7
    assert result["_llm_meta"]["low_score_filtered"] is True
    assert result["_llm_meta"]["filter_method"] == "初筛低分"
    assert result["_llm_meta"]["llm_request_count"] == 1
    assert calls == ["triage prompt"]
