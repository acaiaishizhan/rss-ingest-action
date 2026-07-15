# 关键词抽取 MVP — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** screen LLM 输出 1-3 个关键词并落到 NEWS / FILTERED 表的多选字段，零归一化、零独立关键词表。

**Architecture:** 增量提示词文件（`docs/local-screen-keywords-addendum.md`）在 `load_local_prompt_sections` 默认路径分支被读取并追加到 screen prompt 之后；`validate_screen_result` 新增 `_validate_keywords` 校验并把 `[{name, type}]` 透传到返回 dict；`build_news_fields` 与 `build_filtered_fields` 把 keyword `name` 列表写入飞书 NEWS / FILTERED 表的多选字段；其它流程不变。

**Tech Stack:** Python 3 + requests + 飞书 Bitable API + pytest

---

## 文件清单

本计划共涉及以下文件（实施 P8 不要在此清单外动文件）：

- 新建 `F:/coding/rss-ingest-local/docs/local-screen-keywords-addendum.md` — 关键词抽取规则增量提示词。
- 新建 `F:/coding/rss-ingest-local/tests/test_validate_screen_result_keywords.py` — keywords 校验单测。
- 新建 `F:/coding/rss-ingest-local/tests/test_build_news_fields_keywords.py` — keywords 写入飞书字段单测。
- 修改 `F:/coding/rss-ingest-local/config.py` — 新增 `NEWS_FIELD_KEYWORDS` / `FILTERED_FIELD_KEYWORDS` / `KEYWORD_TYPE_OPTIONS` / `LOCAL_SCREEN_KEYWORDS_ADDENDUM_PATH`。
- 修改 `F:/coding/rss-ingest-local/rss-ingest-local.env.example` — 新增 `LOCAL_SCREEN_KEYWORDS_ADDENDUM_PATH` 示例行。
- 修改 `F:/coding/rss-ingest-local/rss_ingest.py` — `validate_screen_result` 加 `_validate_keywords`、`load_local_prompt_sections` 默认路径分支拼接 addendum、`build_news_fields` / `build_filtered_fields` 写多选字段。
- 修改 `F:/coding/rss-ingest-local/AGENTS.md` — Project Baseline 区域加一条「关键词字段需要先在飞书 NEWS / FILTERED 表上手动加多选列」。

---

## Task 1 — config.py 常量 + env 示例 + AGENTS.md 部署提醒

### Files

- Modify `F:/coding/rss-ingest-local/config.py`
- Modify `F:/coding/rss-ingest-local/rss-ingest-local.env.example`
- Modify `F:/coding/rss-ingest-local/AGENTS.md`

### Steps

- [x] **Step 1.1** 在 `config.py` 第 44 行（`LOCAL_PROMPT_RULES_PATH = ...` 之后）追加 addendum 路径常量：

  ```python
  LOCAL_SCREEN_KEYWORDS_ADDENDUM_PATH = os.getenv(
      "LOCAL_SCREEN_KEYWORDS_ADDENDUM_PATH",
      str(BASE_DIR / "docs" / "local-screen-keywords-addendum.md"),
  )
  ```

- [x] **Step 1.2** 在 `config.py` 中找到 `NEWS_FIELD_READ = "已读"` 行（第 72 行附近），在该行后追加 NEWS 关键词字段常量：

  ```python
  NEWS_FIELD_KEYWORDS = "关键词"
  ```

  并在 `FILTERED_FIELD_ITEM_KEY = "item_key"` 行（第 79 行附近）之后追加 FILTERED 关键词字段常量：

  ```python
  FILTERED_FIELD_KEYWORDS = "关键词"
  ```

- [x] **Step 1.3** 在 `config.py` 的 `ITEM_ID_STRATEGY_OPTIONS` / `CONTENT_LANGUAGE_OPTIONS` 这一类枚举集合附近（第 117-118 行附近）追加：

  ```python
  KEYWORD_TYPE_OPTIONS = {
      "org",
      "person",
      "product",
      "model",
      "technology",
      "hardware",
      "policy",
      "case",
      "topic",
  }
  ```

