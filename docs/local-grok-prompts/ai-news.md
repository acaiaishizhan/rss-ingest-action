这是一次 X 实时检索任务。你的回答质量取决于本次搜索的广度、线索交叉验证和信息判断，不取决于既有知识。

【搜索执行规范】
- 禁止凭记忆作答。所有内容必须来自本次搜索到的真实 X 帖子，每条都要有可点开的 x.com 原始链接。搜不到合格结果就输出空数组 []。
- 只找纯 AI 新闻：模型发布、产品发布、融资收购、政策监管、重大合作、公司人事、开源项目重大版本、安全事故、价格/额度/可用性变化。不要收技巧帖、教程帖、普通观点帖、泛泛行业评论。
- 只搜索 X。链接必须是 x.com status，不要返回 Reddit、新闻站、博客、GitHub、Hacker News 或其它平台链接；这些外链只能作为 evidence。
- 严格限定过去 10 小时内。发布时间拿不准的，time_confidence 标为 uncertain，并在 red_flags 说明；超过 10 小时的不要收。
- 优先收一手来源或靠近一手来源的帖子：官方账号、员工、记者、研究员、开源维护者、社区中贴出原始链接/截图/公告链接的人。
- 硬排除：纯 hype、预测、课程/Newsletter 引流、重复搬运、没有新闻事实的新手问答。

【本次任务】
找过去 10 小时内 X 上的纯 AI 新闻。最多 6 条，宁缺毋滥。

输出格式：只输出一个 JSON 数组，不要其他文字。每个元素：
{"title": "中文一句话新闻标题", "platform": "x", "url": "x.com status", "author": "@handle", "posted_at": "原始发布时间或大致发布时间", "summary": "2-3句中文摘要，说明新闻事实、涉及实体和为什么重要", "category": "ai_news", "news_type": "model|product|funding|policy|partnership|people|opensource|security|pricing|other", "post_type": "first_hand|secondhand|aggregator|vendor", "account_profile": "官方|员工|记者|研究员|开发者|社区用户|聚合号|无法判断，并用一句话说明依据", "evidence": "可核验依据，如公告链接/截图/原始论文/发布页/多源交叉/原帖细节", "evidence_strength": "strong|medium|weak", "time_confidence": "confirmed|uncertain", "red_flags": [], "signal_score": 1-5}

搜不到合格的就输出空数组 []。
