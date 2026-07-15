这是一次 X 实时检索任务。你的回答质量取决于本次搜索的广度、线索交叉验证和信息判断，不取决于既有知识。

【搜索执行规范】
- 禁止凭记忆作答。所有内容必须来自本次搜索到的真实 X 帖子，每条都要有可点开的 x.com 帖子链接。搜不到合格结果就输出空数组 []，不要编造“看起来像 X 帖”的内容。
- 必须多轮搜索，不要一轮就停。自行设计至少 6-8 组不同角度的查询，包括 prompt、system prompt、agent skill、skill pack、AGENTS.md、CLAUDE.md、.cursorrules、Cursor rules、MCP config、workflow template、n8n AI workflow、Claude Code skill、Codex skill、Gemini CLI prompt 等中英文组合。
- 只收最近 24 小时内正在热起来、且能直接复用的 prompt / skill / 配置资产。拿不准发布时间的，time_confidence 标为 uncertain，并在 red_flags 里说明。
- 识别信息性质，而不是简单过滤信源。开发者一手发布、小账号实测、开源维护者分享的 prompt/skill pack 都可以收；聚合号和厂商宣传也可以收，但必须标注 post_type 和 evidence_strength。
- 输出前逐条自检：链接真实可点、内容与帖子对得上、资产能不能复用、发布时间在范围内或已标注存疑、证据字段不是空话。数量不够时宁可少而准，不凑数。

【本次任务】
用你的 X 实时搜索能力，找最近 24 小时内 X 上热门 prompt、热门 agent skill / skill pack、系统提示词和可复用 agent 配置。重点不是“技巧观点”，而是读者拿到后能复制、安装、改造或直接放进项目里的资产。

【什么算合格】
- 至少满足一个：给出 prompt 原文、system prompt、AGENTS.md / CLAUDE.md / .cursorrules 片段、skill / skill pack 入口、GitHub/Gist 链接、MCP 配置、安装命令、工作流模板、真实前后对比。
- 必须有热度或质量信号：单帖互动明显高、多人转发/复测、短时间多个独立账号讨论、GitHub star/issue/PR 活跃、作者是维护者、评论区有人复现。
- 中文帖和英文帖都要搜，最多 8 条，宁缺毋滥。

【优先级】
1. 可直接复制的高质量 prompt / system prompt，且解决明确问题。
2. Agent skill / skill pack / AGENTS.md / CLAUDE.md / Cursor rules 等可复用配置。
3. MCP、workflow template、自动化编排、prompt pack，且有 repo、配置或完整步骤。

【硬性排除】
- “10 个 AI 神器 / 100 个 prompt”式 listicle，除非其中某个资产有独立高热度和完整原文。
- 只说“太强了/收藏了/改变工作流”但不给 prompt、配置、repo 或入口的帖子。
- 课程、社群、付费资料包、Newsletter 引流。
- 营销矩阵帖、引流闸门帖、全大写+火箭 emoji hype 帖。
- 新闻媒体、转载、无实测的 GitHub trending 搬运。

输出格式：只输出一个 JSON 数组，不要其他文字。每个元素：
{"title": "中文一句话概括这个 prompt/skill 解决什么问题", "platform": "x", "url": "帖子链接", "author": "@handle", "posted_at": "原始发布时间或大致发布时间", "summary": "2-3句中文摘要，说明资产是什么、怎么用、为什么值得看", "category": "prompt_skill", "asset_type": "prompt|system_prompt|skill|skill_pack|agent_config|workflow|mcp|other", "post_type": "first_hand|secondhand|aggregator|vendor", "account_profile": "真实用户|开发者|行业人士|营销号|厂商账号|无法判断，并用一句话说明依据", "evidence": "可核验的证据，如 prompt 原文/GitHub 链接/安装命令/demo/配置片段/复测截图/互动异常", "target_user": "适合谁使用", "reuse_entry": "拿到后第一步怎么用，如复制到哪里、clone 什么 repo、改哪个配置；没有则写空字符串", "why_hot": "一句话说明热度或质量信号", "evidence_strength": "strong|medium|weak", "time_confidence": "confirmed|uncertain", "red_flags": [], "signal_score": 1-5}

搜不到合格的就输出空数组 []，不要为了凑数收录泛泛资源合集。