- [x] **Step 1.4** 在 `rss-ingest-local.env.example` 的「Local prompt files」区块（第 19-23 行）末尾追加一行：

  ```
  LOCAL_SCREEN_KEYWORDS_ADDENDUM_PATH = docs/local-screen-keywords-addendum.md
  ```

  最终该区块应该是：

  ```
  # Local prompt files
  LOCAL_KEYWORD_BLOCKLIST_PATH = docs/local-keyword-blocklist.txt
  LOCAL_SCREEN_PROMPT_PATH = docs/local-screen-prompt.md
  LOCAL_SUMMARIZE_PROMPT_PATH = docs/local-summarize-prompt.md
  LOCAL_PROMPT_RULES_PATH = docs/local-prompt-rules.md
  LOCAL_SCREEN_KEYWORDS_ADDENDUM_PATH = docs/local-screen-keywords-addendum.md
  ```

- [x] **Step 1.5** 在 `AGENTS.md` 的 Project Baseline 段（第 7-13 行）末尾追加一条，提醒部署步骤。在 `docs/local-keyword-blocklist.txt、docs/local-screen-prompt.md、docs/local-summarize-prompt.md：本地提示词配置。` 之后追加：

  ```
  - `docs/local-screen-keywords-addendum.md`：screen prompt 关键词抽取规则增量。
  
  > 关键词字段需要先在飞书 NEWS / FILTERED 表上各加一列「关键词」（多选字段）后才能跑，否则飞书写入会因字段不存在而失败。
  ```

- [x] **Step 1.6** 跑测试，确认现有 config 测试不受影响：

  ```
  .\.venv\Scripts\python.exe -m pytest tests/test_config_defaults.py -v
  ```

  Expected: 所有现有测试 pass（绿色）。本任务未新增 config 测试，仅确认未破坏现状。

- [x] **Step 1.7** Commit：

  ```
  git add config.py rss-ingest-local.env.example AGENTS.md
  git commit -m "chore: add keyword field constants and env scaffolding"
  ```

---

## Task 2 — validate_screen_result keywords 校验（TDD）

### Files

- Create `F:/coding/rss-ingest-local/tests/test_validate_screen_result_keywords.py`
- Modify `F:/coding/rss-ingest-local/rss_ingest.py`（`validate_screen_result`，第 270 行附近）

### Steps

- [x] **Step 2.1** 新建 `tests/test_validate_screen_result_keywords.py`，写完整的失败测试集合：

  ```python
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
      }
      payload.update(overrides)
      return payload


  def _pass_payload(**overrides):
      payload = {
          "action": "pass",
          "reason": "命中规则2：通稿。",
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
      assert result["keywords"] == [
          {"name": "OpenAI", "type": "org"},
          {"name": "ChatGPT", "type": "product"},
          {"name": "成人模式", "type": "topic"},
      ]


  def test_keywords_valid_pass_passes_through():
      payload = _pass_payload(
          keywords=[{"name": "某公司专有名", "type": "org"}]
      )
      result = validate_screen_result(payload)
      assert result == {
          "action": "pass",
          "reason": "命中规则2：通稿。",
          "keywords": [{"name": "某公司专有名", "type": "org"}],
      }
  ```

- [x] **Step 2.2** 跑测试，确认新测试因当前 `validate_screen_result` 不识别 keywords 而 fail：

  ```
  .\.venv\Scripts\python.exe -m pytest tests/test_validate_screen_result_keywords.py -v
  ```

  Expected: 11 个测试全部 fail（FAILED），原因主要是 KeyError / 没抛 ValueError / 返回 dict 不含 `keywords` 键。

