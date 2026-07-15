# grok_watch 运维说明

`grok_watch.py` 定时通过网页端 Grok 搜 X 社媒信号：AI 新闻、实战案例、技巧、资源、coding agent 玩法、prompt / skill 等。校验后写成本地 RSS 文件，由 `rss_ingest.py` 当普通源消费。默认不走 xAI API credits。Reddit lane 已暂停，后续单独做 Reddit 管道。

2026-06-16 起生产路径为 `GROK_WATCH_TRANSPORT=web`：`grok_watch.py` 调用公共工具 `F:\coding\solo-company\skills\grok-browser`，由它连接本机 gpt-browser Chrome DevTools endpoint，打开 `grok.com`，切到 `Expert`，提交原 topic prompt 并读取网页回答。`grok-browser` 默认 offscreen/no-focus 运行并在完成后清理命令页；需要可见调试时才加 `--foreground --focus --keep-page`。CLI 和 API transport 仅作为显式备用。

## 数据流

```
任务计划 grok-watch-hourly（每 20 分钟唤醒，:03 / :23 / :43）
  → grok_watch.py 按 topic.schedule_times 判断当前时隙是否到期
  → Grok web transport（原 topic prompt → grok-browser CLI → gpt-browser Chrome → grok.com Expert）
  → 三道闸门：JSON/URL 结构校验 → fxtwitter 回查 → seen 去重 + 硬规则
  → data/grok-feeds/<topic>.xml
  → rss_ingest.py 正常调度读取（rss_parser 支持本地路径）
  → screen 评分 → 飞书 NEWS / FILTERED
```

## 话题配置（docs/local-grok-topics.json）

| key | 话题 | 平台 | 间隔 | 固定时刻 | 搜索窗口 | 标题前缀 |
|---|---|---|---|---|---|---|
| ai_news | 纯AI新闻 | X | 8h | 07:03 / 13:03 / 21:03 | 10h | `[AI新闻] ` |
| deals | AI羊毛 | X | 24h | 01:23 | 48h | |
| rumors | 小道消息 | X | 12h | 00:23 / 18:43 | 24h | `[未核实] ` |
| cases | 实战与赚钱案例 | X | 12h | 08:23 / 16:23 | 24h | |
| burst | 选题爆款雷达 | X | 12h | 07:23 / 19:23 | 24h | `[爆款] ` |
| tips | AI技巧 | X | 8h | 02:23 / 10:23 / 18:23 | 24h | |
| peers | 同行launch雷达 | X | 24h | 20:43 | 48h | |
| resources | 高热资源 | X | 6h | 03:23 / 09:23 / 15:23 / 21:23 | 24h | `[资源] ` |
| codex | AI Coding Agent玩法 | X | 8h | 04:43 / 12:43 / 22:43 | 24h | `[Coding] ` |
| claude | 热门Prompt与Skill | X | 8h | 05:23 / 13:23 / 23:23 | 24h | `[Prompt] ` |

合计 24 个 Grok 网页端调用位/天，全部是 X 搜索。Reddit topic 在 `docs/local-grok-topics.json` 中保留但 `enabled=false`，不会被 `grok_watch.py` 加载；Reddit 后续单独做，不再混进 Grok 搜索排期。Windows 任务每 20 分钟醒来一次，固定使用 `:03 / :23 / :43` 三个时隙，避免同一时刻挤多个 Grok 调用。`schedule_times` 是排期真源；旧 `schedule_hour` 仅作兼容。`GROK_WATCH_MAX_TOPICS_PER_RUN` 默认仍限制为 2，作为配置错误或手工改 state 时的保险；手动 `--force` 不受该限制。撞 SuperGrok 限流先降 tips / cases / resources / codex / claude 的频率。

暂停的 Reddit topic：`reddit_rumors` / `reddit_cases` / `reddit_burst` / `reddit_tips` / `reddit_resources` / `reddit_codex` / `reddit_claude`。

## 文件与组件

