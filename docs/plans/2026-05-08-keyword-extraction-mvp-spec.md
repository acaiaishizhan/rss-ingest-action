# 关键词抽取 MVP Spec — 阶段 0

## 1. 概述

`rss-ingest-local` 项目正在落地「关键词监控」feature，整体阶段已在同目录文档 `2026-05-08-keyword-monitoring-phases.md` 中拆分为 5 个阶段（阶段 0 MVP / 阶段 1 关键词表 / 阶段 2 合并扫描 dry-run / 阶段 3 合并 --apply / 阶段 4 事件聚类）。本 spec **只覆盖阶段 0 — MVP**：让 screen 阶段 LLM 输出关键词，校验后直接以多选字段的形式写入 NEWS 表与 FILTERED 表，让人能在飞书 UI 上「看见」抽到的关键词，作为后续阶段的质量观察基线。

抽取规则的细节（数量 1-3、长度 ≤20、9 类枚举、不应抽取清单）以同目录 `2026-05-08-keyword-monitoring-design.md` 为准，本 spec 在 prompt 增量与校验逻辑中复用，不重复展开。

本 spec 不是 implementation plan。spec 通过后，由后续 plan 拆任务、写代码、跑端到端验证。

## 2. 范围

### IN（本 spec 覆盖）

- screen LLM 输出新增 `keywords` 字段，规则通过新建一个**增量提示词文件**叠加进 screen prompt（不修改 `docs/local-screen-prompt.md` 现有内容）。
- prompt loader 在加载 screen prompt 之后追加 addendum，最终 screen_prompt = 原内容 + 分隔 + addendum 内容。
- `validate_screen_result` 增加 keywords 字段校验（数量 / 长度 / 类型枚举 / 结构）。
- NEWS 表与 FILTERED 表各新增一列「关键词」**多选字段**（用户手动在飞书 UI 上加）。
- `build_news_fields` 与 `build_filtered_fields` 把 keywords 的 `name` 列表写入对应的多选字段。
- `config.py` 新增常量：addendum 路径 / 多选字段名 / 类型枚举集合。
- `rss-ingest-local.env.example` 增加 addendum 路径示例。
- 新增单元测试覆盖 `validate_screen_result` 与 `build_news_fields` 的 keywords 行为。
- 端到端验证：跑一次 `rss_ingest.py`，飞书 NEWS / FILTERED 两表上能看到关键词 chip。

### OUT（本 spec 明确不做，留给后续阶段）

- 独立的关键词表（KEYWORD 表）/ 双向关联字段。
- 归一化（NFKC / lower / 去重 / 别名 list）。
- 进程内 cache / lock / `ensure_keyword`。
- 异步合并扫描 / `merge_keywords.py` / `--apply`。
- `type` 信息写入飞书字段（type 仅留在 LLM JSON 输出 + 内存 analysis dict，不进飞书 UI）。
- 历史 NEWS / FILTERED 关键词回填。
- 任何对 `docs/local-screen-prompt.md` 现有内容的修改。
- 任何对 `docs/local-summarize-prompt.md` 的修改（summary 阶段不参与抽词）。
- cron / scheduled task / 在线热更新。

## 3. 已对齐决策（对话沉淀）

以下决策在 brainstorm 中已对齐，本节固化下来作为后续 plan 的输入：

- **管线位置**：并入 screen stage，summary stage 完全不动。
- **覆盖范围**：全样本——不论 screen 输出 `action: ingest` 还是 `action: pass`，都必须输出 keywords。
- **失败策略**：keywords 字段必填。LLM 缺字段 / 字段为空 / 字段越界 → `validate_screen_result` 抛 `ValueError` → 走现有 `failed_items` 失败池路径（NVIDIA 重试 → fallback DeepSeek → 最终失败池），不新增专用兜底。
- **飞书字段类型**：飞书原生**多选字段**（不是双向关联，不是单选，不是文本），与现有「分类」字段同款。
- **chip 内容**：仅 `name`，不带 type。type 信息保留在 LLM JSON 输出与 Python 内存 analysis dict，方便后续阶段 1 落地 KEYWORD 表时直接复用，但 MVP 阶段不进飞书。
- **飞书字段创建方式**：用户手动在飞书 NEWS 表和 FILTERED 表 UI 上各加一列「关键词」多选，仓库不走 schema migration（与现有所有飞书字段一致）。
- **prompt 修改方式**：**绝对不动** `docs/local-screen-prompt.md`。新增独立 addendum 文件，由 prompt loader 在加载 screen prompt 之后拼接。
- **多选字段未知选项处理**：飞书 Bitable 多选字段在写入未知选项时自动新增选项，无需预配置选项列表。