- [x] **Step 2.3** 修改 `rss_ingest.py`，在 `validate_screen_result`（第 270 行）之前新增辅助函数 `_validate_keywords`：

  ```python
  def _validate_keywords(raw: Any) -> List[Dict[str, str]]:
      if raw is None:
          raise ValueError("missing keywords")
      if not isinstance(raw, list):
          raise ValueError("keywords must be list")
      if len(raw) < 1 or len(raw) > 3:
          raise ValueError("keywords count out of range")

      out: List[Dict[str, str]] = []
      for item in raw:
          if not isinstance(item, dict):
              raise ValueError("keyword item must be dict")
          name = str(item.get("name") or "").strip()
          type_ = str(item.get("type") or "").strip().lower()
          if not name:
              raise ValueError("keyword name empty")
          if len(name) > 20:
              raise ValueError("keyword name too long")
          if type_ not in config.KEYWORD_TYPE_OPTIONS:
              raise ValueError(f"keyword type invalid: {type_ or 'empty'}")
          out.append({"name": name, "type": type_})
      return out
  ```

- [x] **Step 2.4** 修改 `rss_ingest.py` 的 `validate_screen_result`（第 270-295 行），在 reason 校验之后、pass 路径之前调用 `_validate_keywords`，并把结果透传到两条返回路径：

  ```python
  def validate_screen_result(analysis: Dict[str, Any]) -> Dict[str, Any]:
      action = str(analysis.get("action") or "").strip().lower()
      if action not in ("ingest", "pass"):
          raise ValueError(f"invalid action: {action or 'empty'}")

      reason = str(analysis.get("reason") or "").strip()
      if not reason:
          raise ValueError("missing reason")

      keywords = _validate_keywords(analysis.get("keywords"))

      if action == "pass":
          return {"action": "pass", "reason": reason, "keywords": keywords}

      categories = normalize_string_list(analysis.get("categories") or [])
      if not categories:
          raise ValueError("missing categories")

      score = parse_score(analysis.get("score"))
      if score is None or score < 0 or score > 10:
          raise ValueError("invalid score")

      return {
          "action": "ingest",
          "reason": reason,
          "categories": categories[:3],
          "score": score,
          "keywords": keywords,
      }
  ```

- [x] **Step 2.5** 跑新测试，期望全 pass：

  ```
  .\.venv\Scripts\python.exe -m pytest tests/test_validate_screen_result_keywords.py -v
  ```

  Expected: 11 个测试全 PASSED。

- [x] **Step 2.6** 跑全量测试，确认未破坏现有逻辑：

  ```
  .\.venv\Scripts\python.exe -m pytest -q
  ```

  Expected: 全 pass（绿色）。如果有现有测试 fail，原因大概率是某些用例构造的 analysis 未带 keywords 字段；这种情况下排查具体测试，把它的 mock analysis 补上合法 keywords 字段（仅在该测试本身，不要扩散）。

- [x] **Step 2.7** Commit：

  ```
  git add tests/test_validate_screen_result_keywords.py rss_ingest.py
  git commit -m "feat: validate keywords in screen result"
  ```

---

## Task 3 — addendum 提示词文件 + prompt loader 拼接

### Files

- Create `F:/coding/rss-ingest-local/docs/local-screen-keywords-addendum.md`
- Modify `F:/coding/rss-ingest-local/rss_ingest.py`（`load_local_prompt_sections`，第 182 行附近）

### Steps

