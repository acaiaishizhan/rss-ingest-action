# 关键词监控阶段计划

本文档为 `rss-ingest-local` 项目「关键词监控」feature 的阶段计划，作为后续 spec、implementation plan、code review 的共识起点。本文档不是 spec，也不是 implementation plan，每个阶段的具体设计仍需独立 brainstorm。

## 1. 前置共识

本文档延续此前对话已对齐的 4 个核心决策：

- 管线位置：并入 `screen` stage（不动 `summary` stage）
- 覆盖范围：全样本（ingest 与 pass 都参与抽词）
- NEWS / FILTERED 表上的原始关键词字段类型：使用 Feishu Bitable 多选字段 `关键词`
- 阶段 1 关联字段：NEWS / FILTERED 另设 `关键词记录` 关联字段，指向 KEYWORD 表
- 归一化策略：程序侧 NFKC + lower 的 exact match + KEYWORD 表 `归一项` 真实别名 list + 异步 LLM 周期扫描合并

注意：阶段 0 MVP 最终采用 `关键词` 多选字段先落地；阶段 1 在此基础上新增 KEYWORD 表和 `关键词记录` 关联字段，保留原始 chip 便于人工查看。

抽取规则的细节（1-3 个 / ≤20 字符 / 9 类枚举 / 别名归一）以 `2026-05-08-keyword-monitoring-design.md` 为准，本文档不复述。

## 2. 设计原则

- 每阶段的终态都「有用且能停下」——下一阶段不强制进入，可在任意阶段决策点暂停或终止。
- 破坏性改动（飞书 schema 改动、关联关系建立、合并删除）集中在阶段 1 与阶段 3，前置阶段保持只读 / 只增。
- 每阶段结束有明确决策点，决定「继续 / 暂停 / 终止」，并以可观测的产物作为决策依据。

## 3. 阶段总览

| 阶段 | 范围 | 决策点 |
|---|---|---|
| 0. MVP — 先看见关键词 | 改 `docs/local-screen-prompt.md` 加 keywords 字段 + `validate_screen_result` 透传 + dump 到 jsonl / 日志，不碰飞书 schema | 抽词质量过关吗？类型 / 长度 / 数量靠谱吗？拼写变体多严重？ |
| 1. 落地关键词表 | 飞书新建 KEYWORD 表（规范名 / 类型 / 归一项 / 首次出现 / 备注），NEWS / FILTERED 加 `关键词记录` 关联字段，主流程 `ensure_keyword_records` + 写关联 | 飞书 UI 用得顺吗？关键词增长曲线？ |
| 2. 异步合并扫描 dry-run | 独立 entry point `merge_keywords.py`：拉表 → 生成候选组 → LLM 双跑判断 → 输出 dry-run JSON，不动数据 | LLM 判断靠谱吗？是否进入小批量 apply 设计？ |
| 3. 异步合并 --apply | dry-run 加 `--apply` 真执行：改链 NEWS / FILTERED 关联、合并归一项、删除次记录 | 是否要 cron？合并失败回滚怎么搞？ |
| 4. 事件聚类（独立 feature） | 复用现有 Cloudflare Vectorize（embedding 已就位）做相似度聚类，一批文章 → event；event 命名可借用阶段 1+ 稳定下来的关键词 | 跟关键词独立的子项目，单独再 brainstorm |

## 4. 阶段 0 — MVP，先看见关键词

### 范围

- 修改 `docs/local-screen-prompt.md`：模式 A（ingest，写入候选）和模式 B（pass，跳过候选）的 schema 都新增 `keywords: [{"name": "...", "type": "..."}]` 字段。抽取规则（每篇 1-3 个、单个 ≤20 字符、类型限定 9 枚举）引用 `2026-05-08-keyword-monitoring-design.md`，不在 prompt 内重复展开。
- 修改 `rss_ingest.py` 的 `validate_screen_result`（当前在 line 270 附近）：接受 keywords 字段，做基础校验（数量上限、单项长度上限、类型枚举），原样透传到 analysis dict，校验失败时按现有失败链路处理。
- 在主流程现有日志通道里打印每篇文章抽到的关键词；可选 dump 到 `out/keywords-YYYYMMDD.jsonl`，每行一条 `{url, title, keywords: [...]}`。
- 不动飞书 schema、不归一、不建关键词表、不写关联字段。

### 交付物

- 改后的 `docs/local-screen-prompt.md`、`rss_ingest.py`，跑一次本地 ingest 后产出至少一个 jsonl 文件 / 日志片段，覆盖一定数量的样本（建议 ≥ 50 篇）。
- 一份人工抽样 review，覆盖类型分布、长度分布、是否有泛化词 / 标题式短语、是否有明显拼写变体。

