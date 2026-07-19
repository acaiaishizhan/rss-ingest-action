# GitHub Actions RSS 运行说明

生产结构：

```text
Windows Docker RSS / Grok / KEYWORD snapshot
                    |
                    v
       WSL local_feed_publisher.py -> private rss-runtime-data
                                             |
GitHub schedule (07/27/47) ------------------+
                    |
                    v
         public rss-ingest-action -> Feishu
```

## 仓库

- `acaiaishizhan/rss-ingest-action`：公开代码和 RSS Action。
- `acaiaishizhan/rss-runtime-data`：私有，保存 `source-map.json`、本地 RSS / Grok XML 快照和 `keyword_snapshot.json`。

现有带历史的私有仓库不要直接改成公开；公开仓库从经过扫描的工作树快照建立。

## 本地发布器

发布器运行在 WSL `Ubuntu-22.04`，复用用户 `openclaw` 已登录的 GitHub CLI。默认观察：

- `/mnt/f/coding/solo-company/tools/private-rss/data/all.xml`
- `/mnt/f/coding/we-mp-rss/data/db.db` 及其 WAL/SHM 文件

Windows 任务每 10 分钟以 `--once` 运行一次，读取 `http://127.0.0.1:8001/feed/all.rss`、private-rss 的 `all.xml`、Grok feeds、6 个 Substack feed、PromptHub 官方博客和本机 KEYWORD 快照。只有 XML/JSON 合法且语义内容发生变化时才提交；`lastBuildDate` 等 feed 级时间戳变化会忽略。we-mp-rss 的新条目若正文尚未生成，会等待下一班；超过 1 小时仍为空的坏条目会从发布快照中剔除，避免永久卡住其余正常文章。外部博客镜像抓取失败属于软失败，会保留最后一份好快照。发布器只推送数据，不再触发 Action；固定 GitHub schedule 负责入库，避免 push dispatch 与 schedule 重叠。

私有仓库的数据提交采用滚动快照：如果当前 HEAD 已经是数据提交，发布器会 amend 并用 `--force-with-lease` 更新，只保留最新 XML，避免小时级更新让 Git 历史无限增长。配置提交不会被覆盖。

手动单次同步：

```powershell
wsl.exe -d Ubuntu-22.04 -- /usr/bin/python3 /mnt/f/coding/rss-ingest-local/tools/local_feed_publisher.py --once
```

迁移预检阶段只推送 XML、不触发入库：

```powershell
wsl.exe -d Ubuntu-22.04 -- /usr/bin/python3 /mnt/f/coding/rss-ingest-local/tools/local_feed_publisher.py --once --no-dispatch
```

随后在公开仓库手动运行 `rss-ingest`，勾选 `preflight_only`。这一步只验证 Secrets、私有仓库 checkout、source-map、XML 和 KEYWORD 快照，不请求或写入飞书数据。

注册隐藏的登录启动任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\register_local_feed_publisher_task.ps1
```

日志和状态位于 WSL：

```text
/home/openclaw/.local/state/rss-ingest/publisher.log
/home/openclaw/.local/state/rss-ingest/publisher-state.json
```

## Action 来源路由

Action 设置 `RSS_SOURCE_MODE=github`。规则如下：

- 公网 HTTP(S) 源正常运行。
- `source-map.json` 中显式映射的 18 个飞书 RSS 记录改读私有 XML：we-mp-rss、private-rss、9 个 Grok feed、6 个 Substack feed 和 PromptHub 官方博客生成 feed。
- `keyword_snapshot.json` 提供每日关键词索引；每班对快照未命中的新词仍会只读查询飞书再决定是否创建，避免两班之间重复建词。
- 其他 localhost、私网 IP、本地路径和 Grok 文件源会跳过，不计作失败。
- 私有仓库 checkout 失败时，公开源仍继续，Workflow 会记录 degraded warning。

## GitHub LLM provider

GitHub runner 使用 Ark Coding Plan 的 `deepseek-v4-flash`，Secrets 为
`ARK_API_KEY`、`ARK_BASE_URL`、`ARK_MODEL`。2026-07-15 曾因账户额度耗尽返回
`HTTP 429 AccountQuotaExceeded`，额度恢复并通过最小请求验证后已切回 Ark；
`DEEPSEEK_API_KEY` 仅保留为需要人工切换时的应急 provider，不参与当前生产运行。

当一班 run 的所有 queued items 都在 LLM 阶段失败时，`rss_ingest.py` 必须返回
非零退出码，避免 GitHub 把“0 条真正处理”的班次标成绿色成功。Workflow artifact
同时保留 HTTP、JSON 解析和文本去重失败审计日志，便于区分额度、认证、限流和格式问题。

## 云端定时器

公开仓库的原生 schedule 每小时 `07 / 27 / 47` 分运行，共 72 班/天。仓库变量
`RSS_INGEST_ENABLED=true` 时生效。Pipedream 保持 Draft / OFF，本机发布器也不再发送
`workflow_dispatch`；生产环境只有一个定时入口。

## 回滚

1. 禁用公开仓库 `rss-ingest` Workflow 的 schedule。
2. 重新启用 Windows 任务 `rss-ingest-fetch`。
3. 不需要修改飞书源表 URL；来源覆盖只在 GitHub 运行时生效。