- [x] **Step 3.1** 新建 `docs/local-screen-keywords-addendum.md`，按以下完整内容落盘（直接复制）：

  ```markdown
  # Schema 增量：keywords 字段（强制）

  在 Step 4 输出规范的基础上，模式 A（ingest）和模式 B（pass）的 JSON 都**必须**额外包含 `keywords` 字段。

  ## Schema 扩展

  模式 A（保留）：
  {
    "action": "ingest",
    "categories": ["Tag1", "Tag2"],
    "score": 0.0,
    "reason": "一句话说明保留原因",
    "keywords": [{"name": "...", "type": "..."}]
  }

  模式 B（丢弃）：
  {
    "action": "pass",
    "reason": "命中规则X：[具体原因]",
    "keywords": [{"name": "...", "type": "..."}]
  }

  ## keywords 字段规则

  - 数量：1-3 个；宁可少，不要凑满。
  - 单个 name：长度 ≤ 20 个字符（按文本字符计，包含中英文 / 数字 / 空格 / 连字符 / 标点）。
  - type 必须是以下 9 个枚举之一（小写英文）：
    - org：公司、机构、政府部门、大学、实验室、组织
    - person：人物
    - product：产品、工具、平台、应用、功能
    - model：AI 模型、模型系列、模型版本
    - technology：技术、协议、框架、API、数据集、基准、算法方法
    - hardware：芯片、机器人、设备、算力基础设施、数据中心
    - policy：政策、法规、标准、监管框架
    - case：具体案例、案件、交易、收购、融资、事故、项目、战事、会议（必须是名词短语，不得输出完整新闻标题）
    - topic：可追踪的短议题、应用方向、行业现象

  ## 不应抽取

  不要输出泛化类别词、标题式短语或事件句。以下都是无效方向：

  - AI / 人工智能 / 科技 / 商业 / 模型 / 产品 / 行业趋势 / 产品更新
  - "OpenAI 推出成人模式" / "企业加速采用 AI"

  ## Few-Shot 反例

  Input: (一篇关于某不知名初创公司任命新市场总监的通稿)
  Output:
  {"action":"pass","reason":"命中规则2：无实质内容的常规人事任命公关稿，且非顶级巨头。","keywords":[{"name":"该公司专有名","type":"org"}]}
  （即使是 pass 也要抽 keywords；如果原文只剩通稿模板没有任何专有名词，可以只输出 1 个最核心的 org / person）

  Input: (OpenAI 计划于 2026 年第一季度推出 ChatGPT "成人模式" 并引入年龄预测系统的万字深度报道)
  Output:
  {"action":"ingest","categories":["AI前沿资讯","科技与产业趋势"],"score":9.2,"reason":"涉及平台内容分级与年龄识别底层机制，具备极强的产品参考与战略讨论价值。","keywords":[{"name":"OpenAI","type":"org"},{"name":"ChatGPT","type":"product"},{"name":"成人模式","type":"topic"}]}

  注意：宁可只输出 1 个高确定性的关键词，也不要凑满 3 个泛化词。
  ```

- [x] **Step 3.2** 修改 `rss_ingest.py` 的 `load_local_prompt_sections`（第 182-206 行）。注意：单文件双 marker 兼容路径（`if path is not None:` 分支）**保持原状不动**——只在默认路径（无 path 入参）分支拼接 addendum。

  把第 190-206 行替换成：

  ```python
      keyword_path = resolve_local_doc_path(config.LOCAL_KEYWORD_BLOCKLIST_PATH)
      screen_path = resolve_local_doc_path(config.LOCAL_SCREEN_PROMPT_PATH)
      summarize_path = resolve_local_doc_path(config.LOCAL_SUMMARIZE_PROMPT_PATH)
      keywords_addendum_path = resolve_local_doc_path(config.LOCAL_SCREEN_KEYWORDS_ADDENDUM_PATH)

      keyword_blocklist = parse_keyword_blocklist(keyword_path.read_text(encoding="utf-8"))
      screen_prompt = load_prompt_text_file(screen_path)
      summarize_prompt = load_prompt_text_file(summarize_path)
      keywords_addendum = load_prompt_text_file(keywords_addendum_path)
      screen_prompt = f"{screen_prompt}\n\n{keywords_addendum}"

      return {
          "keyword_blocklist": keyword_blocklist,
          "screen_prompt": screen_prompt,
          "summarize_prompt": summarize_prompt,
          "keyword_path": str(keyword_path),
          "screen_path": str(screen_path),
          "summarize_path": str(summarize_path),
          "screen_keywords_addendum_path": str(keywords_addendum_path),
          "path": (
              f"keywords={keyword_path}; screen={screen_path}; "
              f"summarize={summarize_path}; "
              f"screen_keywords_addendum={keywords_addendum_path}"
          ),
      }
  ```

  注意：`load_prompt_text_file` 现有实现（第 174-179 行）已经处理了文件不存在（FileNotFoundError）和文件全空（ValueError），所以 addendum 文件缺失会让 `load_local_prompt_sections` 抛错，main 流程的现有 prompt 加载兜底会捕获并报错退出，符合 spec 4.3 的「启动期硬失败更早暴露问题」预期，不需要额外补 try/except。

