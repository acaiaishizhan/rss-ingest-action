# KEYWORD 运维说明

本文记录 KEYWORD 表当前已经落地的能力、脚本入口和边界。它面向接手维护的人，不是未来规划。

## 当前能力

主流程 `rss_ingest.py` 会在 NEWS / FILTERED 写入时同步维护关键词：

1. screen LLM 输出 1-3 个 `{"name", "type"}`。
2. 原始 `name` 写入 NEWS / FILTERED 的 `关键词` 多选字段。
3. `ensure_keyword_records` 在 KEYWORD 表中查找或创建记录。
4. NEWS / FILTERED 的 `关键词记录` 关联字段写入 KEYWORD record_id 字符串数组。

KEYWORD 表当前字段分两类。

基础字段：

- `规范名`
- `类型`
- `归一项`
- `首次出现`
- `备注`

脚本同步字段：

- `NEWS次数`
- `FILTERED次数`
- `最后出现`
- `热度样本`

父子聚合与窗口热度字段：

- `父关键词`
- `子关键词`
- `自身24h`
- `自身NEWS24h`
- `自身前24h`
- `自身7d`
- `自身前7d`
- `自身30d`
- `自身前30d`
- `24h`
- `NEWS24h`
- `前24h`
- `7d`
- `前7d`
- `30d`
- `前30d`

飞书反向关联字段：

- `相关新闻`
- `相关过滤记录`

手动保留字段：

- `manual/auto`、`manual`、`来源`、`创建方式`、`创建来源` 任一字段值为 `manual` 时，自动清理不会删除该关键词。

## 计数字段的边界

`NEWS次数`、`FILTERED次数`、`最后出现`、`热度样本` 不是实时字段。

它们由 `merge_keywords.py --sync-core-fields` 扫 NEWS / FILTERED 后回填 KEYWORD，是一次快照。RSS 主流程继续写入后，新关键词会有关联记录，但这些计数字段要重新同步才会更新。

同步命令：

```powershell
.\.venv\Scripts\python.exe merge_keywords.py --sync-core-fields --usage-max-pages 50
```

实现上使用飞书 `records/batch_update`，每批最多 500 条。不要改回逐条更新；逐条更新 1700+ 条会很慢。

飞书反向关联字段 `相关新闻` / `相关过滤记录` 对高频 KEYWORD 会被约 500 条可见记录截断。依赖反向关联聚合的公式字段（例如 `NEWS24h`、`24h`、`7d`、`30d`、`最后出现`）对高频词只是近似值。2026-06-04 排查 OpenAI 时确认：NEWS / FILTERED 单条记录上的 `24h` 公式正确，但 KEYWORD 层 `SUM(相关新闻.24h)` 没吃到当天新记录，因为 `相关新闻` 只返回了 500 条旧关联。主表 30d 归档可以减少老记录挤占，但不能替代未来脚本快照热度系统。

截至 2026-05-15，真实同步已验证：

- KEYWORD：1.2 万级记录可全量读取
- 测试：`220 passed`

## 关键词合并 dry-run

`merge_keywords.py` 现在支持只读合并测试，不会修改飞书数据。

固定 fixture 测试：

```powershell
.\.venv\Scripts\python.exe merge_keywords.py --llm-fixture-run --provider deepseek --prompt-path docs\local-merge-prompt-simple.md
```

真实候选 dry-run：

```powershell
.\.venv\Scripts\python.exe merge_keywords.py --llm-dry-run --provider deepseek --prompt-path docs\local-merge-prompt-simple.md --llm-group-limit 0
```

当前采用简单提示词：

- `docs/local-merge-prompt-simple.md`

保留较严格旧版提示词作为参考：

- `docs/local-merge-prompt.md`

合并候选来源：

1. 机械写法差异：大小写、空格、横杠、点号等 compact key 一致的组。
2. 本地别名种子：`docs/local-merge-alias-groups.json`。

别名种子用于让脚本能发现 `NVIDIA / 英伟达`、`Trump / 特朗普 / 川普` 这类跨语言候选。LLM 只能判断送到它面前的候选，不能从全表凭空找出所有译名关系。

截至 2026-05-11，简单提示词测试结果：

- fixture：14/14，Qwen 与 Gemini 都通过
- 真实候选：27/27，Qwen 与 Gemini 都通过
- 真实候选已包含 `Nvidia / 英伟达`、`Trump / 特朗普`

## 泛化词过滤

