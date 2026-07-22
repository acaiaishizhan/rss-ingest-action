# Feishu RSS Digest

本项目把飞书多维表中的 RSS 源同步为结构化资讯：抓取 RSS、按本地提示词筛选和总结、去重后写入飞书新闻表，并可把低分或过滤内容写入过滤表。

当前 `main` 已包含 RSS 主流程、GitHub Actions 云端运行模式、关键词抽取、KEYWORD 表归一化关联、LLM 文本去重、每日关键词别名归一（本机定时任务）、父关键词 / 归属关键词补链、旧资讯季度归档、30d 空关键词清理、KEYWORD 快照和最近记录改链。不包含 KEYWORD_DAILY_STATS、`event_heat` 或话题聚类。关键词运维入口见 [docs/keyword-operations.md](docs/keyword-operations.md)。

## 功能概览

- 从飞书 RSS 源表读取订阅源并按增量规则抓取。
- `grok_watch.py` 定时通过公共 `grok-browser` 工具调用网页端 Grok Expert 搜 X 真料；`grok-browser` 默认以 offscreen/no-focus 方式运行并在完成后清理命令页，经 fxtwitter 验真和多层去重后写本地 RSS feed，由主流程当普通源消费；运维说明见 [docs/grok-watch.md](docs/grok-watch.md)。
- 使用本地提示词文件做 screen 分析：
  - `docs/local-keyword-blocklist.txt`
  - `docs/local-screen-triage-prompt.md`
  - `docs/local-screen-prompt.md`
