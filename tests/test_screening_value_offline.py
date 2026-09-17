"""Offline contracts. Actual Grok functions; RSS functions AST-loaded unchanged.

The attachment omits RSS dependencies. These tests do NOT simulate the remote
services, browser, full RSS module initialization, or a real LLM decision.
"""
import ast
import copy
import datetime as dt
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import grok_watch as gw
from screening_value import finite_score, validate_candidates, validate_increment


def inc(kind="mechanism", source="self_report"):
    return {"kind": kind, "delta": "把观测与授权分开", "detail": "观测隔离；执行只读目标与授权",
            "source_status": source, "boundary": "作者所述，未在本地复现"}


def load_rss_functions():
    tree = ast.parse((ROOT / "rss_ingest.py").read_text(encoding="utf-8-sig"))
    names = {
        "normalize_string_list", "parse_score", "_validate_keywords", "_validate_categories",
        "validate_triage_result", "_triage_retry_prompt", "_screen_retry_prompt",
        "_content_prompt_with_triage", "validate_staged_content_result", "screen_fact_summary",
        "validate_screen_result", "validate_summary_result", "normalize_qa",
        "analyze_article_staged", "has_failed_categories", "attach_llm_meta",
        "get_llm_meta", "mark_analysis_provider", "build_failed_analysis", "build_prompt",
    }
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in selected} == names
    ns = dict(Any=Any, Dict=Dict, List=List, Optional=Optional, dt=dt,
              finite_score=finite_score, validate_increment=validate_increment,
              FAILED_CATEGORIES={"解析失败", "调用失败"}, log=lambda *a: None,
              limit_prompt_text=lambda x, limit: str(x or "")[:limit])
    ns["config"] = SimpleNamespace(NEWS_CATEGORY_OPTIONS={"AI工具与自动化", "AI实战教程"},
        KEYWORD_TYPE_OPTIONS={"product", "org", "topic"}, SCREEN_VALIDATE_RETRIES=2,
        ENABLE_TRIAGE_SCORE_GATE=True, TRIAGE_MIN_SCORE=3.8,
        PROMPT_TITLE_MAX_CHARS=300, PROMPT_CONTENT_MAX_CHARS=12000)
    exec(compile(ast.Module(body=selected, type_ignores=[]), "rss_ingest.py[AST subset]", "exec"), ns)
    return ns


@pytest.fixture
def rss():
    return load_rss_functions()


def screen(kind="mechanism", score=6.0):
    return {"action": "ingest", "increment": inc(kind), "categories": ["AI工具与自动化"],
            "score": score, "reason": "具体新增", "title_zh": "标题", "summary": "事实摘要",
            "keywords": [{"name": "SARA", "type": "product"}],
            "qa": [{"question": "机制是什么？", "answer": "观测不产生执行授权。"}]}


@pytest.mark.parametrize("value", [None, True, False, float("nan"), float("inf"),
                                   -1, 10.1, "NaN", "Infinity", "x"])
def test_invalid_scores_are_not_eligible(value):
    assert finite_score(value) is None


@pytest.mark.parametrize("score", [5.9, 6.0, 6.1, 8.7])
def test_score_is_not_rewritten(rss, score):
    assert rss["validate_staged_content_result"](screen(score=score), "keep")["score"] == score


@pytest.mark.parametrize("source", ["self_report", "secondhand", "opinion", "rumor", "unknown"])
def test_low_evidence_states_are_allowed(rss, source):
    payload = screen(); payload["increment"] = inc(source=source)
    assert rss["validate_staged_content_result"](payload, "keep")["action"] == "ingest"


def test_missing_increment_is_format_error_not_skip(rss):
    payload = screen(); payload.pop("increment")
    with pytest.raises(ValueError, match="invalid increment"):
        rss["validate_staged_content_result"](payload, "keep")