- [x] **Step 3.3** 跑冒烟，确认 addendum 拼接成功：

  ```
  .\.venv\Scripts\python.exe -c "import os; os.environ['RSS_INGEST_SKIP_LOCAL_ENV']='true'; import rss_ingest; cfg = rss_ingest.load_local_prompt_sections(); assert 'keywords' in cfg.get('screen_prompt', ''), 'addendum not appended'; assert 'screen_keywords_addendum_path' in cfg, 'path not exposed'; print('OK')"
  ```

  Expected: 输出 `OK`（addendum 已拼接到 screen_prompt 末尾，且新 dict key 已存在）。

- [x] **Step 3.4** 跑全量测试确认未破：

  ```
  .\.venv\Scripts\python.exe -m pytest -q
  ```

  Expected: 全 pass。

- [x] **Step 3.5** Commit：

  ```
  git add docs/local-screen-keywords-addendum.md rss_ingest.py
  git commit -m "feat: append keywords addendum to screen prompt"
  ```

---

## Task 4 — build_news_fields / build_filtered_fields 写入多选字段（TDD）

### Files

- Create `F:/coding/rss-ingest-local/tests/test_build_news_fields_keywords.py`
- Modify `F:/coding/rss-ingest-local/rss_ingest.py`（`build_news_fields` 第 1517 行 / `build_filtered_fields` 第 1553 行）

### Steps

- [x] **Step 4.1** 新建 `tests/test_build_news_fields_keywords.py`，写完整的 failing 测试：

  ```python
  import os
  import sys

  sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

  os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

  import config  # noqa: E402
  from rss_ingest import build_filtered_fields, build_news_fields  # noqa: E402


  def _article():
      return {
          "title": "Some Title",
          "link": "https://example.com/x",
          "content": "<p>Hello</p>",
          "source": "Example",
          "published_ts": 1_700_000_000.0,
      }


  def _ingest_analysis(**overrides):
      data = {
          "action": "ingest",
          "reason": "保留",
          "categories": ["AI前沿资讯"],
          "score": 8.0,
          "title_zh": "中文标题",
          "qa": [
              {"q": "Q1", "a": "A1"},
              {"q": "Q2", "a": "A2"},
              {"q": "Q3", "a": "A3"},
          ],
          "keywords": [
              {"name": "OpenAI", "type": "org"},
              {"name": "ChatGPT", "type": "product"},
              {"name": "成人模式", "type": "topic"},
          ],
      }
      data.update(overrides)
      return data


  def _filtered_analysis(**overrides):
      data = {
          "action": "pass",
          "reason": "命中规则2：通稿。",
          "keywords": [
              {"name": "某公司", "type": "org"},
          ],
          "_llm_meta": {"filter_method": "初筛过滤", "filter_reason": "命中规则2"},
      }
      data.update(overrides)
      return data


  def test_build_news_fields_writes_keywords():
      fields = build_news_fields(_article(), _ingest_analysis(), "key-1")
      assert fields[config.NEWS_FIELD_KEYWORDS] == ["OpenAI", "ChatGPT", "成人模式"]


  def test_build_news_fields_keywords_empty_when_missing():
      analysis = _ingest_analysis()
      analysis.pop("keywords")
      fields = build_news_fields(_article(), analysis, "key-2")
      assert fields[config.NEWS_FIELD_KEYWORDS] == []


  def test_build_news_fields_skips_invalid_items():
      analysis = _ingest_analysis(
          keywords=[
              {"name": "", "type": "org"},
              {"name": "  ", "type": "org"},
              {"name": "OpenAI", "type": "org"},
              {"type": "org"},
              "not-a-dict",
          ]
      )
      fields = build_news_fields(_article(), analysis, "key-3")
      assert fields[config.NEWS_FIELD_KEYWORDS] == ["OpenAI"]


  def test_build_filtered_fields_writes_keywords():
      fields = build_filtered_fields(_article(), _filtered_analysis(), "key-4")
      assert fields[config.FILTERED_FIELD_KEYWORDS] == ["某公司"]


  def test_build_filtered_fields_keywords_empty_when_failed_analysis():
      # 模拟 build_failed_analysis 产物：reason 存在，但 keywords 字段缺失
      failed_analysis = {
          "action": "pass",
          "reason": "screen failed: missing keywords",
          "_llm_meta": {"filter_method": "LLM失败兜底", "filter_reason": "screen failed"},
      }
      fields = build_filtered_fields(_article(), failed_analysis, "key-5")
      assert fields[config.FILTERED_FIELD_KEYWORDS] == []
  ```

