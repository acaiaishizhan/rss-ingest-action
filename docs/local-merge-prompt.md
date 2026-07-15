# RSS KEYWORD 自动合并判断提示词

判断同一个 `candidate_group` 里的关键词是否指向同一个对象。

对象可以是实体、产品、模型、工具、公司、概念或事件。

## 输入

你会收到一个 JSON：

{
  "group_id": "g_001",
  "candidates": [
    {
      "keyword_id": "kw_123",
      "name": "Claude Code",
      "type": "product",
      "aliases": [],
      "news_count": 12,
      "filtered_count": 3,
      "last_seen_at": "2026-05-11T13:30:00+08:00",
      "sample_titles": [
        "Anthropic 发布 Claude Code 新功能",
        "开发者比较 Claude Code 与 Codex"
      ]
    }
  ]
}

字段说明：

- `keyword_id`：已有 KEYWORD 记录 ID，不要创造新 ID。
- `name`：关键词规范名或候选名。
- `type`：关键词类型。
- `aliases`：已知别名。
- `news_count`：出现在 NEWS 表的次数，代表更有价值的出现。
- `filtered_count`：出现在 FILTERED 表的次数，代表噪音或低价值出现。
- `last_seen_at`：最近出现时间。
- `sample_titles`：最近样本标题，只用于判断语境，不要被标题主题带偏；不能因为两个关键词经常出现在同一条新闻里，就判定它们可以合并。

## 判断目标

只允许合并“同一对象的不同写法”。

可以合并：

- 大小写差异：`claude code` / `Claude Code`
- 空格差异：`Cherry Studio` / `CherryStudio`
- 标点差异：`GPT-5.5` / `gpt5.5`
- 全半角差异
- 明确同义缩写，且样本标题支持
- 明确中英文别名，且样本标题支持

必须跳过：

- 上位词 / 下位词
- 公司 / 产品
- 产品 / CLI 工具
- 模型大版本 / 小版本
- 系列名 / 具体版本
- 泛概念 / 具体事件
- 语义相近但不是同一对象
- 样本标题不足以证明同一对象
- `type` 不一致。LLM 不得自行判断旧数据误标，不得用 reason 覆盖 type 冲突。

## 强制 skip 示例

- `Claude` 与 `Claude Code`：skip。前者是模型、品牌或助手泛称，后者是具体编码工具。
- `OpenAI` 与 `ChatGPT`：skip。前者是公司，后者是产品。
- `GPT-5` 与 `GPT-5.5`：skip。不同模型版本，不可合并。
- `Gemini` 与 `Gemini CLI`：skip。前者是模型或产品族，后者是具体命令行工具。
- `Anthropic` 与 `Claude`：skip。前者是公司，后者是产品或模型。
- `AI` 与 `AI Agent`：skip。前者是泛概念，后者是子概念。
- `ChatGPT Agent` 与 `AI Agent`：skip。前者是具体产品或能力名，后者是泛概念。

## canonical 选择规则

从候选里选一个已有 keyword 作为 canonical。

优先选择：

1. 名称更清晰、更接近官方写法的 keyword。
2. 大小写、空格、标点更自然的 keyword。
3. `news_count` 更高的 keyword。
4. `last_seen_at` 更新的 keyword。

不要创造新的 canonical 名称。
不要创造新的 keyword_id。

## 输出要求

只输出严格 JSON。
不要输出 Markdown。
不要输出注释。
不要输出多余字段。

输出形状示例如下。

调用层必须使用 JSON Schema / Pydantic / Zod 做严格校验。校验失败时，本组结果视为 skip，禁止自动合并。

所有字段必须存在；不得出现 schema 外字段；enum 值必须完全匹配；数组里的 `keyword_id` 必须全部来自输入 `candidates`。

如果无法满足上述结构，输出 `decision = "skip"`，不要尝试解释。

示例：

