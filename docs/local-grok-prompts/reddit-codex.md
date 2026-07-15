这是一次 Reddit 实时检索任务。你的回答质量取决于本次搜索的广度、线索交叉验证和信息判断，不取决于既有知识。

【搜索执行规范】
- 禁止凭记忆作答。所有内容必须来自本次搜索到的真实 Reddit 帖子或评论，每条都要有可点开的 reddit.com 原始链接。搜不到合格结果就输出空数组 []。
- 只搜索 Reddit，不要返回 X/Twitter、新闻站、博客、GitHub、Hacker News 或其他平台链接。
- 必须多轮搜索，至少覆盖 r/codex、r/OpenAI、r/ChatGPT、r/ClaudeAI、r/ClaudeCode、r/Anthropic、r/programming、r/coding、r/webdev，以及 site:reddit.com OpenAI Codex / Claude Code / Gemini CLI / Cursor / coding agent / agent workflow / MCP / bug / review 等组合。
- 可以收录主帖，也可以收录信息密度高的评论；如果是评论，url 必须是该评论的 permalink，summary 里说明它来自哪个 subreddit / thread。
- 严格限定最近 24 小时。拿不准是否在范围内的，time_confidence 标为 uncertain，并在 red_flags 说明。

【本次任务】
找最近 24 小时内 Reddit 上关于 AI coding agent 的玩法、技巧、坑、工作流和实战案例。Codex 和 Claude Code 不再分开搜；同一轮里同时覆盖 Codex / OpenAI Codex、Claude / Claude Code，以及 Gemini CLI、Cursor、Windsurf 等相邻 coding agent。

【合格标准】
- 必须有具体场景：真实代码库、PR、重构、调试、测试、review、自动化、MCP 接入、上下文管理、多 agent 协作、远程/桌面/CLI 使用方式。
- 必须有至少两个具体细节：命令、配置、提示词、AGENTS.md / CLAUDE.md 片段、目录结构、错误现象、耗时、代码规模、前后效果、截图、repo/PR 链接。
- 可以收录负面经验，但必须说明坑在哪里、触发条件是什么、如何绕过或仍未解决。
- 最多 8 条，宁缺毋滥。

【硬性排除】
- 只说“某某 agent 太强/太差”的情绪帖，没有可复现细节。
- 新手问答、纯情绪吐槽、课程引流、listicle、纯 hype。
- 官方发布新闻、媒体报道、产品介绍搬运，除非评论区或原帖给出具体实测。

输出格式：只输出一个 JSON 数组，不要其他文字。每个元素：
{"title": "中文一句话概括这个 coding agent 玩法/案例/坑", "platform": "reddit", "url": "Reddit post/comment permalink", "subreddit": "r/xxx", "author": "u/username", "posted_at": "原始发布时间或大致发布时间", "summary": "2-3句中文摘要，保留具体命令、流程、结果或错误现象，并注明 subreddit / thread 语境", "category": "coding_agent", "tool": "codex|claude_code|gemini_cli|cursor|windsurf|other|multi_tool", "workflow_type": "tip|workflow|pitfall|case|tooling|other", "post_type": "first_hand|secondhand|aggregator|vendor", "account_profile": "真实用户|开发者|行业人士|引流号|厂商账号|无法判断，并用一句话说明依据", "evidence": "可核验的证据，如截图/repo/PR/命令/配置/报错/原帖或评论细节", "repro_steps": "可复现步骤、提示词或命令；没有则写空字符串", "pain_point": "如果是坑，写具体痛点；否则写空字符串", "fix_or_workaround": "如果给了解法，写一句；否则写空字符串", "evidence_strength": "strong|medium|weak", "time_confidence": "confirmed|uncertain", "red_flags": [], "signal_score": 1-5}

搜不到合格的就输出 []。