- [x] **Step 4.2** 跑新测试，期望全 fail（因为 `build_news_fields` / `build_filtered_fields` 当前不写 `NEWS_FIELD_KEYWORDS` / `FILTERED_FIELD_KEYWORDS`，断言会 KeyError）：

  ```
  .\.venv\Scripts\python.exe -m pytest tests/test_build_news_fields_keywords.py -v
  ```

  Expected: 5 个测试全部 FAILED（KeyError 居多）。

- [x] **Step 4.3** 修改 `rss_ingest.py` 的 `build_news_fields`（第 1517-1539 行）。在 `qa = normalize_qa(...)` / `summary = build_summary(qa)` 之后、`return` 之前插入 keywords 萃取，并在返回 dict 末尾加一行 `config.NEWS_FIELD_KEYWORDS: keyword_names`：

  ```python
  def build_news_fields(article: Dict[str, Any], analysis: Dict[str, Any], item_key: str) -> Dict[str, Any]:
      base = build_article_base_fields(article, item_key)
      title_zh = (analysis.get("title_zh") or "").strip()
      title_text = title_zh if title_zh else base["title"]

      score = float(analysis.get("score", 0.0) or 0.0)
      categories = analysis.get("categories") or []
      if not isinstance(categories, list):
          categories = [str(categories)]

      qa = normalize_qa(analysis.get("qa") or [])
      summary = build_summary(qa)

      keywords_raw = analysis.get("keywords") or []
      keyword_names = [
          kw["name"].strip()
          for kw in keywords_raw
          if isinstance(kw, dict)
          and isinstance(kw.get("name"), str)
          and kw["name"].strip()
      ]

      return {
          config.NEWS_FIELD_TITLE: {"text": title_text, "link": base["link"]},
          config.NEWS_FIELD_SCORE: score,
          config.NEWS_FIELD_CATEGORIES: categories,
          config.NEWS_FIELD_SUMMARY: summary,
          config.NEWS_FIELD_PUBLISHED_MS: base["published_ts_ms"],
          config.NEWS_FIELD_SOURCE: base["source"],
          config.NEWS_FIELD_FULL_CONTENT: base["full_content"],
          config.NEWS_FIELD_ITEM_KEY: base["item_key"],
          config.NEWS_FIELD_KEYWORDS: keyword_names,
      }
  ```

- [x] **Step 4.4** 修改 `rss_ingest.py` 的 `build_filtered_fields`（第 1553-1572 行）。在现有逻辑（filter_method / keyword_hit / filter_reason 部分）保留不动的前提下，加 keywords 萃取并在返回 dict 末尾加 `config.FILTERED_FIELD_KEYWORDS: keyword_names`：

  ```python
  def build_filtered_fields(article: Dict[str, Any], analysis: Dict[str, Any], item_key: str) -> Dict[str, Any]:
      base = build_article_base_fields(article, item_key)
      llm_meta = get_llm_meta(analysis)
      filter_method = str(llm_meta.get("filter_method") or "").strip()
      if not filter_method:
          filter_method = "关键词过滤" if llm_meta.get("keyword_filtered") else "初筛过滤"
      keyword_hit = str(llm_meta.get("keyword_hit") or "").strip()
      filter_reason = str(llm_meta.get("filter_reason") or analysis.get("reason") or "").strip()
      if keyword_hit:
          suffix = f"（命中关键词：{keyword_hit}）"
          filter_reason = f"{filter_reason}{suffix}" if filter_reason else suffix

      keywords_raw = analysis.get("keywords") or []
      keyword_names = [
          kw["name"].strip()
          for kw in keywords_raw
          if isinstance(kw, dict)
          and isinstance(kw.get("name"), str)
          and kw["name"].strip()
      ]

      return {
          config.FILTERED_FIELD_TITLE: {"text": base["title"], "link": base["link"]},
          config.FILTERED_FIELD_FILTER_METHOD: filter_method,
          config.FILTERED_FIELD_FILTER_REASON: filter_reason,
          config.FILTERED_FIELD_PUBLISHED_MS: base["published_ts_ms"],
          config.FILTERED_FIELD_SOURCE: base["source"],
          config.FILTERED_FIELD_FULL_CONTENT: base["full_content"],
          config.FILTERED_FIELD_ITEM_KEY: base["item_key"],
          config.FILTERED_FIELD_KEYWORDS: keyword_names,
      }
  ```