{
  "group_id": "g_001",
  "decision": "merge",
  "confidence": 0.98,
  "risk": "low",
  "canonical_id": "kw_123",
  "items": [
    {
      "keyword_id": "kw_123",
      "name": "Claude Code",
      "action": "canonical",
      "reason": "规范写法，NEWS 出现更多"
    },
    {
      "keyword_id": "kw_456",
      "name": "claudecode",
      "action": "merge_to_canonical",
      "reason": "仅大小写和空格差异，样本均指向 Claude Code 工具"
    }
  ],
  "merge_ids": ["kw_456"],
  "skip_ids": [],
  "force_skip_reason": ""
}

字段规则：

- `decision`：只能是 `merge` 或 `skip`。
- `confidence`：0 到 1 的数字。
- `risk`：只能是 `low`、`medium`、`high`。
- `canonical_id`：如果 `decision = merge`，必须是候选里的一个 `keyword_id`；如果 `decision = skip`，填空字符串。
- `items[].action`：只能是 `canonical`、`merge_to_canonical`、`skip`。
- `merge_ids`：所有要合并到 canonical 的 keyword_id。
- `skip_ids`：所有明确跳过的 keyword_id。
- `force_skip_reason`：当必须跳过时，写一句原因；否则填空字符串。
- `items[].reason`：不超过 40 个中文字符。

v1 采用整组 all-or-nothing 判断。

当 `decision = "merge"` 时：

- `items` 中必须且只能有 1 个 `action = "canonical"`。
- `merge_ids` 必须非空。
- `skip_ids` 必须为空数组。
- `candidates` 里的所有 `keyword_id` 必须恰好等于 `canonical_id + merge_ids`。
- 组内不得存在任何 `action = "skip"`。

只要组内任何候选不应合并，整个 group 输出 `decision = "skip"`。

置信度校准：

- `confidence >= 0.95` 只用于大小写、空格、标点、全半角、明确官方别名等低风险写法差异。
- 只要需要语义推断，`confidence` 不得超过 `0.89`。
- 存在上位词 / 下位词、公司 / 产品、版本差异、工具差异、样本不足时，必须 `decision = "skip"`。

## 自动合并口径

只有满足以下条件时，系统才会自动合并：

- `decision = "merge"`
- `confidence >= 0.95`
- `risk = "low"`
- `merge_ids` 非空
- 所有 `merge_ids` 都只是同一对象的低风险写法差异
- 没有上位词 / 下位词、公司 / 产品、版本差异、工具差异
- 没有样本不足问题

只要有任何不确定，输出 `decision = "skip"`。

## 输出示例：可以合并

{
  "group_id": "g_claude_code",
  "decision": "merge",
  "confidence": 0.98,
  "risk": "low",
  "canonical_id": "kw_claude_code",
  "items": [
    {
      "keyword_id": "kw_claude_code",
      "name": "Claude Code",
      "action": "canonical",
      "reason": "规范写法，样本标题均指向 Anthropic 编码工具"
    },
    {
      "keyword_id": "kw_claudecode",
      "name": "claudecode",
      "action": "merge_to_canonical",
      "reason": "仅大小写和空格不同，语义相同"
    }
  ],
  "merge_ids": ["kw_claudecode"],
  "skip_ids": [],
  "force_skip_reason": ""
}

## 输出示例：必须跳过

{
  "group_id": "g_claude",
  "decision": "skip",
  "confidence": 0.99,
  "risk": "high",
  "canonical_id": "",
  "items": [
    {
      "keyword_id": "kw_claude",
      "name": "Claude",
      "action": "skip",
      "reason": "模型、品牌或助手泛称"
    },
    {
      "keyword_id": "kw_claude_code",
      "name": "Claude Code",
      "action": "skip",
      "reason": "具体编码工具，不等同于 Claude"
    }
  ],
  "merge_ids": [],
  "skip_ids": ["kw_claude", "kw_claude_code"],
  "force_skip_reason": "上位词 / 下位词，不允许自动合并"
}

