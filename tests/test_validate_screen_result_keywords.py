import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

from rss_ingest import validate_screen_result  # noqa: E402


def _ingest_payload(**overrides):
    payload = {
        "action": "ingest",
        "reason": "保留原因",
        "categories": ["AI前沿资讯"],
        "score": 8.0,
        "keywords": [{"name": "OpenAI", "type": "org"}],
        "title_zh": "OpenAI 发布新模型",
        "summary": "OpenAI 发布新模型。",
    }
    payload.update(overrides)
    return payload


def _pass_payload(**overrides):
    payload = {
        "action": "pass",
        "reason": "命中规则2：通稿。",
        "title_zh": "某公司发布新产品",
        "summary": "某公司宣布发布新产品，但缺少实质信息。",
        "keywords": [{"name": "某公司", "type": "org"}],
    }
    payload.update(overrides)
    return payload


def test_keywords_missing_raises():
    payload = _ingest_payload()
    payload.pop("keywords")
    with pytest.raises(ValueError, match="missing keywords"):
        validate_screen_result(payload)


def test_keywords_not_list_raises():
    payload = _ingest_payload(keywords="OpenAI")
    with pytest.raises(ValueError, match="keywords must be list"):
        validate_screen_result(payload)


def test_keywords_empty_list_raises():
    payload = _ingest_payload(keywords=[])
    with pytest.raises(ValueError, match="keywords count out of range"):
        validate_screen_result(payload)


def test_pass_allows_empty_keywords():
    result = validate_screen_result(_pass_payload(keywords=[]))
    assert result == {
        "action": "pass",
        "reason": "命中规则2：通稿。",
        "title_zh": "某公司发布新产品",
        "summary": "某公司宣布发布新产品，但缺少实质信息。",
        "keywords": [],
    }


def test_keywords_too_many_raises():
    payload = _ingest_payload(
        keywords=[
            {"name": "A", "type": "org"},
            {"name": "B", "type": "org"},
            {"name": "C", "type": "org"},
            {"name": "D", "type": "org"},
        ]
    )
    with pytest.raises(ValueError, match="keywords count out of range"):
        validate_screen_result(payload)


def test_keywords_item_not_dict_raises():
    payload = _ingest_payload(keywords=["OpenAI"])
    with pytest.raises(ValueError, match="keyword item must be dict"):
        validate_screen_result(payload)


def test_keywords_name_empty_raises():
    payload = _ingest_payload(keywords=[{"name": "  ", "type": "org"}])
    with pytest.raises(ValueError, match="keyword name empty"):
        validate_screen_result(payload)


def test_keywords_name_too_long_raises():
    long_name = "X" * 21
    payload = _ingest_payload(keywords=[{"name": long_name, "type": "org"}])
    with pytest.raises(ValueError, match="keyword name too long"):
        validate_screen_result(payload)


def test_keywords_name_too_long_is_dropped_when_valid_keywords_remain():
    long_name = "X" * 21
    payload = _ingest_payload(
        keywords=[
            {"name": long_name, "type": "org"},
            {"name": "OpenAI", "type": "org"},
        ]
    )
    result = validate_screen_result(payload)
    assert result["keywords"] == [{"name": "OpenAI", "type": "org"}]


def test_keywords_invalid_type_raises():
    payload = _ingest_payload(keywords=[{"name": "OpenAI", "type": "company"}])
    with pytest.raises(ValueError, match="keyword type invalid"):
        validate_screen_result(payload)


def test_keywords_type_lowercased():
    payload = _ingest_payload(keywords=[{"name": "OpenAI", "type": "Org"}])
    result = validate_screen_result(payload)
    assert result["keywords"] == [{"name": "OpenAI", "type": "org"}]


def test_keywords_valid_ingest_passes_through():
    payload = _ingest_payload(
        keywords=[
            {"name": "OpenAI", "type": "org"},
            {"name": "ChatGPT", "type": "product"},
            {"name": "成人模式", "type": "topic"},
        ]
    )
    result = validate_screen_result(payload)
    assert result["action"] == "ingest"
    assert result["categories"] == ["AI前沿资讯"]
    assert result["score"] == 8.0
    assert result["summary"] == "OpenAI 发布新模型。"
    assert result["brief_summary"] == "OpenAI 发布新模型。"
    assert result["keywords"] == [
        {"name": "OpenAI", "type": "org"},
        {"name": "ChatGPT", "type": "product"},
        {"name": "成人模式", "type": "topic"},
    ]


def test_ingest_allows_denoise_contract_without_categories_or_score():
    payload = _ingest_payload()
    payload.pop("categories")
    payload.pop("score")
    result = validate_screen_result(payload)
    assert result["action"] == "ingest"
    assert "categories" not in result
    assert "score" not in result
    assert result["summary"] == "OpenAI 发布新模型。"


def test_categories_reject_unknown_label():
    payload = _ingest_payload(categories=["AI前沿资讯", "AI产品"])
    with pytest.raises(ValueError, match="invalid categories: AI产品"):
        validate_screen_result(payload)


def test_categories_dedupes_and_keeps_allowed_labels():
    payload = _ingest_payload(categories=["AI前沿资讯", "AI前沿资讯", "科技与产业趋势"])
    result = validate_screen_result(payload)
    assert result["categories"] == ["AI前沿资讯", "科技与产业趋势"]


def test_ingest_requires_summary():
    payload = _ingest_payload(summary="  ")
    with pytest.raises(ValueError, match="missing summary"):
        validate_screen_result(payload)


def test_ingest_still_accepts_legacy_brief_summary():
    payload = _ingest_payload()
    payload.pop("summary")
    payload["brief_summary"] = "OpenAI 发布新模型。"
    result = validate_screen_result(payload)
    assert result["summary"] == "OpenAI 发布新模型。"


def test_keywords_valid_pass_passes_through():
    payload = _pass_payload(
        keywords=[{"name": "某公司专有名", "type": "org"}]
    )
    result = validate_screen_result(payload)
    assert result == {
        "action": "pass",
        "reason": "命中规则2：通稿。",
        "title_zh": "某公司发布新产品",
        "summary": "某公司宣布发布新产品，但缺少实质信息。",
        "keywords": [{"name": "某公司专有名", "type": "org"}],
    }


def test_pass_requires_summary():
    payload = _pass_payload(summary="")
    with pytest.raises(ValueError, match="missing summary"):
        validate_screen_result(payload)


def test_pass_requires_title_zh():
    payload = _pass_payload(title_zh="")
    with pytest.raises(ValueError, match="missing title_zh"):
        validate_screen_result(payload)
