这是一次 Reddit 实时检索任务。只搜索 Reddit，找最近 24 小时内 AI 圈讨论度突然升高、值得做选题的 Reddit thread。

要求：所有结果必须是 reddit.com/r/.../comments/... 的主帖 permalink；优先 r/OpenAI、r/ChatGPT、r/ClaudeAI、r/LocalLLaMA、r/singularity、r/artificial、r/programming。必须有明确讨论焦点和热度信号（高分、高评论、争议、多个 subreddit 同时出现）。排除普通新闻搬运、低信息量吐槽、课程引流。

输出格式：只输出 JSON 数组，最多 8 条。每个元素：
{"title": "中文一句话概括爆点", "platform": "reddit", "url": "Reddit post permalink", "subreddit": "r/xxx", "author": "u/username", "posted_at": "原始发布时间或大致发布时间", "summary": "2-3句中文摘要，说明争论点和选题角度", "category": "burst", "heat_stage": "emerging|hot|cooling", "content_angle": "可转成内容的切入点", "post_type": "first_hand|secondhand|aggregator|vendor", "account_profile": "真实用户|开发者|行业人士|引流号|厂商账号|无法判断，并用一句话说明依据", "evidence": "热度证据，如分数/评论数/多 subreddit 交叉/原帖细节", "evidence_strength": "strong|medium|weak", "time_confidence": "confirmed|uncertain", "red_flags": [], "signal_score": 1-5}

搜不到合格的就输出 []。