- `docs/local-summarize-prompt.md` 仅作为旧 fallback：screen 未输出 `qa` 时才会补跑。
- screen 先在同一次初筛中输出 `keep / filter / uncertain` 与信号评分；低于 `TRIAGE_MIN_SCORE` 的条目直接进入 FILTERED。其余 `keep / uncertain` 再由内容处理一次输出内容评分、分类和 `title_zh + summary + keywords + qa`，且只允许 `uncertain` 在内容处理时改判 `pass`。内容处理评分只作字段，不再次决定去留。
- screen 阶段同时输出 `keywords: [{name, type}]`，写入新闻表和过滤表的 `关键词` 多选字段，并可通过 `关键词记录` 关联到 KEYWORD 表做归一化。
- KEYWORD 表支持脚本同步 `NEWS次数`、`FILTERED次数`、`最后出现`、`热度样本`；这些是快照字段，不是实时趋势。
- `merge_keywords.py` 支持关键词合并 fixture 测试、真实候选 dry-run、核心计数字段同步，以及把别名发现结果批量追加到 KEYWORD「归一项」。
- 本机任务计划程序 `keyword-alias-daily` 每天北京时间 04:00 做关键词维护（`tools/run_keyword_alias_daily_local.ps1`）：先把 NEWS / FILTERED 中 `30d = 0` 的旧资讯归档到季度表，再删除 `30d = 0`、非 `manual`、且首次出现超过保护期的 KEYWORD，之后做别名归一、改链、父关键词 / 归属关键词补链和巡检；`.github/workflows/keyword-alias-daily.yml` 仅保留手动触发。
- RSS 主流程默认 LLM provider 为 Volcengine Ark Coding Plan 的 `deepseek-v4-flash`（subagent `d` 同款 Flash lane）；本机关键词维护脚本使用 Ark `deepseek-v4-pro`（`dsp` 同款 Pro lane）。手动触发的关键词维护 GitHub Action 走 DeepSeek 直连（`DEEPSEEK_API_KEY` secret），也可显式切换 Gemini、Ark、iFlow、OpenAI、Zhipu。
- RSS 源抓取支持并发，默认 `RSS_FETCH_CONCURRENCY=20`，同一 host 默认最多并发 4 个请求；超时源会用 4 并发补跑一次，避免本地 RSSHub 被打满后直接漏源。
- 对 RSS 正文为空或极短的条目支持网页全文 fallback：重点源使用定向解析，其他公开 HTTP(S) 文章页使用保守通用解析；所有 HTTP 抓取都会拒绝内网/保留地址、逐跳校验重定向，并限制响应大小。
- 支持 `item_key` 精确去重和 screen 后的 LLM 文本去重：先按关键词记录 / 关键词名称 / 本地归一快照找候选旧 NEWS，再由 LLM 判断是否同一事件。
- 支持失败条目池 `failed_items`，后续运行会有限重试。
- 支持飞书提醒记录表、过滤表和可选二次同步表。
- GitHub Actions 默认每 20 分钟运行 RSS 主流程；本机 `rss-ingest-fetch` 只作为迁移回滚入口。Action 日志保留为 7 天 artifact，非零退出继续通过飞书 webhook 告警。

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item rss-ingest-local.env.example rss-ingest-local.env
```

填好 `rss-ingest-local.env` 后运行：

```powershell
.\.venv\Scripts\python.exe rss_ingest.py
```

运行测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

`pytest.ini` 已限制默认测试目录为 `tests/`，避免归档目录或本地临时目录被误收集。

## 必要配置

最小可运行配置：

```env
FEISHU_APP_ID =
FEISHU_APP_SECRET =
FEISHU_APP_TOKEN =
FEISHU_NEWS_TABLE_ID =
FEISHU_RSS_TABLE_ID =
LLM_PROVIDER = ark
ARK_API_KEY =
ARK_BASE_URL = https://ark.cn-beijing.volces.com/api/coding/v3
ARK_MODEL = deepseek-v4-flash
```

常用可选配置：

```env
FEISHU_FILTERED_TABLE_ID =
FEISHU_KEYWORD_TABLE_ID =
FEISHU_NOTIFY_TABLE_ID =
ENABLE_SECONDARY_SYNC = false
FEISHU_SYNC_TABLE_ID =
FEISHU_SYNC_APP_TOKEN =
RSS_FETCH_CONCURRENCY = 20
RSS_FETCH_PER_HOST_CONCURRENCY = 4
RSS_FETCH_RETRY_CONCURRENCY = 4
DEFAULT_FETCH_INTERVAL_MIN = 180
RSS_INGEST_ITEM_KEY_PREFETCH_ATTEMPTS = 2
TEXT_DEDUP_PREFETCH_MAX_PAGES = 2
TEXT_DEDUP_PROVIDER = ark
ENABLE_KEYWORD_SNAPSHOT_INDEX = true
KEYWORD_SNAPSHOT_GIT_REF = origin/main
KEYWORD_SNAPSHOT_GIT_FETCH = true
KEYWORD_SNAPSHOT_GIT_FETCH_INTERVAL_MIN = 60
KEYWORD_RUNTIME_SNAPSHOT_PATH = .cache/keyword_snapshot_runtime.json
LLM_CONCURRENCY = 4
FEISHU_MIN_SCORE = 6.0
ENABLE_TRIAGE_SCORE_GATE = true
TRIAGE_MIN_SCORE = 3.8
SCREEN_PROVIDER =
ARK_API_KEY =
ARK_BASE_URL = https://ark.cn-beijing.volces.com/api/coding/v3
ARK_MODEL = deepseek-v4-flash
DEEPSEEK_SCREEN_MODEL =
LOCAL_SCREEN_KEYWORDS_ADDENDUM_PATH =
```

配置只从本项目的 `rss-ingest-local.env` 加载，不读取父目录的 `local.env` 或 `.env`。Ark key 归属在该文件中；默认主链走 Ark Coding Plan：`deepseek-v4-flash` 对应 `d`，本机关键词维护脚本的 `deepseek-v4-pro` 对应 `dsp`。若只在本机运行，并希望覆盖 Ark 模型，可设置：

```env
ARK_API_KEY =
ARK_MODEL = deepseek-v4-flash
```

真实的 `rss-ingest-local.env` 被 Git 忽略，只提交 `rss-ingest-local.env.example`。

## 数据流

1. 读取飞书 RSS 源表。
2. 抓取 RSS entries，生成稳定 `item_key`。
3. 入队前生成正文：优先使用 RSS `content`，其次使用 `summary/description`；正文为空、过短或命中特定预览摘要时才请求公开原文页，重点源走定向解析，其他文章页走保守通用解析，覆盖范围与安全边界见 [docs/article-extraction.md](docs/article-extraction.md)。
4. 跳过已存在新闻和不满足时间窗口的条目。
5. 进入 LLM 队列并发处理。
6. 命中关键词或业务规则的条目写入过滤表。
7. 三态初筛评分达到 `TRIAGE_MIN_SCORE` 后继续处理；`uncertain` 由内容处理终审，最终通过者写入新闻表。
8. 更新 RSS 源表状态、游标和失败条目池。
9. 可选把新增新闻同步到二次表。
10. 本机 `keyword-alias-daily` 定时任务做关键词维护：每天先按 NEWS / FILTERED 的 `30d = 0` 归档旧资讯到季度表，再清理 30d 空关键词、增量归一新增词、补父级 / 归属关系；真实写入成功后更新 `data/keyword_snapshot.json`。GitHub Action 仅保留手动触发，全量校准需要时手动 `full_run=true`。

RSS ingest 默认优先用 KEYWORD snapshot 建索引：先从 git 的 `origin/main:data/keyword_snapshot.json` 读取已提交的基线（`KEYWORD_SNAPSHOT_GIT_REF` 可改），再用 `.cache/keyword_snapshot_runtime.json`，最后才回源飞书 KEYWORD 表。`git fetch` 默认按 `KEYWORD_SNAPSHOT_GIT_FETCH_INTERVAL_MIN=60` 跨进程节流，避免每个十分钟任务都访问远端；每轮仍执行本地 `git show`。新 KEYWORD 创建成功后会写入运行时 snapshot，避免下一轮重复创建。本机日跑 4 点更新的是工作区的 `data/keyword_snapshot.json`，需要定期提交并合回 `main`，否则 git 基线会越来越旧、只能靠运行时 snapshot 兜底。`KEYWORD_SNAPSHOT_MIN_ENTRIES` 默认 1000，避免误用测试残留或损坏的小 snapshot。

screen 和 QA 默认走 Ark Coding Plan 的 `deepseek-v4-flash`；文本去重候选池默认只预取最近 NEWS 的 2 页（`TEXT_DEDUP_PREFETCH_MAX_PAGES=2`），减少每轮启动时从飞书扫大表的耗时；LLM 判重默认走 `TEXT_DEDUP_PROVIDER=ark`，本机可用 env 覆盖。

## 飞书表字段

RSS 源表常用字段：

- `name`
- `feed_url`
- `enabled`
- `status`
- `last_fetch_time`
- `last_item_pub_time`
- `failed_items`

新闻表常用字段：

- `标题`
- `AI打分`
- `分类`
- `摘要`（screen 阶段事实摘要）
- `QA总结`（默认展示 3-8 组问答）
- `关键词`（多选字段，1-3 个，需在飞书表 UI 上手动加好，否则写入会失败）
- `关键词记录`（关联字段，关联 KEYWORD 表，用于归一化统计）
- `发布时间`
- `来源`
- `全文`
- `item_key`

NEWS / FILTERED 的「全文」写入前会被限制在 80000 字符内，避免飞书单元格 `TooLargeCell`；截断只影响飞书保存的全文字段，不改变 screen 阶段用于判断的正文输入。

过滤表常用字段：

- `标题`
- `过滤方式`
- `过滤原因`
- `关键词`（多选字段，同新闻表）
- `关键词记录`（关联字段，同新闻表）
- `发布时间`
- `来源`
- `全文`
- `item_key`

> 部署关键词功能时需要在飞书 NEWS / FILTERED / SYNC（如启用二次同步）三张表上各手动加一列 `关键词`，类型选**多选**，无需预加任何选项。
> 阶段 1 关键词表使用 `FEISHU_KEYWORD_TABLE_ID`，基础字段为 `规范名`、`类型`、`归一项`、`首次出现`、`备注`；脚本同步字段为 `NEWS次数`、`FILTERED次数`、`最后出现`、`热度样本`。NEWS / FILTERED 仍保留原 `关键词` 多选字段作为原始抽取结果，并新增 `关键词记录` 关联字段。大小写 / NFKC 归一由代码处理，`归一项` 只存真实别名，不写纯 lower 结果。
> 代码通过飞书 Bitable v1 API 写关联字段，`关键词记录` 的值是 record_id 字符串数组，如 `["recxxx"]`。

## 关键词维护

常用命令：

```powershell
.\.venv\Scripts\python.exe merge_keywords.py --sync-core-fields --usage-max-pages 50
.\.venv\Scripts\python.exe merge_keywords.py --llm-fixture-run --provider deepseek --prompt-path docs\local-merge-prompt-simple.md
.\.venv\Scripts\python.exe merge_keywords.py --llm-dry-run --provider deepseek --prompt-path docs\local-merge-prompt-simple.md --llm-group-limit 0
.\.venv\Scripts\python.exe tools\run_keyword_alias_daily.py --dry-run --provider gemini --model gemini-3-flash-preview --keyword-snapshot-path data\keyword_snapshot.json
```

`docs/local-merge-alias-groups.json` 维护跨语言别名种子，例如 `NVIDIA / 英伟达`、`Trump / 特朗普 / 川普`。日常自动归一由本机 `keyword-alias-daily` 任务用 Ark `deepseek-v4-pro` 跑，只把最近新增关键词映射到历史快照中的规范词；规范词优先选择 `NEWS次数 + FILTERED次数` 更高的 KEYWORD 记录。大批量 LLM 泛词审查仍可用官方 Gemini 3 Flash（见 `docs/keyword-operations.md`）。

## 运维恢复

资讯表出现断层时，先看 [docs/rss-gap-recovery.md](docs/rss-gap-recovery.md)。常见恢复方式是把 RSS 源表游标回拨到断层开始时间，重新运行 `rss_ingest.py`；现有 NEWS / FILTERED 的 `item_key` 会被预取去重，已写过的记录不会重复写。

## GitHub Actions 与本地数据桥

生产 RSS 入库由 `.github/workflows/rss-ingest.yml` 运行：每小时 `07 / 27 / 47` 分触发，也支持手动 `workflow_dispatch`。Action 使用 `RSS_SOURCE_MODE=github`，公开 HTTP(S) 源直接抓取；本地私有源通过私有 `rss-runtime-data` 仓库中的 `source-map.json` 映射为 XML 文件，并从同一仓库读取每日 KEYWORD 快照。未映射的 localhost、本地文件和 Grok feed 会被跳过，不计作源失败。

本机只保留数据生产和发布：

- `we-mp-rss` 与 `private-rss` 继续按原频率更新。
- `tools/local_feed_publisher.py` 在 WSL 中观察本地 RSS、Grok feed、外部博客/Reddit 镜像和 KEYWORD 快照；校验通过且内容变化时推送私有数据仓库，并 dispatch 一班入库。GitHub schedule 作为电脑离线时的 best-effort 兜底；两种触发共享 `feishu-write` 并发组，重叠时串行执行。
- 发布器复用 WSL 当前 `gh` 登录态，不把 GitHub Token 写进两个 RSS 容器。
- 电脑关机时，Action 继续处理公开源；开机后发布器启动会主动对账并补推两个本地源。

KEYWORD 别名归一目前仍由本机 Windows 任务计划程序运行，任务名 `keyword-alias-daily`，入口为 `tools\run_keyword_alias_daily_local.ps1`。本地任务使用 Volcengine Ark Coding Plan：

- 每天北京时间 04:00：先归档 NEWS / FILTERED 中 `30d = 0` 的旧资讯，再清理 `30d = 0`、非 `manual`、且首次出现超过保护期的 KEYWORD，然后增量归一，默认真实写飞书。
- LLM provider 为 `ark`，模型固定 `deepseek-v4-pro`，不依赖 GitHub Secrets / Vertex / 本机 Ollama。
- 本地真实写入成功后会更新工作区里的 `data/keyword_snapshot.json`；发布器将其同步到私有运行仓库。snapshot schema v2 包含 `parent_ids` / `owner_ids`，供本机和 GitHub RSS ingest 直接建 KEYWORD 索引。
- 每周自动全量校准已暂停；需要全量校准时手动运行 `full_run=true`。
- 手动 dry-run：`powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\run_keyword_alias_daily_local.ps1 -DryRun`。
- 非零退出会经 `task_alerts.py` 发飞书 webhook 告警（同任务 2 小时冷却）。

关键词维护暂不进入公开 Action 仓库；RSS 主链稳定后再单独迁移其快照和每日维护任务。

## 维护建议

- 修改筛选标准时优先改 `docs/local-screen-prompt.md`。
- 修改事实摘要或 QA 输出风格时优先改 `docs/local-screen-prompt.md`；`docs/local-summarize-prompt.md` 仅是 screen 未输出 `qa` 时的旧 fallback。
- 修改硬过滤词时改 `docs/local-keyword-blocklist.txt`。
- 调关键词抽取规则（type 枚举、数量上限、few-shot 反例）改 `docs/local-screen-prompt.md`。
- 调关键词合并规则优先改 `docs/local-merge-prompt-simple.md`；增加跨语言别名候选改 `docs/local-merge-alias-groups.json`。
- 关键词计数字段同步、合并 dry-run 和边界说明见 `docs/keyword-operations.md`。
- 调整网页全文 fallback 时看 `docs/article-extraction.md`，新增来源 parser 前先确认 feed 缺正文且页面结构稳定。
- 处理资讯断层或回拨 RSS 游标时看 `docs/rss-gap-recovery.md`。
- 配置变更先本地跑 `pytest`，再运行一次小范围真实抓取。