### 决策点

- 抽词质量是否达到可上线标准？
- 类型枚举是否需要调整（合并 / 拆分 / 删除某类）？
- 拼写变体严重程度是否值得在阶段 1 先做归一，还是等阶段 2 / 3 LLM 合并？
- 是否进入阶段 1。

### 风险

低：所有改动都在本地代码 / prompt 层，飞书 schema、主流程现有字段都不动，回滚只是 revert 两个文件。最坏情况是 jsonl 没人看 / 抽词烂，停在阶段 0 也没有沉没成本。

## 5. 阶段 1 — 落地关键词表

### 范围

- 飞书在同一个 app（`FEISHU_APP_TOKEN`）下新建 KEYWORD 表。
- KEYWORD 表字段：
  - 规范名：文本，主显示字段
  - 类型：单选，对齐 design 文档的 9 枚举
  - 归一项：多行文本，每行存放一个真实别名；不写规范名自身的小写结果，NFKC + lower 在代码索引里完成
  - 首次出现：日期
  - 备注：文本
- 新增环境变量 `FEISHU_KEYWORD_TABLE_ID`，挂在 `config.py` 的飞书字段映射模块。
- NEWS 表与 FILTERED 表保留「关键词」多选字段作为原始抽取结果，分别新增「关键词记录」关联字段，关联到 KEYWORD 表。
- 启动期 prefetch：扫一遍 KEYWORD 表，构建内存索引 `{normalized_alias: (record_id, canonical, type)}`，用于查找时做 NFKC + lower exact match。
- 主流程在 `build_news_fields` / `build_filtered_fields` 之前新增 `ensure_keyword_records`：
  - 命中索引 → 复用 record_id
  - 未命中 → 创建 KEYWORD 记录，写入规范名 / 类型 / 首次出现，更新内存索引
- 写完关键词记录后再把 record_id 字符串数组挂到 NEWS / FILTERED 的 `关键词记录` 关联字段上。飞书 Bitable v1 API 要求值形如 `["recxxx"]`，不是 `[{ "id": "recxxx" }]`。
- 当前实现使用线程锁保护 `ensure_keyword_records` 内存索引，不做缓存淘汰，不做历史批量补建。

### 交付物

- 飞书 KEYWORD 表（含完整字段）、NEWS / FILTERED 表的 `关键词记录` 关联字段。
- 改造后的 `rss_ingest.py`、`config.py`，端到端跑一次 ingest，飞书侧能看到 NEWS 与 FILTERED 都有关键词关联，KEYWORD 表里每条记录有完整字段。
- 启动期 prefetch 与 `ensure_keyword_records` 的运行日志。

### 决策点

- 飞书 UI 看关键词关联是否易用（点击关键词能否反查相关新闻）？
- 关键词数量增长曲线是否在可接受范围（每天新增多少 / 总量多少）？
- 拼写变体在 KEYWORD 表上重复严重到什么程度，是否到了必须做异步合并的临界？
- 是否进入阶段 2。

### 风险

中：动飞书 schema 是破坏性改动，新增字段在历史记录上会出现空值；KEYWORD 表 prefetch 量级随时间膨胀，单线程 `ensure_keyword` 在新关键词高峰可能拖慢主流程。回滚需要清理飞书字段 + revert 代码 + 处理已写入的 KEYWORD 记录。

## 6. 阶段 2 — 异步合并扫描 dry-run

### 范围

- `merge_keywords.py` 独立 entry point，与 `rss_ingest.py` 主流程不串联，可独立调用。
- 流程：拉取 KEYWORD 全表 → 生成候选组 → LLM 原顺序 / 反向顺序各跑一次 → 严格 JSON 校验 → 输出 dry-run JSON。
- 候选生成包含两类：
  - compact key 机械写法差异：大小写、空格、横杠、点号等。
  - 本地别名种子：`docs/local-merge-alias-groups.json`，用于 `NVIDIA / 英伟达`、`Trump / 特朗普 / 川普` 等跨语言候选。
- 默认 dry-run，不动飞书数据，不修改 KEYWORD 表，不影响 NEWS / FILTERED 关联。
- 当前推荐提示词为 `docs/local-merge-prompt-simple.md`；`docs/local-merge-prompt.md` 保留为较严格的旧版参考。
- LLM 不负责最终主记录选择；只判断是否能合并。主记录由代码用确定性规则选择，避免顺序敏感。
- `merge_keywords.py --sync-core-fields` 还可同步 KEYWORD 的 `NEWS次数 / FILTERED次数 / 最后出现 / 热度样本` 快照字段。

