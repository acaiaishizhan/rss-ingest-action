# 提示词索引

程序默认不再读取这个总文件，而是分别读取下面 4 个文件：

1. `docs/local-keyword-blocklist.txt`
2. `docs/local-screen-triage-prompt.md`（三态高召回初筛 + 信号评分；低于 `TRIAGE_MIN_SCORE` 时直接过滤）
3. `docs/local-screen-prompt.md`（内容处理；生成内容评分 + 分类 + 标题 + 摘要 + keywords + QA，并只对 uncertain 做终审；内容评分不再次决定去留）
4. `docs/local-summarize-prompt.md`（fallback：仅在内容处理未输出 qa 时调用）

如果你只想改关键词黑名单，就改第 1 个文件。
如果你想改第一道闸门的召回、分流或信号评分，改第 2 个文件；修改终审去噪、内容评分、分类标签、标题/摘要/关键词/QA 逻辑，改第 3 个文件。
第 4 个文件是旧版 QA 提示词，仅作为 fallback 保留。

只有在代码里显式传入这个文件路径时，才会按旧的单文件格式解析。