## 4. Prompt 增量设计

### 4.1 文件与环境变量

- 新建文件：`docs/local-screen-keywords-addendum.md`（中文）。
- 新增环境变量：`LOCAL_SCREEN_KEYWORDS_ADDENDUM_PATH`，默认指向 `docs/local-screen-keywords-addendum.md`。
- `config.py` 新增常量：

  ```
  LOCAL_SCREEN_KEYWORDS_ADDENDUM_PATH = os.getenv(
      "LOCAL_SCREEN_KEYWORDS_ADDENDUM_PATH",
      str(BASE_DIR / "docs" / "local-screen-keywords-addendum.md"),
  )
  ```

- `rss-ingest-local.env.example` 在「Local prompt files」区块追加：

  ```
  LOCAL_SCREEN_KEYWORDS_ADDENDUM_PATH = docs/local-screen-keywords-addendum.md
  ```

### 4.2 拼接策略

在 `load_local_prompt_sections` 现有流程的基础上：

- 已有：`screen_prompt = load_prompt_text_file(screen_path)`。
- 在它之后追加：
  - `addendum_path = resolve_local_doc_path(config.LOCAL_SCREEN_KEYWORDS_ADDENDUM_PATH)`
  - `addendum_text = load_prompt_text_file(addendum_path)`（沿用现有函数：路径不存在或文件为空都会抛错）
  - `screen_prompt = f"{screen_prompt}\n\n{addendum_text}"`
- 返回 dict 同步加 `"screen_keywords_addendum_path": str(addendum_path)`，供日志 / 调试。
- `load_local_prompt_sections` 显式传 `path` 参数的分支（解析单文件双 marker 的兼容路径）暂不引入 addendum，保留旧行为；MVP 只在默认路径分支生效。

### 4.3 缺失处理

- addendum 文件不存在 → `load_prompt_text_file` 沿用现有行为抛 `FileNotFoundError`，主流程启动失败。
- addendum 文件存在但全空（`.strip()` 为空）→ 沿用现有 `load_prompt_text_file` 抛 `ValueError("prompt file is empty: ...")`。

部署一致性高于灵活性：keywords 字段是必填的，addendum 缺失意味着 LLM 不会被告知输出 keywords，必然全量校验失败、全量进失败池——直接启动期硬失败更早暴露问题。

### 4.4 addendum 文件内容草稿（实施时按此落盘）

> 以下为 `docs/local-screen-keywords-addendum.md` 的内容草稿，spec 阶段不要新建该文件，留给 implementation plan 落地。草稿已对齐 `2026-05-08-keyword-monitoring-design.md` 的抽取规则。

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

实施阶段如发现 LLM 仍倾向输出泛词或越界，可在 addendum 内继续加反例 / 加禁止清单——这是 addendum 的设计目的，跟原 prompt 解耦，便于迭代。

## 5. LLM 输出 Schema 扩展

ingest 模式：

```json
{
  "action": "ingest",
  "categories": ["AI前沿资讯", "科技与产业趋势"],
  "score": 9.2,
  "reason": "涉及平台内容分级与年龄识别底层机制，具备极强的产品参考与战略讨论价值。",
  "keywords": [
    {"name": "OpenAI", "type": "org"},
    {"name": "ChatGPT", "type": "product"},
    {"name": "成人模式", "type": "topic"}
  ]
}
```

pass 模式：

```json
{
  "action": "pass",
  "reason": "命中规则2：无实质内容的常规人事任命公关稿，且非顶级巨头。",
  "keywords": [
    {"name": "某公司专有名", "type": "org"}
  ]
}
```

字段约束（与 addendum 一致，以校验代码为准）：

- `keywords` 是 list。
- `1 <= len(keywords) <= 3`。
- 每项是 dict，`name` 是非空 str 且 `len(name) <= 20`，`type` 是 9 枚举之一。
- pass 模式同样必填 keywords，违规视为模型未遵循输出格式，走失败池。

## 6. validate_screen_result 改动

### 6.1 现有实现（line 270 附近）

```python
def validate_screen_result(analysis: Dict[str, Any]) -> Dict[str, Any]:
    action = ...
    reason = ...
    if action == "pass":
        return {"action": "pass", "reason": reason}

    categories = ...
    score = ...
    return {
        "action": "ingest",
        "reason": reason,
        "categories": categories[:3],
        "score": score,
    }
```

### 6.2 改动伪代码

新增一个内部辅助函数 `_validate_keywords(raw)`，在 ingest 与 pass 两条返回路径上各调用一次，把校验后的 list 透传到返回 dict。

