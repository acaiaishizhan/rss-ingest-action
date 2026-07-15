# 提示词索引

程序默认不再读取这个总文件，而是分别读取下面 3 个文件：

1. `docs/local-keyword-blocklist.txt`
2. `docs/local-screen-prompt.md`（v2：一次调用完成去噪 + 评分 + 分类 + 标题 + 摘要 + keywords + QA）
3. `docs/local-summarize-prompt.md`（fallback：仅在 screen 未输出 qa 时调用）

如果你只想改关键词黑名单，就改第 1 个文件。
如果你想改 screen 阶段的去噪规则、评分档位、分类标签、标题/摘要/关键词/QA 逻辑，改第 2 个文件。
第 3 个文件是旧版 QA 提示词，v2 合并后仅作为 fallback 保留。

只有在代码里显式传入这个文件路径时，才会按旧的单文件格式解析。
