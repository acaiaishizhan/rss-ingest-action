这是一次 Reddit 实时检索任务。只搜索 Reddit，找最近 24 小时内 AI 实战案例：用 AI 赚钱/接单/做产品，或用 AI 完成具体工作/项目。

要求：所有结果必须是 reddit.com/r/.../comments/... 的主帖或评论 permalink；优先 r/Entrepreneur、r/SaaS、r/SideProject、r/ChatGPT、r/OpenAI、r/programming、r/coding。必须是一手或可验证分享，并至少有两个具体细节：金额、定价、客户来源、工具栈、步骤、截图、repo、产品页。排除爽文、课程社群、DM 引流和第三人称搬运。

输出格式：只输出 JSON 数组，最多 8 条。每个元素：
{"title": "中文一句话概括", "platform": "reddit", "url": "Reddit post/comment permalink", "subreddit": "r/xxx", "author": "u/username", "posted_at": "原始发布时间或大致发布时间", "summary": "2-3句中文摘要，保留具体数字和操作", "category": "case", "post_type": "first_hand|secondhand|aggregator|vendor", "account_profile": "真实用户|开发者|行业人士|引流号|厂商账号|无法判断，并用一句话说明依据", "evidence": "可核验证据，如金额截图/GitHub/产品页/后台截图/原帖或评论细节", "evidence_strength": "strong|medium|weak", "time_confidence": "confirmed|uncertain", "red_flags": [], "signal_score": 1-5}

搜不到合格的就输出 []。
