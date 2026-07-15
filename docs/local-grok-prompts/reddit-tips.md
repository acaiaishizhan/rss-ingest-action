这是一次 Reddit 实时检索任务。只搜索 Reddit，找最近 24 小时内 AI 实用技巧帖：prompt 技巧、工作流配置、工具组合用法。

要求：所有结果必须是 reddit.com/r/.../comments/... 的主帖或评论 permalink；优先 r/ChatGPT、r/OpenAI、r/ClaudeAI、r/LocalLLaMA、r/ArtificialInteligence、r/productivity、r/programming。必须有可复现步骤、prompt 原文、配置、截图或代码；排除“10 个神器”式 listicle、课程引流、纯工具推荐。

输出格式：只输出 JSON 数组，最多 8 条。每个元素：
{"title": "中文一句话概括", "platform": "reddit", "url": "Reddit post/comment permalink", "subreddit": "r/xxx", "author": "u/username", "posted_at": "原始发布时间或大致发布时间", "summary": "2-3句中文摘要，保留关键步骤", "category": "tip", "post_type": "first_hand|secondhand|aggregator|vendor", "account_profile": "真实用户|开发者|行业人士|引流号|厂商账号|无法判断，并用一句话说明依据", "evidence": "可复现性证据，如 prompt 原文/代码/配置/截图/原帖或评论细节", "evidence_strength": "strong|medium|weak", "time_confidence": "confirmed|uncertain", "red_flags": [], "signal_score": 1-5}

搜不到合格的就输出 []。
