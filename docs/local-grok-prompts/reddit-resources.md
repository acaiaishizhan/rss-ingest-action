这是一次 Reddit 实时检索任务。只搜索 Reddit，找最近 24 小时内高热度且真的好用的 AI 资源：prompt、agent skill / skill pack、开源项目、模板仓库、可复用工作流。

要求：所有结果必须是 reddit.com/r/.../comments/... 的主帖或评论 permalink；优先 r/LocalLLaMA、r/OpenAI、r/ChatGPT、r/ClaudeAI、r/programming、r/selfhosted、r/opensource。必须有资源链接、使用场景和至少一个可验证细节（GitHub、demo、配置、截图、用户反馈）。排除资源合集、水贴、Newsletter 引流。

输出格式：只输出 JSON 数组，最多 8 条。每个元素：
{"title": "中文一句话概括资源价值", "platform": "reddit", "url": "Reddit post/comment permalink", "subreddit": "r/xxx", "author": "u/username", "posted_at": "原始发布时间或大致发布时间", "summary": "2-3句中文摘要，说明资源是什么、适合谁、怎么用", "category": "resource", "resource_type": "prompt|repo|template|workflow|skill|dataset|tool|other", "target_user": "适合的人群", "post_type": "first_hand|secondhand|aggregator|vendor", "account_profile": "真实用户|开发者|行业人士|引流号|厂商账号|无法判断，并用一句话说明依据", "evidence": "可核验证据，如 GitHub/demo/配置/截图/评论反馈", "evidence_strength": "strong|medium|weak", "time_confidence": "confirmed|uncertain", "red_flags": [], "signal_score": 1-5}

搜不到合格的就输出 []。