`docs/local-keyword-name-blocklist.txt` 包含泛化词（AI、人工智能、技术、产品、模型、GPT、API、CLI、App、Agent 等）。

LLM 抽出的关键词如果 exact match 黑名单，自动跳过不写入 KEYWORD 表。匹配规则：NFKC + lower + strip 后精确比较。拦”AI”不拦”AI Agent”，拦”芯片”不拦”AI 芯片”。

主流程里还有一层脏关键词兜底：纯数字、极短英文字母、URL / 域名等不会写入 KEYWORD，也不会参与 NEWS / FILTERED 的关键词字段。注意这只是兜底；真正要减少浪费，优先在 `docs/local-screen-prompt.md` 里约束 LLM 不要输出这些词，因为关键词最多只有 3 个，脏词会占掉有效名额。

2026-05-13 去重流程已改为“关键词命中的旧 NEWS 才进入 LLM 文本去重候选”。这让关键词质量直接影响去重召回。后续优化关键词时，需要重点处理两件事：

1. 泛化词去除：继续扩充泛化词黑名单，避免“AI / 模型 / 技术 / 产品更新”等词把候选池撑大。
2. 归一化：继续做中英文别名、大小写、空格、符号、简称 / 全称归一，例如 `Codex for Chrome` 与 `Codex`、`自我复制` 与 `AI自我复制`。

已有泛化词标记：

```powershell
.\.venv\Scripts\python.exe merge_keywords.py --tag-generic-dry-run
.\.venv\Scripts\python.exe merge_keywords.py --tag-generic
```

标记写入 KEYWORD 的「备注」字段，前缀 `[generic:blocklist]`，不删除记录、不改关联。

## 稳定关键词重建

`tools/keyword_rebuild.py` 负责重建关键词派生层的安全编排。它只应该在备份和 dry-run 审核后使用真实 apply。

常用顺序：

```powershell
.\.venv\Scripts\python.exe tools\keyword_rebuild.py --backup --output-dir out\keyword-backup-YYYYMMDD-HHMMSS
.\.venv\Scripts\python.exe tools\keyword_rebuild.py --audit --output out\keyword-audit.json
.\.venv\Scripts\python.exe tools\keyword_rebuild.py --clear-links --dry-run --output out\clear-links-dryrun.json
.\.venv\Scripts\python.exe tools\keyword_rebuild.py --clear-links --apply --output out\clear-links-apply.json
.\.venv\Scripts\python.exe tools\keyword_rebuild.py --delete-keywords --dry-run --output out\delete-keywords-dryrun.json
.\.venv\Scripts\python.exe tools\keyword_rebuild.py --delete-keywords --apply --output out\delete-keywords-apply.json
```

`--delete-keywords --apply` 会在 NEWS / FILTERED 仍有 KEYWORD 链接时拒绝执行。

最近 60 天回填使用：

```powershell
.\.venv\Scripts\python.exe tools\backfill_keywords.py --apply --rebuild-existing --clear-when-empty --days 60 --max-records 100000 --llm-concurrency 8 --state-path out\backfill-60d.state.json
```

也可以按日期窗口拆分：

```powershell
.\.venv\Scripts\python.exe tools\backfill_keywords.py --apply --rebuild-existing --clear-when-empty --since-date 2026-03-17 --before-date 2026-03-24 --max-records 20000 --llm-concurrency 8 --state-path out\backfill-20260317-20260324.state.json
```

`--record-ids path.json` 会直接按 record_id 取记录，适合补漏；`--state-path` 会记录已成功写回的 `table + record_id`，中断后可恢复。

## 父子关系与热度聚合

`tools/keyword_parent_rollup.py` 负责：

- 创建或复用 `父关键词`。
- 创建或复用 `归属关键词`。
- 根据稳定包含关系生成父关键词计划。
- 更新自身热度和总热度公式。

命令：

```powershell
.\.venv\Scripts\python.exe tools\keyword_parent_rollup.py --dry-run --output out\keyword-parent-dryrun.json
.\.venv\Scripts\python.exe tools\keyword_parent_rollup.py --apply --input out\keyword-parent-dryrun.json --output out\keyword-parent-apply.json
```

总热度字段使用 `自身字段 + SUM(子关键词.自身字段)`，不要改成 `SUM(子关键词.24h)`，否则父子层级会重复计算。`NEWS24h` 只统计 NEWS 表，公式链路是 `自身NEWS24h = SUM(相关新闻.24h)`，再聚合子关键词的 `自身NEWS24h`。

