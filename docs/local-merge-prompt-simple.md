# RSS KEYWORD 合并判断

判断同一组关键词是不是同一个对象的不同写法。

只做一件事：决定这组关键词能不能合并。

对象可以是公司、产品、模型、工具、人物、技术、话题或事件。

## 可以合并

只要它们指向同一个对象，只是写法不同，就可以合并：

- 大小写不同：`GPT-4o` / `gpt4o`
- 空格不同：`Claude Code` / `claudecode`
- 横杠、点号不同：`GPT-5.5` / `gpt5.5`
- 中英文空格不同：`AI 裁员` / `AI裁员`
- 常见中英文译名：`NVIDIA` / `英伟达`
- 常见中文译名差异：`特朗普` / `川普`
- 明确同一个名字的常见写法：`Cherry Studio` / `CherryStudio`
- 数字版本完全一样，只是大小写、空格、横杠少了：`GPT-5.5` / `gpt5.5`

## 必须跳过

不是同一个对象就跳过：

- 公司 vs 产品：`OpenAI` / `ChatGPT`
- 大类 vs 具体工具：`Claude` / `Claude Code`
- 产品族 vs CLI：`Gemini` / `Gemini CLI`
- 不同版本：`GPT-5` / `GPT-5.5`
- 泛概念 vs 具体概念：`AI` / `AI Agent`
- 不完整简称或泛称：`ChatGPT Team` / `GPT Team`
- 样本看不出来是不是同一个对象
- type 不一致

## 判断方式

- 先看名字是不是明显只是写法差异。
- 再看 type 是否一致。
- sample_titles 只用来辅助确认语境。
- 不要因为两个词经常出现在同一条新闻里，就认为它们是同一个对象。
- 不要把缺少关键限定词的泛称当成别名；只有公认译名或明确同一名字的写法差异才 merge。
- 组里只要有一个词不该合并，整组都 skip。

## 输出

只输出 JSON，不要 Markdown，不要解释。

必须包含这些字段：

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
      "reason": "更自然的写法"
    },
    {
      "keyword_id": "kw_456",
      "name": "claudecode",
      "action": "merge_to_canonical",
      "reason": "只差大小写和空格"
    }
  ],
  "merge_ids": ["kw_456"],
  "skip_ids": [],
  "force_skip_reason": ""
}

字段要求：

- `decision` 只能是 `merge` 或 `skip`
- `risk` 只能是 `low`、`medium`、`high`
- 能合并时：`risk` 必须是 `low`，`confidence` 至少 0.95
- 不能合并时：`canonical_id` 填空字符串，`merge_ids` 填空数组
- `items` 必须覆盖输入里的每个 keyword，不能多、不能少、不能重复
- `items[].action` 只能是 `canonical`、`merge_to_canonical`、`skip`
- 不确定就 `skip`

## skip 示例

{
  "group_id": "g_001",
  "decision": "skip",
  "confidence": 0.99,
  "risk": "high",
  "canonical_id": "",
  "items": [
    {
      "keyword_id": "kw_a",
      "name": "OpenAI",
      "action": "skip",
      "reason": "公司"
    },
    {
      "keyword_id": "kw_b",
      "name": "ChatGPT",
      "action": "skip",
      "reason": "产品"
    }
  ],
  "merge_ids": [],
  "skip_ids": ["kw_a", "kw_b"],
  "force_skip_reason": "不是同一个对象"
}