@pytest.mark.parametrize("verdict", ["keep", "uncertain"])
def test_none_cannot_be_rescued_by_nine_points(rss, verdict):
    out = rss["validate_staged_content_result"](screen(kind="none", score=9), verdict)
    assert out["action"] == "pass"
    assert out["increment"]["kind"] == "none"
    assert "低增量" in out["reason"]


def test_keep_can_be_vetoed_without_retry(rss):
    replies = iter([{"verdict": "keep", "score": 6.5, "evidence": "通用流程", "reason": "范围相关"},
                    screen(kind="none", score=6.6)])
    calls = []
    def llm(*args, **kwargs):
        calls.append(args)
        return next(replies)
    rss["analyze_with_provider_prompt"] = llm
    out = rss["analyze_article_staged"]({"title": "保函", "content": "提取→初稿→人审→留痕"},
                                      "triage", "screen", "mock", "mock")
    assert out["action"] == "pass"
    assert out["_llm_meta"]["llm_request_count"] == len(calls) == 2


def test_one_qa_is_enough(rss):
    assert len(rss["validate_summary_result"](screen())["qa"]) == 1
    assert len(rss["validate_staged_content_result"](screen(), "keep")["qa"]) == 1


@pytest.mark.parametrize("value", [None, True, "NaN", float("inf")])
def test_missing_and_nonfinite_ingest_score_fails_validation(rss, value):
    with pytest.raises(ValueError, match="(?:missing|invalid) score"):
        rss["validate_staged_content_result"](screen(score=value), "keep")


def test_prompt_does_not_present_generated_title_as_fact(rss):
    result = rss["build_prompt"]({"title": "十分钟签单", "content": "10分钟出方案 [Grok摘要]10分钟成交"}, "system")
    assert "不是独立事实" in result
    assert "旧格式从[Grok摘要]" in result
    assert "system" in result and "10分钟出方案" in result


def test_grok_rejects_none_even_at_five_and_accepts_low_heat():
    tweet = {"created_ms": 1000, "followers": 0, "views": 0}
    assert gw.hard_filter({"increment": inc("none"), "signal_score": 5}, tweet, 2000, 24) == "low_increment"
    assert gw.hard_filter({"increment": inc(), "category": "deal", "signal_score": 1}, tweet, 2000, 24) is None


def test_grok_requires_candidate_contract():
    for key in ("kind", "source_status"):
        payload = inc(); payload[key] = []
        with pytest.raises(ValueError, match="invalid increment"):
            validate_increment(payload)
    with pytest.raises(ValueError, match="increment"):
        validate_candidates([{"signal_score": 4}])
    for score in [True, None, float("nan"), 0, 6, 3.5]:
        with pytest.raises(ValueError, match="signal_score"):
            validate_candidates([{"increment": inc(), "signal_score": score}])


def test_grok_candidate_path_and_provenance(tmp_path):
    prompt_file = tmp_path / "topic.md"; prompt_file.write_text("topic rules")
    topic = {"key": "tips", "name": "教程", "prompt_file": str(prompt_file), "window_hours": 24}
    payload = [dict(title="通用流程", url="https://x.com/a/status/11", signal_score=5, increment=inc("none")),
               dict(title="新机制", url="https://x.com/b/status/12", signal_score=1, increment=inc(),
                    summary="Grok转述的解释", evidence="所读线程", evidence_strength="weak", time_confidence="confirmed")]
    prompts = []
    def search(prompt):
        prompts.append(prompt); return json.dumps(payload)
    def lookup(sid, author):
        return dict(text="原始正文"+sid, author=author, created_ms=1000, followers=0, likes=0, views=0)
    state = gw.default_state()
    result = gw.process_topic(topic, state, 2000, search, lookup, tmp_path / "feeds")
    assert result["accepted"] == 1 and result["dropped"] == {"low_increment": 1}
    assert prompts[0].startswith("topic rules") and "增量约束" in prompts[0]
    xml = ET.fromstring((tmp_path / "feeds" / "tips.xml").read_text(encoding="utf-8"))
    text = xml.find("./channel/item/description").text
    assert "[原帖正文] 原始正文12" in text
    assert "Grok所述依据" in text and "非独立证据" in text
    assert "evidence_strength: weak" in text and "time_confidence: confirmed" in text
    assert "[证据]" not in text