`tools/sync_keyword_expanded_links.py` 负责把 NEWS / FILTERED 的 `关键词记录` 扩展成：

```text
LLM 原始关键词 + 父关键词 + 归属关键词
```

例子：

```text
原始关键词：Claude Opus 4.7
关键词记录补后：Claude Opus 4.7、Claude、Opus、Anthropic
```

这样 `Claude`、`Opus`、`Anthropic` 的 `30d` 会由飞书关联和公式自动更新。脚本使用飞书批量更新；如果某一批失败，会拆成单条回退。

## 旧资讯季度归档

`tools/archive_old_records.py` 负责把 NEWS / FILTERED 主表中过了保留窗口的记录归档到季度表，避免历史新闻长期挤占 KEYWORD 反向关联的 500 条可见容量。

判断规则：

```text
NEWS / FILTERED 单条记录的 30d = 0
```

目标表按记录时间决定：

```text
NEWS      -> 2026Q2、2026Q3 ...
FILTERED  -> 2026Q2回收站、2026Q3回收站 ...
```

时间来源优先级：

```text
发布时间 -> 创建时间
```

命令：

```powershell
.\.venv\Scripts\python.exe tools\archive_old_records.py --output out\archive-old-records-dryrun.json
.\.venv\Scripts\python.exe tools\archive_old_records.py --apply --output out\archive-old-records-apply.json
```

注意：

- 默认用飞书服务端筛选 `30d = 0`，不是扫全表；`--scan-all` 仅用于排障复核。
- 归档字段不带 `关键词记录`，避免季度历史表继续污染 KEYWORD 的反向关联。
- 使用 `item_key` 做幂等；如果记录已经在目标季度表中，apply 会只删除主表残留。
- 目标季度表不存在时会报告 `missing_tables`，不会删除主表记录。首次换季前需要准备好 `YYYYQn` / `YYYYQn回收站` 表。
- 这一步归档的是 NEWS / FILTERED 资讯，不会删除 KEYWORD。

## 30d 空关键词清理

`tools/cleanup_stale_keywords.py` 负责删除最近 30 天没有贡献的 KEYWORD：

```powershell
.\.venv\Scripts\python.exe tools\cleanup_stale_keywords.py --output out\stale-keyword-cleanup-dryrun.json
.\.venv\Scripts\python.exe tools\cleanup_stale_keywords.py --apply --output out\stale-keyword-cleanup-apply.json
```

删除规则：

```text
30d = 0
并且不是 manual
并且首次出现早于保护期（默认 48 小时）
```

注意：

- 看总字段 `30d`，不要看 `自身30d`。
- 默认保护 48 小时内的新词，避免 RSS 主流程刚创建关键词、维护流程同时清理时误删。
- 字段缺失时不删，避免公式字段异常时误删。
- 这一步只删 KEYWORD 记录，不会删除 NEWS / FILTERED 的新闻正文、标题、链接。

## 巡检

`tools/audit_keywords.py` 输出 KEYWORD 健康报告：

```powershell
.\.venv\Scripts\python.exe tools\audit_keywords.py --output out\keyword-final-audit.json
```

健康条件包括：

- merged/generic 关键词没有 active links。
- exact 泛词没有作为 KEYWORD 记录存在。
- compact duplicate groups 为 0，或重复组全部有明确 `duplicate-ok:` reason。
- 抽样热度对账为 OK。

## 别名自动发现

`alias_discovery.py` 自动发现”可能是同一实体但被存成多条”的候选。当前有两种模式。

日常增量模式：

- 输入：最近新增 / 变动的关键词。
- 对照：`data/keyword_snapshot.json` 的历史 KEYWORD 快照。
- LLM 任务：只判断“新增关键词应该归到哪个历史规范词”，不允许历史词之间自由组团。
- 输出：accepted pair，再交给 `merge_keywords.py --alias-update-preview` 生成 KEYWORD「归一项」追加计划。

全量校准模式：

- 按 type 扫完整 KEYWORD 表。
- 用宽松召回 + 规则 veto + 严格验证三段式找别名。
- 适合每周校准或人工排查，不适合每天都跑。