### 交付物

- `merge_keywords.py`、`docs/local-merge-prompt-simple.md`、`docs/local-merge-alias-groups.json`。
- 至少一次完整真实候选 dry-run 输出文件，覆盖当前候选组。
- fixture 覆盖机械写法差异、跨语言别名、公司 / 产品、上位 / 下位、版本差异、泛称简称等反例。

### 决策点

- LLM 判断精确率是否足够（误并率是否可接受）？
- 别名种子维护成本是否可接受，是否需要扩大种子来源？
- 是否进入阶段 3 的小批量 `--apply` 设计。

### 风险

低：完全只读，最坏情况是 LLM 建议没法用 / 提示词不收敛，迭代成本只在提示词层，不污染线上数据。

## 7. 阶段 3 — 异步合并 --apply

### 范围

- `merge_keywords.py` 增加 `--apply` flag，开启后真执行 dry-run JSON 中的合并方案。
- 执行步骤（每条建议）：
  - 把次记录上的归一项追加合并到主记录的归一项字段
  - 改链：将所有引用次记录的 NEWS / FILTERED 关联指向主记录
  - 硬删除次记录（不软删，不保留 tombstone）
- 主流程的 KEYWORD 内存索引会因次记录消失 / 主记录归一项扩张而失效，约定在主流程下次启动时重建（不做在线热更新，不在 `merge_keywords.py` 里通知主流程）。
- `--apply` 模式默认仍要求显式传参，避免误触发。

### 交付物

- `merge_keywords.py --apply` 完整实现，至少一次端到端 apply 验证（建议先小批量 apply）。
- apply 执行日志（每条建议成功 / 失败 / 跳过原因）。
- 主流程下次启动时重建索引并恢复正常 ingest 的端到端验证。

### 决策点

- 是否需要把 `merge_keywords.py --apply` 接入 cron / scheduled task，跑多频？
- 单条合并失败时的回滚策略是否完善（部分改链成功 + 删除失败 = 数据不一致风险）？
- apply 的速率与并发是否需要限流（飞书 API 速率约束）？

### 风险

高：硬删除次记录 + 改链 NEWS / FILTERED 关联是双写破坏性操作，飞书 API 中途失败会留下不一致状态；改链涉及历史 NEWS / FILTERED 全量，量级大；回滚不能简单 revert 代码，需要对照 dry-run JSON 反操作。建议在 dry-run JSON 之外单独保留一份 apply 前的备份快照（KEYWORD 全表 + 受影响的 NEWS / FILTERED 关联），以便人工恢复。

## 8. 阶段 4 — 事件聚类（独立 feature）

事件聚类是独立 feature，复用现有 Cloudflare Vectorize（embedding 已就位）做相似度聚类，一批文章聚成 event，event 的命名 / 标签可借用阶段 1+ 稳定下来的关键词体系。

该阶段不在本计划展开，待关键词监控走完阶段 1+ 之后单独 brainstorm 与立项。

## 9. 进度状态

- [x] 阶段 0 — MVP，先看见关键词（2026-05-08 完成，commit 314c255 / d083e66 / 1eb8f56 / 61f5a4f / 40264f0 / 61476c2）
- [x] 阶段 1 — 落地关键词表主链路（2026-05-11 完成，commit 3b270bb / 596f0d6 / 5d3b375 / 4fca968；KEYWORD 表、`关键词记录` 关联、NFKC+lower exact match 已上线）
- [x] 阶段 2 — 异步合并扫描 dry-run（2026-05-11 完成：fixture 14/14，真实候选 27/27；Qwen 与 Gemini 均通过）
- [ ] 阶段 3 — 异步合并 --apply
- [ ] 阶段 4 — 事件聚类（独立 feature）

## 10. 下一步

2026-05-12 补充完成项：
- 泛化词过滤：42 个 exact-only 黑名单 + `--tag-generic` 标记已有泛化词
- 别名种子：从 2 组扩到 52 组（科技巨头、AI 公司、人物、产品、硬件中英文对照）
- 别名自动发现：`alias_discovery.py --dry-run`，按 type 整批 LLM，自动剔除低频词，negative reason filter 拦截自相矛盾输出。真实 dry-run 精确率 ~100%
- 过滤表摘要：screen pass 模式输出 title_zh + summary，过滤表也有中文标题和摘要
- 分页修复：飞书 records/search 分页参数从 body 改到 query params

待做：
- 趋势热度系统（keyword_heat.py + SQLite 每日统计），设计已跟 GPT 碰完
- 合并 --apply（真实执行合并、改链）
- 每日定时跑 alias discovery + 合并同步
