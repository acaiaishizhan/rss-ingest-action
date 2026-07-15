# GitHub Actions RSS 运行说明

生产结构：

```text
Windows Docker RSS -> WSL local_feed_publisher.py -> private rss-runtime-data
                                                       |
                                                       v
public rss-ingest-action workflow -> Feishu
```

## 仓库

- `acaiaishizhan/rss-ingest-action`：公开代码和 RSS Action。
- `acaiaishizhan/rss-runtime-data`：私有，只保存 `source-map.json` 与本地 RSS / Grok XML 快照。

现有带历史的私有仓库不要直接改成公开；公开仓库从经过扫描的工作树快照建立。

## 本地发布器

发布器运行在 WSL `Ubuntu-22.04`，复用用户 `openclaw` 已登录的 GitHub CLI。默认观察：

- `/mnt/f/coding/solo-company/tools/private-rss/data/all.xml`
- `/mnt/f/coding/we-mp-rss/data/db.db` 及其 WAL/SHM 文件

检测到文件变化后等待 90 秒；随后读取 `http://127.0.0.1:8001/feed/all.rss` 和 private-rss 的 `all.xml`。只有 XML 合法、至少包含一个 item，且 item/entry 的语义指纹发生变化时才提交；`lastBuildDate` 等 feed 级时间戳变化会忽略。推送成功后触发公开仓库的 `rss-ingest.yml`。

私有仓库的数据提交采用滚动快照：如果当前 HEAD 已经是数据提交，发布器会 amend 并用 `--force-with-lease` 更新，只保留最新 XML，避免小时级更新让 Git 历史无限增长。配置提交不会被覆盖。

手动单次同步：

```powershell
wsl.exe -d Ubuntu-22.04 -- /usr/bin/python3 /mnt/f/coding/rss-ingest-action/tools/local_feed_publisher.py --once
```

迁移预检阶段只推送 XML、不触发入库：

```powershell
wsl.exe -d Ubuntu-22.04 -- /usr/bin/python3 /mnt/f/coding/rss-ingest-action/tools/local_feed_publisher.py --once --no-dispatch
```

随后在公开仓库手动运行 `rss-ingest`，勾选 `preflight_only`。这一步只验证 Secrets、私有仓库 checkout、source-map 和 XML，不请求或写入飞书数据。

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
- `source-map.json` 中显式映射的两个飞书 RSS 记录改读私有 XML。
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

## 可选云端定时器

Pipedream 工作流 `RSS Ingest Dispatch` 已准备为每 20 分钟触发一次
`workflow_dispatch`。当前保持 Draft / OFF；需要电脑关机后仍持续抓公网源时再部署。
免费计划不使用 10 分钟频率：144 次/天会超过当前 100 credits/天，20 分钟频率
为 72 次/天并避免与 9–15 分钟的 GitHub ingest 大量重叠。

Pipedream OFF 时，本机 `rss-local-feed-publisher` 仍会在快照内容变化并成功推送后
触发一次 `workflow_dispatch`，因此本机开机期间主链不受影响。

## 回滚

1. 禁用公开仓库 `rss-ingest` Workflow 的 schedule。
2. 重新启用 Windows 任务 `rss-ingest-fetch`。
3. 不需要修改飞书源表 URL；来源覆盖只在 GitHub 运行时生效。