```powershell
.\.venv\Scripts\python.exe alias_discovery.py --dry-run --provider deepseek
.\.venv\Scripts\python.exe alias_discovery.py --fixture-run --provider deepseek
.\.venv\Scripts\python.exe alias_discovery.py --three-stage --provider gemini --model gemini-3-flash-preview --page-size 500 --max-pages 2 --batch-size 50
.\.venv\Scripts\python.exe merge_keywords.py --alias-update-preview --alias-discovery-path out\alias_three_stage_sample.json --output out\alias_update_preview.json
.\.venv\Scripts\python.exe tools\run_keyword_alias_daily.py --dry-run --provider gemini --model gemini-3-flash-preview --keyword-snapshot-path data\keyword_snapshot.json
```

工作原理：
全量三段式工作原理：

1. 从飞书拉 KEYWORD 全表。
2. 1 次关键词也进入归一候选，避免漏掉低频但真实的同义词。
3. 按 type 分组后先宽松召回候选。
4. 代码先拦掉域名、套餐 / tier、疑似理由、泛 AI 服务词、普通中文泛议题等高风险误归一。
5. 剩余候选再交给 LLM 严格验证。
6. 输出候选 JSON 到 `out/` 目录。

`--alias-update-preview` 会把 accepted 结果转成“准备写入 KEYWORD「归一项」字段”的预览。它只读飞书，不写入。预览里会列出：

- 哪条 KEYWORD 作为主词
- 要追加哪些归一项
- 哪些 accepted 已经存在于归一项里，因此跳过

规范词选择规则：优先选择 `NEWS次数 + FILTERED次数` 更高的 KEYWORD 记录；次数相同时选更短的名称，再按名称排序。这样高频历史规范词不会被低频新增别名替换。

真实写入分两步：

```powershell
.\.venv\Scripts\python.exe merge_keywords.py --alias-update-apply out\alias_update_preview.json
.\.venv\Scripts\python.exe tools\apply_keyword_alias_links.py --apply --recent-hours 25 --keyword-snapshot-path data\keyword_snapshot.json
```

第一步批量写 KEYWORD「归一项」。第二步把 NEWS / FILTERED 的「关键词记录」从别名 KEYWORD 改链到规范 KEYWORD，并给被改链的别名记录追加 `[merged→主词]` 备注。它不会删除 KEYWORD 记录。

别名种子：`docs/local-merge-alias-groups.json`（52 组），覆盖科技巨头、AI 公司、人物、产品、硬件的中英文对照。

## 自动归一本地任务

日常 KEYWORD 归一由本机 Windows 任务计划程序运行，任务名 `keyword-alias-daily`，入口为 `tools\run_keyword_alias_daily_local.ps1`：

- 每天北京时间 04:00：先归档 NEWS / FILTERED 中 `30d = 0` 的旧资讯，再删 `30d = 0`、非 `manual`、且首次出现超过 48 小时保护期的 KEYWORD，然后增量归一，默认真实写飞书。
- LLM provider 为 `ark`（Volcengine Ark Coding Plan），模型固定 `deepseek-v4-pro`。
- 每次归一后会执行 alias link、parent rollup、core field sync、expanded keyword links 和 keyword audit；真实写入模式下 audit fail 时工作流失败并上传报告，`dry_run=true` 只报告不阻断。旧资讯归档是流水线第一步，目标季度表缺失时会在 summary 里显示 `missing_tables` 并阻止 apply 删除源记录。
- 每周自动全量校准已暂停；需要全量校准时手动运行 `full_run=true`。
- 手动 dry-run：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\run_keyword_alias_daily_local.ps1 -DryRun
```

- 全量校准：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\run_keyword_alias_daily_local.ps1 -FullRun
```

- 真实写入成功后更新本地 `data/keyword_snapshot.json`，需要定期提交合回 `main`，保持 RSS ingest 的 git 基线新鲜。
- 非零退出会经 `task_alerts.py` 发飞书 webhook 告警（同任务 2 小时冷却）。
- `.github/workflows/keyword-alias-daily.yml` 只保留手动 `workflow_dispatch` 应急 fallback，不再定时运行。

阶段性稳定验证记录：

- 2026-05-15 全量真实写入成功：KEYWORD「归一项」写入 87 条，NEWS 改链 80 条，FILTERED 改链 72 条，KEYWORD 备注 88 条，失败 0。
- 2026-05-15 增量真实写入成功：KEYWORD「归一项」写入 2 条，NEWS 改链 4 条，FILTERED 改链 8 条，KEYWORD 备注 7 条，失败 0。
- 2026-05-15 增量 dry-run 成功：扫描最近新增 698 条，对照快照 12806 条，计划写 2 条，失败 0。