```python
def _validate_keywords(raw: Any) -> List[Dict[str, str]]:
    if raw is None or not isinstance(raw, list):
        raise ValueError("missing keywords" if raw is None else "keywords must be list")

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


def validate_screen_result(analysis: Dict[str, Any]) -> Dict[str, Any]:
    action = ...
    reason = ...
    keywords = _validate_keywords(analysis.get("keywords"))

    if action == "pass":
        return {"action": "pass", "reason": reason, "keywords": keywords}

    categories = ...
    score = ...
    return {
        "action": "ingest",
        "reason": reason,
        "categories": categories[:3],
        "score": score,
        "keywords": keywords,
    }
```

### 6.3 校验顺序与失败信息

按以下顺序逐条校验，第一个失败就 `raise ValueError(...)`，错误信息建议保持英文短语，便于日志聚合：

1. `missing keywords`：字段缺失（`analysis.get("keywords") is None`）。
2. `keywords must be list`：字段存在但不是 list。
3. `keywords count out of range`：长度不在 [1, 3]。
4. `keyword item must be dict`：list 元素不是 dict。
5. `keyword name empty`：name 缺失或全空白。
6. `keyword name too long`：`len(name) > 20`（按 Python 字符串 len，等价 codepoint 数）。
7. `keyword type invalid: ...`：type 不在 `KEYWORD_TYPE_OPTIONS` 中。

### 6.4 长度计量约定

`len(name)` 使用 Python 默认字符串长度（codepoint 数），与 design 文档「字符按文本长度计算，英文、数字、空格、连字符和标点都计入长度」一致。emoji / 组合字符在阶段 0 不做特殊处理；如果实际数据出现 surrogate pair / ZWJ 序列引发 codepoint 偏差，留给后续阶段评估，不在 MVP 范围内打补丁。

### 6.5 KEYWORD_TYPE_OPTIONS 常量

`config.py` 新增：

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

放置位置建议在 `STATUS_OPTIONS` / `ITEM_ID_STRATEGY_OPTIONS` 这一类枚举集合附近，与现有风格一致。

## 7. 飞书字段定义

### 7.1 字段属性

NEWS 表（`FEISHU_NEWS_TABLE_ID`）：

- 字段名：`关键词`
- 字段类型：**多选**（与「分类」字段同款，不是关联，不是单选，不是文本）
- 选项管理：飞书 Bitable 多选字段在写入未知选项时**自动新增**选项；无需在飞书 UI 预配置选项列表，写入即可生效。

FILTERED 表（`FEISHU_FILTERED_TABLE_ID`）：

- 字段名：`关键词`
- 字段类型：**多选**（与 NEWS 完全一致）

### 7.2 config.py 新增常量

放置于 NEWS_FIELD_* / FILTERED_FIELD_* 常量分组内：

```python
NEWS_FIELD_KEYWORDS = "关键词"
FILTERED_FIELD_KEYWORDS = "关键词"
```

### 7.3 用户手动部署步骤

实施阶段在 plan 与 AGENTS.md 中提示用户：

1. 在飞书 NEWS 表上新增一列，列名输入 `关键词`，类型选「多选」，保存。
2. 在飞书 FILTERED 表上重复同样的操作。
3. 不需要预先添加任何选项；首次跑 `rss_ingest.py` 后，飞书会按写入的字符串自动建选项。

## 8. 写入路径改动

### 8.1 现有实现（line 1517 / 1553 附近）

`build_news_fields` 和 `build_filtered_fields` 都返回一个字段名 → 字段值的 dict，签名不需要改，只在返回 dict 里加一项。

### 8.2 改动伪代码

`build_news_fields`：

