这是一次 X 实时检索任务。你的回答质量取决于本次搜索的广度、线索交叉验证和信息判断，不取决于既有知识。

【搜索执行规范】
- 禁止凭记忆作答。所有内容必须来自本次搜索到的真实 X 帖子，每条都要有可点开的 x.com 帖子链接。搜不到合格结果就输出空数组 []，不要编造“看起来像 X 帖”的内容。
- 必须多轮搜索，不要一轮就停。自行设计至少 6-8 组不同角度的查询，覆盖 Codex / OpenAI Codex、Claude Code / Claude Desktop、Gemini CLI、Cursor、Windsurf、coding agent、agent workflow、MCP、AGENTS.md、CLAUDE.md、review、debug、CI、坑、workflow、case study 等中英文组合。
- 识别信息性质，而不是简单过滤信源。不要因为发帖人不是官方或大 V 就丢弃；真实项目里的踩坑、工作流截图、小账号实测可能更有价值。你的职责是判断并标注：这是当事人一手经验、转述、聚合整理，还是官方/厂商宣传？账号画像是真实用户、开发者、行业人士、营销号，还是无法判断？
- 时效从严。严格限定最近 24 小时，保留每条原始发布时间。拿不准是否在范围内的，time_confidence 标为 uncertain，并在 red_flags 里说明。
- 输出前逐条自检：链接真实可点、内容与帖子对得上、发布时间在范围内或已标注存疑、信息性质没有漏标、证据字段不是空话。数量不够时宁可少而准，不凑数。

【本次任务】
用你的 X 实时搜索能力，找最近 24 小时内 X 上关于 AI coding agent 的玩法、技巧、坑、工作流和实战案例。Codex 和 Claude Code 不再分开搜；同一轮里同时覆盖 Codex / OpenAI Codex、Claude / Claude Code，以及 Gemini CLI、Cursor、Windsurf 等相邻 coding agent。

【什么算合格】
- 必须有具体场景：真实代码库、PR、重构、调试、测试、review、自动化、MCP 接入、上下文管理、多 agent 协作、远程/桌面/CLI 使用方式。
- 必须有至少两个具体细节：命令、配置、提示词、AGENTS.md / CLAUDE.md 片段、MCP server、目录结构、错误现象、耗时、代码规模、前后效果、截图、repo/PR 链接。
- 可以收录负面经验，但必须说明坑在哪里、触发条件是什么、如何绕过或仍未解决。
- 最多 8 条，中文和英文都要搜，宁缺毋滥。

【优先级】
1. 一手实战案例：作者用 Codex、Claude Code 或其它 coding agent 完成真实项目、PR、迁移、测试、debug，并给出过程或结果。
2. 可复用工作流：AGENTS.md、CLAUDE.md、MCP、subagents、多模型协作、git worktree、code review、CI 修复、远程任务等组合方式。
3. 真实踩坑：上下文丢失、误改文件、测试跑不动、权限/沙箱/登录态/工具链问题、速率限制、模型选择，以及明确 workaround。

【硬性排除】
- 只说“某某 agent 太强/太差”的情绪帖，没有可复现细节。
- 官方发布新闻、媒体报道、产品介绍搬运，除非评论区或原帖给出具体实测。
- “AI coding agent 排行榜/十大神器”式 listicle。
- 课程、社群、Newsletter 引流。
- 营销矩阵帖、引流闸门帖、全大写+火箭 emoji hype 帖。

输出格式：只输出一个 JSON 数组，不要其他文字。每个元素：
{"title": "中文一句话概括这个 coding agent 玩法/案例/坑", "url": "帖子链接", "author": "@handle", "posted_at": "原始发布时间或大致发布时间", "summary": "2-3句中文摘要，保留具体命令、流程、结果或错误现象", "category": "coding_agent", "tool": "codex|claude_code|gemini_cli|cursor|windsurf|other|multi_tool", "workflow_type": "tip|workflow|pitfall|case|tooling|other", "post_type": "first_hand|secondhand|aggregator|vendor", "account_profile": "真实用户|开发者|行业人士|营销号|厂商账号|无法判断，并用一句话说明依据", "evidence": "可核验的证据，如截图/repo/PR/MCP配置/命令/报错/原帖细节", "repro_steps": "可复现步骤、提示词或命令；没有则写空字符串", "pain_point": "如果是坑，写具体痛点；否则写空字符串", "fix_or_workaround": "如果给了解法，写一句；否则写空字符串", "evidence_strength": "strong|medium|weak", "time_confidence": "confirmed|uncertain", "red_flags": [], "signal_score": 1-5}

搜不到合格的就输出空数组 []，不要把普通 AI 编程讨论硬归为 coding agent 玩法。