- `grok_watch.py` — 主脚本（根目录，独立于 RSS 主流程，不依赖飞书）
- `F:\coding\solo-company\skills\grok-browser` — 网页端 Grok 公共工具真源：连接 gpt-browser 的 Chrome endpoint，选择 `Expert`，提交 prompt，读取最新 assistant JSON
- `tools/grok_web_runner.js` — 已废弃的旧网页端 runner。默认不再使用；只有显式设置 `GROK_WATCH_ALLOW_LEGACY_WEB_RUNNER=1` 且新 `grok-browser` CLI 缺失时才作为短期 fallback。
- `docs/local-grok-prompts/<topic>.md` — 启用话题的搜索 prompt；当前 Grok 排期只启用 X topic。
- `data/grok_watch_state.json` — 运行状态：每话题 `last_run_ms` + `recent_items`，全局 `seen_posts`（status_id 池）+ `seen_text`（文案指纹池），14 天滚动（gitignored）
- `data/grok-feeds/<topic>.xml` — 产物 feed（gitignored），feed 内保留近 72h、最多 50 条
- `C:\Users\24599\.grok\bin\grok.exe` — CLI 备用路径的外层 `grok-guard` wrapper；真实 CLI 保存在同目录 `grok-real.exe`。
- 任务调用链：`grok-watch-hourly`（`wscript.exe`）→ `tools/run_grok_watch_hidden.vbs`（隐藏窗口启动器）→ `tools/run_grok_watch_local.ps1`（runner）→ `grok_watch.py`，输出写 UTF-8 日志 `grok_watch_runs.log`。用 wscript 隐藏启动是因为任务是 InteractiveToken 跑在桌面会话，直接 `powershell.exe` 会闪蓝窗。
- `tools/register_grok_watch_task.ps1` — 任务注册脚本（每 20 分钟触发、IgnoreNew 防重叠、InteractiveToken）。**改任务启动方式只改这里**：它已注册为走上面的隐藏启动器；重跑此脚本会以隐藏方式重建任务，旧版直接 powershell 启动的写法已废弃。
- 飞书 RSS 源表需要为每个要入库的话题配置 1 行：名称 `Grok搜索-<话题名>`，feed_url 填本地绝对路径 `F:\coding\rss-ingest-local\data\grok-feeds\<key>.xml`

## 手动运行

```powershell
# 只跑指定话题、忽略到期判断
.\.venv\Scripts\python.exe grok_watch.py --topic deals --force
# 按到期正常跑（任务计划等价行为）
.\.venv\Scripts\python.exe grok_watch.py
```

## 关键设计决策（改动前必读）

1. **feed 的 pubDate = 入库时刻，不是推文真实发布时间。** grok 搜回来的是历史推文，若 pubDate 用真实时间，会早于 rss_ingest 的增量 cutoff（`last_item_pub or last_fetch`）而被时间窗砍掉，表现为日志 `[RSS] Grok-X new=0`、内容永不入库（2026-06-12 实际踩坑，commit d102d38 修复）。推文真实时间保留在 description 的 `[来源]` 行。**不要"修正"回真实时间。**
2. **作者 / 时间必须回查。** X 一律以 fxtwitter 回查为准。Grok 返回的 author / posted_at 只能当线索，不能直接入库。
3. **网页端 transport 依赖 grok-browser + gpt-browser endpoint。** 默认 `grok-browser` CLI 是 `F:\coding\solo-company\skills\grok-browser\bin\cli.js`，可用 `GROK_WATCH_GROK_BROWSER_CLI` 覆盖；默认 endpoint 文件是 `%LOCALAPPDATA%\gpt-browser\state\chrome-ws-endpoint.txt`，Node 依赖默认从 `%APPDATA%\npm\node_modules\gpt-browser\node_modules` 读取。到期话题执行前会真实连接 endpoint 的本机 TCP 端口；文件缺失、内容非法或端口已死时自动调用 `gpt-browser launch`，刷新 endpoint 后再检查一次。可用 `GROK_WATCH_GPT_BROWSER_COMMAND` / `GROK_WATCH_GPT_BROWSER_LAUNCH_TIMEOUT_S` 覆盖启动命令和超时。Grok 登录态仍需预先存在。
4. **网页端默认用 Expert。** `grok-browser send --model Expert --json` 会在提交 prompt 前打开模型菜单并选择 `Expert`，同时默认 offscreen/no-focus、完成后清理命令页；可用 `GROK_WATCH_WEB_MODEL` 覆盖。不要改回 `Fast`。
5. **CLI transport 只是备用。** 2026-06-15 实测 CLI 参数不是完整安全边界，`grok-real.exe` 内部仍可能启动额外子进程并打开 X 页面。只有显式设置 `GROK_WATCH_TRANSPORT=cli` 时才走 CLI，并且必须保留外层 wrapper/进程树监控。
6. **API transport 是显式备用，不是默认。** 只有设置 `GROK_WATCH_TRANSPORT=api` 时才读取 `XAI_API_KEY` / `GROK_API_KEY` 并走 xAI API credits。

## 四层去重

| 层 | 位置 | 拦什么 |
|---|---|---|
| status_id seen 池 | grok_watch | grok 反复返回同一热帖（实测 2 分钟内两轮重 2 条） |
| 文案指纹 seen 池 | grok_watch | 营销矩阵多账号同文案 |
| item_key（x.com URL） | rss_ingest 入口 | 跨源重复（如已被 AI HOT 聚合源抓过） |
| LLM 文本判重（7 天窗口） | screen 后 | 同一事件不同帖子 / 转述 |

