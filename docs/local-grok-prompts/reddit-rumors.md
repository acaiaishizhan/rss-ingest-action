这是一次 Reddit 实时检索任务。只搜索 Reddit，找最近 24 小时内 AI 行业小道消息：未官宣的产品/模型动向、内部传闻、泄露信息、融资/收购风声。

要求：所有结果必须是 reddit.com/r/.../comments/... 的主帖或评论 permalink；优先 r/OpenAI、r/ClaudeAI、r/Anthropic、r/LocalLLaMA、r/singularity、r/artificial。必须有明确可证伪声明和信源链路；已官宣新闻不要收，除非评论区出现新的未确认线索。排除无来源“据说”、个人预测、标题党。

输出格式：只输出 JSON 数组，最多 8 条。每个元素：
{"title": "中文一句话概括传闻内容", "platform": "reddit", "url": "Reddit post/comment permalink", "subreddit": "r/xxx", "author": "u/username", "posted_at": "原始发布时间或大致发布时间", "claim": "具体声明了什么", "source_chain": "信源链路：谁说的+什么身份/依据", "corroboration": "single_source|multi_source", "summary": "2-3句中文摘要", "category": "rumor", "post_type": "first_hand|secondhand|aggregator|vendor", "account_profile": "真实用户|行业人士|引流号|厂商账号|无法判断，并用一句话说明依据", "evidence": "可核验证据，如截图/页面线索/多源交叉/原帖或评论细节", "evidence_strength": "strong|medium|weak", "time_confidence": "confirmed|uncertain", "signal_score": 1-5, "red_flags": []}

搜不到合格的就输出 []。
