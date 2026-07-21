import json
import re
from pathlib import Path


DOCS = Path(__file__).resolve().parents[1] / "docs"


def _read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8-sig")


def _output_json_strings(text: str) -> list[str]:
    lines = text.splitlines()
    outputs: list[str] = []
    for idx, line in enumerate(lines):
        if "Output:" not in line:
            continue
        value = line.split("Output:", 1)[1].strip()
        if not value:
            for next_line in lines[idx + 1:]:
                value = next_line.strip()
                if value:
                    break
        if value.startswith("{"):
            outputs.append(value)
    return outputs


def _fenced_json_objects(text: str) -> list[dict]:
    return [json.loads(value) for value in re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)]


def test_local_prompt_output_examples_are_valid_json():
    for path in sorted(DOCS.glob("local-*.md")):
        for text in _output_json_strings(path.read_text(encoding="utf-8-sig")):
            json.loads(text)


def test_screen_prompt_examples_include_keywords():
    text = _read("local-screen-prompt.md")
    outputs = _fenced_json_objects(text)
    assert outputs
    for payload in outputs:
        assert isinstance(payload.get("keywords"), list)
        assert 1 <= len(payload["keywords"]) <= 3
        for item in payload["keywords"]:
            assert sorted(item) == ["name", "type"]


def test_screen_prompt_ingest_contract_has_required_fields():
    required = {"action", "reason", "title_zh", "summary", "keywords", "categories", "score"}
    text = _read("local-screen-prompt.md")
    outputs = _fenced_json_objects(text)
    ingest_count = 0
    qa_count = 0
    for payload in outputs:
        if payload.get("action") == "ingest":
            ingest_count += 1
            assert required.issubset(payload)
            assert "brief_summary" not in payload
            if "qa" in payload:
                qa_count += 1
                assert isinstance(payload["qa"], list)
                for item in payload["qa"]:
                    assert "question" in item and "answer" in item
    assert ingest_count >= 1
    assert qa_count >= 1


def test_screen_prompt_pass_schema_matches_validator():
    text = _read("local-screen-prompt.md")
    for marker in ("## 模式 B（pass", "模式 B（丢弃）字段顺序：", "模式 B（丢弃）："):
        parts = text.split(marker, 1)
        if len(parts) > 1:
            pass_block = parts[1].split("}", 1)[0]
            break
    else:
        raise AssertionError("模式 B block not found")
    for field in ('"action"', '"reason"', '"title_zh"', '"summary"', '"keywords"'):
        assert field in pass_block
    assert "`ingest` 严格 1-3 个" in text
    assert "`pass` 允许 0-3 个" in text
    assert "无效内容" not in text


def test_triage_prompt_has_three_way_json_contract():
    text = _read("local-screen-triage-prompt.md")
    assert '"verdict":"keep或filter或uncertain"' in text
    assert '"evidence"' in text
    assert '"reason"' in text
    assert "不要评分、不要分类、不要改标题、不要写摘要、关键词或 QA" in text


def test_summarize_schema_shows_at_least_three_qa_items():
    text = _read("local-summarize-prompt.md")
    schema = text.split("# JSON Schema", 1)[1].split("# Step 1", 1)[0]
    assert len(re.findall(r'"question"', schema)) >= 3
    assert "少于 3 组会被系统拒绝" in text
    assert '"title_zh"' not in schema


def test_merge_prompts_do_not_show_markdown_fences():
    for name in ("local-merge-prompt.md", "local-merge-prompt-simple.md"):
        assert "```" not in _read(name)


def test_dedup_prompt_requires_literal_action_object_match():
    text = _read("local-dedup-prompt.md")
    assert "同一动作对象" in text
    assert "字面出现在双方摘要中" in text
    assert "不允许\"推断\"" in text
    assert "不允许\"归并\"" in text
    assert "不允许\"上下文可视为\"" in text


def test_dedup_prompt_keeps_openclaw_hermes_counterexample():
    text = _read("local-dedup-prompt.md")
    assert "OpenClaw 代理平台 vs xAI 通过 Hermes Agent 开放 Grok" in text
    assert "平台名字面不同" in text