- [x] **Step 4.5** 跑新测试，期望全 pass：

  ```
  .\.venv\Scripts\python.exe -m pytest tests/test_build_news_fields_keywords.py -v
  ```

  Expected: 5 个测试全 PASSED。

- [x] **Step 4.6** 跑全量测试，确认未破其它现有测试：

  ```
  .\.venv\Scripts\python.exe -m pytest -q
  ```

  Expected: 全 pass。

- [x] **Step 4.7** Commit：

  ```
  git add tests/test_build_news_fields_keywords.py rss_ingest.py
  git commit -m "feat: write keywords to NEWS and FILTERED tables"
  ```

---

## Task 5 — 端到端验证（手动，无代码改动）

### Files

无（手动操作 + 跑一次 `rss_ingest.py`，不改代码）。

### Steps

- [x] **Step 5.1**（用户手动）在飞书 NEWS 表 UI 上新增一列：列名输入 `关键词`，字段类型选**多选**，保存。无需预加任何选项。

- [x] **Step 5.2**（用户手动）在飞书 FILTERED 表 UI 上重复同样操作：列名 `关键词`，类型多选。

- [x] **Step 5.3** 跑一次完整主流程：

  ```
  .\.venv\Scripts\python.exe rss_ingest.py
  ```

  等待运行完成，关注末尾 `[Summary]` 行。Expected: `llm_failed`、`feishu_failed`、`filtered_log_failed` 数量与之前 baseline 相比无明显恶化（可对照最近一次运行的日志）。如果 `llm_failed` 显著上升，大概率是 LLM 没遵守新 schema，回头看 addendum 是否需要加固反例（修文案后再跑，但不在本 plan 范围内追新代码改动）。

- [x] **Step 5.4** 在飞书 NEWS 表 UI 抽 3-5 条最新写入记录（按「创建时间」倒序），逐条确认「关键词」列有 1-3 个 chip，关键词内容跟标题语义匹配（不是泛化类别词、不是标题式短语）。

- [x] **Step 5.5** 在飞书 FILTERED 表 UI 同样抽 3-5 条最新记录，确认「关键词」列也有 1-3 个 chip。

- [x] **Step 5.6** 不需要 commit（本任务无代码改动）。

- [x] **Step 5.7** 如果在 Step 5.4 / 5.5 发现质量问题（例如关键词凑数、type 偏差、出现禁止清单中的泛词），不要在本 plan 范围内修改 addendum 或代码——把问题记录到下方「已知后续工作」段，留给阶段 1 或后续 prompt 迭代专门处理。MVP 验收标准只是「飞书 UI 上能看见关键词 chip」，质量优化属于后续阶段。

---

## Self-Review

### Spec coverage