def test_six_point_write_gate_still_exists_and_none_no_longer_bypasses():
    source = (ROOT / "rss_ingest.py").read_text(encoding="utf-8-sig")
    assert "if score >= config.FEISHU_MIN_SCORE:" in source
    assert "score is None or score >= config.FEISHU_MIN_SCORE" not in source
    assert "invalid_score: finite 0-10 required for ingest" in source
    config = (ROOT / "config.py").read_text(encoding="utf-8-sig")
    assert 'FEISHU_MIN_SCORE = float(os.getenv("FEISHU_MIN_SCORE", "6.0"))' in config

@pytest.mark.parametrize("score,expected", [(5.9, False), (6.0, True), (6.1, True),
                                           (None, False), (True, False), (float("nan"), False)])
def test_actual_queue_write_branch_with_mocked_services(rss, score, expected):
    """Run the actual queue function, mocking services, not its score decision."""
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from urllib.parse import urlparse
    tree = ast.parse((ROOT / "rss_ingest.py").read_text(encoding="utf-8-sig"))
    names = {"run_llm_queue", "get_analysis_action", "analysis_provider_used"}
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    created, filtered, failures = [], [], []
    def remember(pool, *args):
        pool.append(args); failures.append(args)
    def create(*args, **kwargs):
        created.append(args[3]); return True, "mock-id"
    rss.update(threading=threading, ThreadPoolExecutor=ThreadPoolExecutor, as_completed=as_completed,
               urlparse=urlparse, sys=sys, KeywordRecord=object, ENABLE_TEXT_DEDUP=False,
               DedupCandidateStore=lambda: None,
               _analyze_with_llm_compat=lambda *a, **kw: screen(score=score),
               ensure_keyword_records=lambda *a, **kw: [],
               upload_article_images_for_attachment=lambda *a: [],
               build_news_fields=lambda article, analysis, *a, **kw: analysis,
               create_record_with_keyword_multiselect_fallback=create,
               build_filtered_analysis=lambda analysis, *a, **kw: analysis,
               record_filtered_outcome=lambda *a, **kw: filtered.append(a) or True,
               upsert_failed_item=remember, render_progress=lambda *a, **kw: "")
    for k,v in dict(LLM_CONCURRENCY=1, PROGRESS_BAR_WIDTH=10, FEISHU_MIN_SCORE=6.0,
                    FEISHU_APP_TOKEN="mock", FEISHU_NEWS_TABLE_ID="mock",
                    NEWS_FIELD_KEYWORDS="keywords", NEWS_FIELD_IMAGES="images", HTTP_TIMEOUT=1, HTTP_RETRIES=1).items():
        setattr(rss["config"], k, v)
    exec(compile(ast.Module(body=selected, type_ignores=[]), "rss_ingest.py[queue AST]", "exec"), rss)
    stats = dict(llm_success=0,llm_failed=0,entries_processed=0,entries_new=0,feishu_create_failed=0)
    state = {"s": {"updated_failed_items":[], "now_ms":1000, "new_count":0}}
    rss["run_llm_queue"]([{"source_id":"s", "item_key":"k", "entry_ts_ms":1,
                           "article":{"title":"t", "content":"c", "link":"https://example.test"}}],
                         state,"mock",set(),stats)
    assert bool(created) is expected
    invalid = finite_score(score) is None
    assert len(failures) == int(invalid)
    assert bool(filtered) is (not expected and not invalid)
    assert stats["entries_new"] == int(expected)
    if invalid:
        assert "invalid_score" in failures[0][-2]