```python
def build_news_fields(article, analysis, item_key):
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
        kw["name"]
        for kw in keywords_raw
        if isinstance(kw, dict) and isinstance(kw.get("name"), str) and kw["name"].strip()
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

`build_filtered_fields`：在现有实现尾部以同样的方式加一行。

```python
def build_filtered_fields(article, analysis, item_key):
    base = build_article_base_fields(article, item_key)
    llm_meta = get_llm_meta(analysis)
    filter_method = ...
    filter_reason = ...

    keywords_raw = analysis.get("keywords") or []
    keyword_names = [
        kw["name"]
        for kw in keywords_raw
        if isinstance(kw, dict) and isinstance(kw.get("name"), str) and kw["name"].strip()
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

### 8.3 注意事项

- 函数签名不变，只是返回 dict 多一项。
- 字段值是 `List[str]`（多选字段的标准 payload 格式），飞书 API 自动处理新选项创建。
- type 信息**不写入**飞书字段，只在 `analysis` dict 里保留以备阶段 1 复用。
- `analysis.get("keywords") or []` 兜底：当上游因为某种异常没有透传 keywords（比如手工构造的 failed analysis），不应让 build 函数崩溃。校验失败的文章按现有失败链路本来就不会走到 build_news_fields，但 build_filtered_fields 可能拿到 `build_failed_analysis` 的产物（不带 keywords），这种情况下写一个空 list `[]`，飞书侧表现为该字段为空，符合预期。

## 9. 失败 / 降级路径

完全复用现有机制，不新增分支：

- LLM 返回非 JSON / 解析失败 → `analyze_with_provider_prompt` 已有处理。
- LLM 返回 JSON 但 keywords 字段缺失 / 越界 → `validate_screen_result` 抛 `ValueError` → `analyze_article` 在 `except ValueError as exc` 分支构造 `build_failed_analysis(f"screen: {exc}")` → 走 NVIDIA 重试 → fallback DeepSeek → 仍失败则进 `failed_items` 池。
- 飞书 NEWS 写入失败 → 复用现有 `feishu_create_failed` 计数与失败池复处理逻辑。
- 飞书 FILTERED 写入失败 → 复用现有 `filtered_log_failed` 计数。
- addendum 文件缺失 / 为空 → 启动期 `load_local_prompt_sections` 抛错，主流程不会启动；这是有意设计，确保 addendum 与 keywords 必填规则同步部署。

## 10. 测试覆盖

### 10.1 新增单元测试文件

#### `tests/test_validate_screen_result_keywords.py`

覆盖 `validate_screen_result` 在加 keywords 校验后的所有分支：

- `test_keywords_missing_raises`：analysis 不含 `keywords` 键 → `ValueError("missing keywords")`。
- `test_keywords_not_list_raises`：`keywords = "OpenAI"` → `ValueError("keywords must be list")`。
- `test_keywords_empty_list_raises`：`keywords = []` → `ValueError("keywords count out of range")`。
- `test_keywords_too_many_raises`：4 项 → `ValueError("keywords count out of range")`。
- `test_keywords_item_not_dict_raises`：`keywords = ["OpenAI"]` → `ValueError("keyword item must be dict")`。
- `test_keywords_name_empty_raises`：`{"name": "  ", "type": "org"}` → `ValueError("keyword name empty")`。
- `test_keywords_name_too_long_raises`：21 字符 name → `ValueError("keyword name too long")`。
- `test_keywords_invalid_type_raises`：`{"name": "X", "type": "company"}` → `ValueError(...)`（不在 9 枚举内）。
- `test_keywords_valid_ingest_passes_through`：合法 ingest 输入，返回 dict 含 `keywords` 列表，元素是 `{"name": ..., "type": ...}`。
- `test_keywords_valid_pass_passes_through`：合法 pass 输入（reason + keywords），返回 dict 含 `action: pass` + `reason` + `keywords`。
- `test_keywords_type_lowercased`：输入 `type = "Org"` 应被归一为 `"org"` 并通过校验（如果实现做了 `.lower()`，否则该测试改为期望失败——以最终实现为准，spec 推荐做 `.lower()`）。

#### `tests/test_build_news_fields_keywords.py`

覆盖 `build_news_fields` 与 `build_filtered_fields` 写入多选字段的行为：

- `test_keywords_written_to_news_field`：analysis 含 3 个合法 keywords → 返回 dict 中 `config.NEWS_FIELD_KEYWORDS` 等于 `["OpenAI", "ChatGPT", "成人模式"]`（仅 name，无 type）。
- `test_keywords_written_to_filtered_field`：同上，针对 `build_filtered_fields` 与 `config.FILTERED_FIELD_KEYWORDS`。
- `test_keywords_empty_when_missing`：analysis 不含 `keywords` 键 → 返回 dict 中字段值为 `[]`（兜底分支，验证不抛错）。
- `test_keywords_skip_invalid_items`：analysis 含 `[{"name": "", "type": "org"}, {"name": "OpenAI", "type": "org"}]` → 字段值为 `["OpenAI"]`（空 name 被过滤）。

### 10.2 端到端验证（手动）

不写自动化集成测试。实施阶段按以下步骤手动验证：

1. 用户先在飞书 NEWS 表与 FILTERED 表 UI 上各加一列「关键词」（多选）。
2. 跑 `.\.venv\Scripts\python.exe rss_ingest.py`。
3. 在飞书 NEWS 表上检查若干条新写入记录，确认「关键词」列有 chip，每条 1-3 个。
4. 在飞书 FILTERED 表上检查若干条 pass 记录，确认「关键词」列同样有 chip。
5. 检查 `out/` 目录下日志，确认有少量 `screen: ...` 失败记录可以接受（属于 LLM 偶发不遵守 schema），但比例不应高到吞掉大半文章；如果大面积失败，回头看 addendum 表述是否需要加固反例。

## 11. 实施顺序建议

留给 plan 阶段一个最小风险的起点（每一步都能独立验证、独立回滚）：

1. `config.py` 新增 `KEYWORD_TYPE_OPTIONS` / `LOCAL_SCREEN_KEYWORDS_ADDENDUM_PATH` / `NEWS_FIELD_KEYWORDS` / `FILTERED_FIELD_KEYWORDS`，更新 `rss-ingest-local.env.example`。
2. 落盘 `docs/local-screen-keywords-addendum.md`（按第 4.4 节草稿）。
3. 改 `load_local_prompt_sections` 拼接 addendum 到 screen_prompt。
4. 改 `validate_screen_result`，加 `_validate_keywords` 辅助函数与两条返回路径透传。
5. 写 `tests/test_validate_screen_result_keywords.py`，先把校验单测跑绿。
6. 改 `build_news_fields` 与 `build_filtered_fields`，加 keywords 字段。
7. 写 `tests/test_build_news_fields_keywords.py`，跑绿。
8. 提示用户在飞书 NEWS 表与 FILTERED 表上手动加「关键词」多选字段。
9. 跑一次完整 `rss_ingest.py` 端到端验证（飞书侧能看到关键词 chip）。
10. 在 `AGENTS.md` 的 Project Baseline / Local Workflow 区域补一句「关键词字段需要先在飞书 NEWS / FILTERED 表上手动加多选列」，保证下一个 agent 知道部署步骤。

## 12. DON'T（YAGNI 清单）

- 不写代码（spec 阶段只描述「应该怎么做」，不下手改 `rss_ingest.py` / `config.py`）。
- 不修改 `docs/local-screen-prompt.md`（绝对禁区，会扰动现有 score / categories 判断质量）。
- 不修改 `docs/local-summarize-prompt.md`（summary 阶段不参与抽词）。
- 不在 spec 阶段创建 `docs/local-screen-keywords-addendum.md`（留给实施阶段，spec 只给草稿）。
- 不在 spec 阶段创建 `tests/test_validate_screen_result_keywords.py` / `tests/test_build_news_fields_keywords.py`（同上）。
- 不建独立的 KEYWORD 表 / 双向关联字段。
- 不做归一化（NFKC / lower / dedup）。
- 不加进程内 cache / lock / `ensure_keyword`。
- 不做异步合并扫描 / `merge_keywords.py` / `--apply`。
- 不把 type 信息写入飞书字段。
- 不回填历史 NEWS / FILTERED。
- 不接 cron / scheduled task。
- 不 commit。
- 不加 emoji。
- 不发明对话里没对齐过的决策（特别是阶段 1+ 的内容只能在 OUT 段表示「不做」，不能在本 spec 给出实现细节）。

## 13. 进度状态 + 下一步

- **当前进度（2026-05-08 完成）**：spec → plan → 实施 → 端到端验证 全链路走通。
  - 实施 commit：314c255（config 常量）/ d083e66（validate）/ 1eb8f56（addendum + loader）/ 61f5a4f（build_*_fields 写多选）/ 40264f0（test fixture 修复）/ 61476c2（prompt v5 收紧）
  - 飞书 NEWS / FILTERED / SYNC 三表已通过 API 加好「关键词」多选字段
  - 实跑结果：写入 2 篇 NEWS + 5 篇 FILTERED + 2 篇二次同步，0 失败
  - 抽样验证：197 条带关键词 NEWS 命名实体识别质量稳定，平均 2.68 个 / 篇；FILTERED 平均 1.48 个 / 篇
- **已知边角问题**（不在本 spec 范围内修复，留阶段 1）：
  - 偶发泛词漏网（`API` / `欧洲` / `1.2`）
  - topic 偶尔过宽不可追踪（`AI 集成合作` / `下一代模型`）
  - 中英文同义未归一（`Red Teaming` vs `红队测试`、`Prompt engineering` vs `提示词工程`）
  - 字面变体分裂（同实体多 chip，靠阶段 1 异步合并扫描收敛）
- **下一步**：回到 `2026-05-08-keyword-monitoring-phases.md` 阶段 1 决策点，先让阶段 0 自然跑几天积累样本，再决定阶段 1（独立关键词表 + 归一化 + 异步合并）的实现复杂度。