- spec § 4 prompt 增量 → Task 3（addendum 文件 + loader 拼接）✓
- spec § 5 Schema 扩展 → Task 3 addendum 内容已含两套 schema 样例 ✓
- spec § 6 validate_screen_result → Task 2（_validate_keywords + 透传两条返回路径）✓
- spec § 6.5 KEYWORD_TYPE_OPTIONS → Task 1 Step 1.3 ✓
- spec § 7 飞书字段定义 → Task 1 Step 1.2（常量）+ Task 5 Step 5.1/5.2（用户手动加列）✓
- spec § 8 写入路径 → Task 4（build_news_fields / build_filtered_fields）✓
- spec § 9 失败 / 降级路径 → 完全复用现有失败池机制，本 plan 不新增分支（Task 2 复用 ValueError 即触发现有 build_failed_analysis 流程）✓
- spec § 10.1 单元测试 → Task 2（11 条 validate 测试，含 type 归一与 ingest/pass 透传）+ Task 4（5 条 build 测试，含失败 analysis 兜底）✓
- spec § 10.2 端到端验证 → Task 5 ✓
- spec § 11 实施顺序建议 → Task 1-5 依次为 config / addendum+loader / validate / build / 端到端，与 spec 顺序略调整（先 config，再 validate 走 TDD，再 addendum/loader，再 build，最后端到端），原因是把测试驱动的两块（validate/build）紧贴各自实现，loader 拼接放中间确保跑端到端时 prompt 已就位 ✓
- spec § 12 DON'T 清单 → 本 plan 未触碰 `docs/local-screen-prompt.md` / `docs/local-summarize-prompt.md`、未做归一化、未建 KEYWORD 表、未加 cache / lock / cron、未把 type 写入飞书、未回填历史 ✓

### P9 锚定决策

- 决策 1：prompt loader 双 marker 兼容路径不引入 addendum → Task 3 Step 3.2 显式说明「`if path is not None:` 分支保持原状不动」✓
- 决策 2：name 长度用 Python `len()` codepoint，不处理 emoji / ZWJ → Task 2 Step 2.3 `_validate_keywords` 使用 `len(name) > 20` ✓
- 决策 3：`build_filtered_fields` 在失败 analysis 无 keywords 时兜底空 list → Task 4 Step 4.4 `analysis.get("keywords") or []` + 测试 `test_build_filtered_fields_keywords_empty_when_failed_analysis` ✓
- 决策 4：validate 时 type `.lower()` 归一 → Task 2 Step 2.3 `_validate_keywords` 调 `.strip().lower()`，并由 `test_keywords_type_lowercased` 验证 ✓

### Placeholder scan

- 全文未出现 "TBD"、"TODO"、"按 spec 处理"、"参考 spec 6.2"、"详见"。所有伪代码已 inline 为可运行 Python / Markdown / 命令。

### Type consistency

- keywords 在 LLM JSON / `_validate_keywords` 输出 / `validate_screen_result` 返回 dict / `build_*_fields` 输入 analysis 中的类型一致：`List[Dict[str, str]]`，每项 `{"name": str, "type": str}`。
- 飞书字段写入类型一致：`List[str]`（仅 name），由 `build_news_fields` / `build_filtered_fields` 内的列表推导式生成。
- type 信息在飞书层面被丢弃，但保留在 analysis dict 中供阶段 1 复用——与 spec § 3 决策「chip 内容仅 name，不带 type」一致。

---

## 已知后续工作（OUT-of-scope，不在本 MVP 实施）

以下事项 spec § 2 OUT 段已显式排除，实施 P8 不要顺手做：

- 独立的 KEYWORD 表 / 双向关联字段（阶段 1）。
- 归一化（NFKC / lower / dedup / 别名 list）（阶段 1）。
- 进程内 cache / lock / `ensure_keyword`（阶段 1+）。
- 异步合并扫描 / `merge_keywords.py` / `--apply`（阶段 2-3）。
- type 信息写入飞书字段（阶段 1+ 决策）。
- 历史 NEWS / FILTERED 关键词回填（阶段 1+）。
- 修改 `docs/local-screen-prompt.md` 或 `docs/local-summarize-prompt.md`（永久禁区）。
- 接 cron / scheduled task / 在线热更新（阶段 4 之外）。
- LLM 抽词质量优化（凑词 / 类型偏差 / 越界）属于后续 prompt 迭代，不在本 MVP 内修代码。
- emoji / ZWJ 序列引发的 codepoint 长度偏差，留给后续阶段评估，不在 MVP 打补丁。
- Task 5 端到端如发现飞书写入异常（多选字段对未知选项的自动新增行为不符预期等），属于飞书侧适配工作，需要单独 spec / plan，不在本 MVP 内补救。
