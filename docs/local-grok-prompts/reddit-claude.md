这是一次 Reddit 实时检索任务。只搜索 Reddit，找最近 24 小时内热门 prompt、热门 agent skill / skill pack、系统提示词和可复用 agent 配置。

【搜索执行规范】
- 禁止凭记忆作答。所有内容必须来自本次搜索到的真实 Reddit 帖子或评论，每条都要有可点开的 reddit.com 原始链接。搜不到合格结果就输出空数组 []。
- 只搜索 Reddit，不要返回 X/Twitter、新闻站、博客、Hacker News 或其它平台链接；外链 repo / gist / demo 可以作为 evidence，但主 url 必须是 Reddit permalink。
- 必须多轮搜索，优先覆盖 r/ChatGPT、r/OpenAI、r/ClaudeAI、r/ClaudeCode、r/codex、r/LocalLLaMA、r/artificial、r/programming、r/coding、r/webdev、r/productivity、r/selfhosted、r/opensource、r/SideProject。
- 可以收录主帖，也可以收录信息密度高的评论；如果是评论，url 必须是该评论的 permalink，summary 里说明它来自哪个 subreddit / thread。
- 严格限定最近 24 小时。拿不准是否在范围内的，time_confidence 标为 uncertain，并在 red_flags 说明。

【本次任务】
找最近 24 小时内 Reddit 上热门 prompt、热门 agent skill / skill pack、系统提示词、AGENTS.md / CLAUDE.md / Cursor rules、MCP 配置和可复用 workflow。重点不是“技巧观点”，而是读者拿到后能复制、安装、改造或直接放进项目里的资产。

【合格标准】
- 至少满足一个：给出 prompt 原文、system prompt、AGENTS.md / CLAUDE.md / .cursorrules 片段、skill / skill pack 入口、GitHub/Gist 链接、MCP 配置、安装命令、工作流模板、真实前后对比。
- 必须有热度或质量信号：高分、高评论、多人复测、作者是维护者、评论区有人给出使用反馈、短时间多个 subreddit 同时出现。
- 最多 8 条，宁缺毋滥。

【优先级】
1. 可直接复制的高质量 prompt / system prompt，且解决明确问题。
2. Agent skill / skill pack / AGENTS.md / CLAUDE.md / Cursor rules 等可复用配置。
3. MCP、workflow template、自动化编排、prompt pack，且有 repo、配置或完整步骤。

【硬性排除】
- “我用 AI 做了 X”但不分享具体 prompt/配置的帖子。
- 纯讨论帖，无可复用资产。
- “10 个技巧 / 100 个 prompt”式 listicle，除非资产本身完整可复用。
- 课程推广、付费工具广告、社群引流、低信息量吐槽。

输出格式：只输出 JSON 数组，不要其他文字。每个元素：
{"title": "中文一句话概括这个 prompt/skill 解决什么问题", "platform": "reddit", "url": "Reddit post/comment permalink", "subreddit": "r/xxx", "author": "u/username", "posted_at": "原始发布时间或大致发布时间", "summary": "2-3句中文摘要，说明资产是什么、怎么用、为什么值得看", "category": "prompt_skill", "asset_type": "prompt|system_prompt|skill|skill_pack|agent_config|workflow|mcp|other", "post_type": "post|comment|crosspost|vendor", "account_profile": "真实用户|开发者|开源维护者|行业人士|引流号|厂商账号|无法判断，并用一句话说明依据", "evidence": "可核验证据，如 prompt 原文/GitHub 链接/安装命令/demo/配置片段/评论复测/热度数据", "target_user": "适合谁使用", "reuse_entry": "拿到后第一步怎么用，如复制到哪里、clone 什么 repo、改哪个配置；没有则写空字符串", "why_hot": "一句话说明热度或质量信号", "evidence_strength": "strong|medium|weak", "time_confidence": "confirmed|uncertain", "red_flags": [], "signal_score": 1-5}

搜不到合格的就输出 []。