已知缝隙：seen 池 14 天滚动 + 飞书记录被季度归档的组合下，老帖理论上可能重进，概率极低，观察期留意即可。

## 故障与补跑

- **某话题内容没进飞书**：先看 `grok_watch_runs.log`（grok 侧）再看 ingest 日志中 `[RSS] Grok搜索-X new=N`。若 feed 有内容但 `new=0`，参考 `docs/rss-gap-recovery.md` 的思路回拨该源的 `last_fetch_time` / `last_item_pub_time` 为 0 强制重抓（用 `update_bitable_record_fields` 写 RSS 源表对应行即可）。
- **grok_watch 返回 2**：通常是 `GROK_WATCH_TRANSPORT=web` 但 gpt-browser endpoint 文件不存在、Node 命令找不到、`grok-browser` CLI 文件不存在、`puppeteer-core` 不可解析；或显式设置了 `api` 但没有 `XAI_API_KEY` / `GROK_API_KEY`。
- **`ECONNREFUSED 127.0.0.1:<port>`**：endpoint 文件指向已经退出的 Chrome。当前版本会先运行 `gpt-browser launch` 自愈；只有启动失败或新 endpoint 仍不可达才退出并告警。日志出现 `gpt-browser endpoint recovered` 表示已恢复后继续执行，不需要手动补跑。
- **网页端提示未登录 / 找不到输入框**：先打开 gpt-browser 里的 `https://grok.com/`，确认能看到输入框和账号头像，再重跑。
- **找不到 Expert**：Grok 网页 UI 或账号权限变了；先手动打开模型菜单确认是否还有 `Expert`，不要静默降级到 `Fast`。
- **单个 topic 超时**：`grok_watch` 会杀掉 Grok 进程树并停止该 topic，不再重试同一轮；这是为了防止一次复杂 prompt 连续烧两轮。需要恢复召回时先调 prompt / topic 频率，不要直接加大并发。
- **后台又打开 X 页面**：如果 transport 是 `cli`，立即 `Disable-ScheduledTask -TaskName grok-watch-hourly`，再查 `C:\Users\24599\.grok\guard\launches.jsonl`。web transport 本来就会使用 gpt-browser 里的 Grok 页面，但不应打开 X 页面。
- **入库延迟**：本机生产当前 `rss-ingest-local.env` 覆盖 `DEFAULT_FETCH_INTERVAL_MIN=0`，`rss-ingest-fetch` 任务每 10 分钟跑一次，所以 grok_watch 写完 feed 后通常由下一轮 ingest 消费。若 feed 恰好在 ingest 本轮抓取之后才写完，会延后一轮。若恢复示例默认 `DEFAULT_FETCH_INTERVAL_MIN=180`，新内容最多会延迟约 3 小时。
- **任务窗口又开始每小时闪**：八成是任务被旧版逻辑（直接 `powershell.exe` 启动）重注册了。重跑 `tools/register_grok_watch_task.ps1` 即可恢复 wscript 隐藏启动。
- **grok 突然不产新内容、`grok_watch_runs.log` 和 `data/grok_watch_state.json` 的 mtime 冻住，但任务 `LastResult=0`**：多半是 `tools/run_grok_watch_*.ps1` / `*.vbs` 里混入了非 ASCII（中文）注释。任务用 `powershell.exe`(Windows PowerShell 5.1) 和 `wscript` 按系统 ANSI 代码页（zh-CN 下是 GBK）解析**无 BOM** 脚本，中文注释会让解析错乱、python 根本不执行却仍 `exit 0`。曾导致 2026-06-12（commit 956d2d8 给 ps1 加中文注释）→ 2026-06-15 静默哑火 2.5 天。**这些启动脚本必须保持纯 ASCII**（注释用英文）或存为 UTF-8 with BOM。验证别只看退出码：裸跑 `.\.venv\Scripts\python.exe grok_watch.py` 对照，看 log/state 的 mtime 是否**真的前进**。

## 已知问题（与 grok 无关但排查时会撞见）

- 2026-06-16 已给 NEWS / FILTERED 的「全文」字段加 `FEISHU_TEXT_CELL_SAFE_LIMIT=80000` 截断保护，解决长帖或长文触发的飞书 `TooLargeCell`（code 1254130）。如果之后仍出现 `TooLargeCell`，优先检查关键词多选 / 关联字段是否超出飞书单元格限制。

## 观察期清单（建议跑两周后回顾）

- 每话题入库率 / dup 率（`grok_watch_runs.log` 的 `dropped` 统计）
- FILTERED 中 grok 帖的误杀率（社区帖被 screen 打低分的比例）
- grok 月额度实际消耗 vs 3% 基线
- prompt 微调：tips 是唯一未实测调优过的话题
