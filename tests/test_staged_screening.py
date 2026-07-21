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


def test_staged_keep_cannot_be_overridden(monkeypatch):
    calls = []
    responses = [
        {"verdict": "keep", "evidence": "工具能力变化", "reason": "明确命中"},
        {
            "action": "pass",
            "reason": "内容太薄",
            "title_zh": "测试标题",
            "summary": "测试摘要",
            "keywords": [],
        },
        {
            "action": "ingest",
            "categories": ["AI工具与自动化"],
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
        {"verdict": "uncertain", "evidence": "只公布融资金额", "reason": "可能只是资本信号"},
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
    assert result["_llm_meta"]["llm_request_count"] == 2


def test_staged_filter_stops_after_triage(monkeypatch):
    calls = []

    def fake_analyze(*args, **kwargs):
        calls.append(args[2])
        return {"verdict": "filter", "evidence": "只有一句感谢", "reason": "真正空内容"}

    monkeypatch.setattr(rss_ingest, "analyze_with_provider_prompt", fake_analyze)
    result = rss_ingest.analyze_article(ARTICLE, _prompts(), provider="deepseek", include_summary=False)

    assert result["action"] == "pass"
    assert result["_llm_meta"]["triage_verdict"] == "filter"
    assert result["_llm_meta"]["llm_request_count"] == 1
    assert calls == ["triage prompt"]