## LLM 泛词审查

`tools/audit_keyword_noise_llm.py` 用来审查 KEYWORD 表里过泛、无实体、无法追踪的关键词。它默认只 dry-run，输出 `block_auto` / `review`，不写飞书。

这个任务和归一不一样：

- 归一：输入可以很大，但最终只输出少量 `mappings` / `groups`。
- 泛词审查：输入 500 条时，可能要输出几百个 `record_id`，输出本身就大。

2026-05-17 实测结论：

- 官方 Gemini 3 Flash 推荐配置：

```json
{
  "maxOutputTokens": 65536,
  "thinkingConfig": {
    "thinkingLevel": "minimal"
  }
}
```

- 日常增量泛词审查不再人为分批：最近 48h 单请求实测扫描 334 个新增关键词、送审 329 个候选，耗时约 52 秒，错误 0。
- 日常增量归一不再人为分批：按 keyword type 分组后每类整包跑。最近 48h 单次实测扫描 333 个新增关键词，耗时约 91 秒，错误 0，接受 2 个归一候选。
- 如果不设置 `thinkingLevel: minimal`，Gemini 会把大量输出预算花在 thinking 上，导致 JSON 写到一半被截断。一次失败样本里 `thoughtsTokenCount=11520`，真正 JSON 只写了约 465 tokens。
- JSON 解析不要用“取第一个 `{` 到最后一个 `}`”的粗糙方式；模型可能在完整 JSON 后多吐少量尾巴。应使用 `json.JSONDecoder().raw_decode()` 取第一个完整 JSON 对象。
- 旧 Ollama Cloud 路径已停用；大批量泛词审查写表前优先用官方 Gemini/Vertex 或 DeepSeek 直连。

泛词审查结果进入写表前必须人工抽样：

- `block_auto`：只删除明显无实体、不可追踪的泛词。
- `review`：只定位，不自动删。
- 与归一冲突时，先归一后清泛词；不要把“相关词”当“别名”合并。

## 文本去重怎么使用归一结果

LLM 文本去重现在会读取一份本地归一快照，默认路径是 `docs/local-merge-alias-groups.json`，也可以用 `LOCAL_DEDUP_ALIAS_GROUPS_PATH` 指向定期生成的新快照。

它的作用很窄：只帮去重找到候选旧新闻，不改 KEYWORD 表，不写飞书字段，也不替 LLM 判重。当前实现不是 TF-IDF / scikit-learn 相似度，而是“关键词候选筛选 + LLM 判重”。

例子：

- 新文章关键词是 `英伟达`
- 旧 NEWS 关键词是 `NVIDIA`
- 快照里这两个词在同一组
- 去重候选筛选会把这篇旧 NEWS 捞出来
- 最后仍然由 LLM 看标题和摘要，判断是不是同一条新闻

这里不要把“相关”当成“同一个”。如果归一快照里把 `OpenAI` 和 `ChatGPT` 放进同一组，去重候选会明显变脏，误杀风险会上升。

提示词：`docs/local-alias-discovery-prompt.md`（严格版，禁止上下位/竞品/版本/同领域不同切面合并）。

fixture 测试：`tests/fixtures/alias_discovery_cases.json`（33 个用例）。

## 过滤表摘要

screen 阶段 pass（丢弃）的文章也会输出中文标题（title_zh）和事实摘要（summary），写入过滤表「摘要」字段；过滤原因写入单独字段，不混进 summary。

## 尚未实现

以下不要误认为已经完成：

- `merge_keywords.py --apply`：还没有实现真实合并和删除次记录。
- 趋势热度系统：还没有 `keyword_heat.py`。设计已完成（SQLite 存每日统计，飞书只放榜单），待实现。
- KEYWORD_DAILY_STATS：还没有建每日统计表。

趋势下一步：
- 今日热度榜、本周升温榜、高信号热词榜、噪音词榜
- 低量假趋势处理（最小量门槛 + 伪计数平滑）
- 趋势数据存 SQLite，飞书只写当前状态和榜单

## 安全提醒

- 合并前先跑 fixture，再跑真实候选 dry-run。
- 真实合并前必须保存 KEYWORD 和受影响 NEWS / FILTERED 关联快照。
- `rss-ingest-local.env` 是真实配置，不要提交。
- 不要用 `git clean -fdx` 清理仓库；会删掉被 gitignore 的真实 env。
