# SoPilot owner 交接准备（未应用）

本包只生成副本与 patch。当前生产采集、publisher、任务、入池、游标均未改变；真实 pending、任务实时状态、最新 runtime HEAD 尚未核查，不能写“pending 0”。独立 shadow 和另外一路 24h 对照由主会话继续。本包不重复排期，不恢复 sopilot-info、Info 旧 supervisor、关键词维护、国学或旧下载队列。

## 可审查变更

- `sopilot-only.patch`：仅删除 `PublisherConfig.from_env()` 的 SoPilot SourceSpec 和 `watch_paths` 的 SoPilot 文件；常量及映射保留。适合只交 SoPilot owner。
- `combined-three-source.patch`：在上述基础上，仅在构建 SourceSpec 时过滤 PromptHub / Reddit IndieHackers。**二选一，不依次叠加应用。** 迁出目标严格为 `feeds/sopilot.xml`、`feeds/public-mirror/prompthub-blog.xml`、`feeds/public-mirror/reddit-indiehackers-ai-revenue.xml`。另 8 个公开镜像仍留本机原样；Grok 不变。
- 本轮 temp `sopilot-production/owner-diff/baseline/tools/local_feed_publisher.py` 是开工时**当前有未提交改动的文件副本**，不是仓库 HEAD 的替代版。完整 baseline/modified 副本不随生产分支提交。此目录只带 `baseline-manifest.json` 保存当前内容 SHA256 与相关代码指纹。应用前重算 SHA256，变化则重新从最新文件生成局部 diff，不整文件覆盖。
- 映射仍来自 `SOPILOT_SOURCE_URL`、`SUBSTACK_FEED_TARGETS`、`REDDIT_FEED_TARGETS`；`tools/build_runtime_source_map.py` 不变。特别保留 PromptHub 的历史 Substack URL → 现有 feed 目标映射。不能因移交删除来源、重建 record_id 或改消费窗口。
- `verification.json`：两个 patch 均在真实生产工作树执行 `git apply --check` 通过（仅检查，未应用）；语法编译通过；除指定 sources/watch 外配置、其他源顺序/内容、Grok scope、普通 RSS dispatch、immutable 设置均相同。生产文件字节和 git status 前后相同。

## 统一切换顺序（当前未执行）

1. 主会话先确认云生产代码已审、写权限有效、shadow 通过、健康/恢复 diff 已准备，云生产 publish gate 仍关闭。不要以停本机换取等待权限；先把独立准备做完。
2. 选择紧接本机 SoPilot 每小时 `:02` 完成、避开 publisher `:05/:20/:35/:50` 的短维护窗口。SoPilot 注册器预算 20 分钟、IgnoreNew；publisher 的一次执行也可能长于间隔，不能只看钟点假定空闲。
3. 先禁止 SoPilot 下次触发；活跃执行继续等终态，不杀进程。只在主会话已批准切换时使用以下命令，**本包未执行**：

   ```powershell
   Get-ScheduledTask -TaskName 'sopilot-watch-hourly' | Select-Object TaskName,State
   Get-ScheduledTaskInfo -TaskName 'sopilot-watch-hourly' | Select-Object LastRunTime,LastTaskResult,NextRunTime
   Disable-ScheduledTask -TaskName 'sopilot-watch-hourly'
   # 活 run 等终态后，再查结果；禁用状态并不证明子进程已结束。
   Get-ScheduledTaskInfo -TaskName 'sopilot-watch-hourly' | Select-Object LastRunTime,LastTaskResult,NextRunTime
   Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $LocalRepo 'data/sopilot-feed.xml')
   Get-Item -LiteralPath (Join-Path $LocalRepo 'data/sopilot-feed.xml') | Select-Object Length,LastWriteTimeUtc
   ```

   同时保存最终 RSS 的 GUID 集合和 `out/sopilot-watch.log` 对应本轮起止/结果。不要把旧的本地快照当云生产首次输入。
4. 给**普通 RSS publisher**约一个既有周期的维护窗口，阻止下一次触发但保留现有任务定义及时间，不停 Grok publisher、不取消已执行请求。可在批准窗口 `Disable-ScheduledTask -TaskName 'rss-local-feed-publisher'`，待其当前 run 终态后核原锁已空闲。用原锁协调，不删除锁、不手动抹 state；若期限内未结束，继续等并延后接管，不强杀。若注册器的 RestartCount 已排恢复、或有独立常驻调用，也须在窗口前核实后续触发已被阻止。
5. **先执行下节 pending 审计并核清原操作，再应用选定 owner patch。** `sync_once()` 恢复 ahead commit、push_pending、managed 脏文件早于 pull；不能靠删 SourceSpec 或 `--no-dispatch` 阻止旧混合 commit 被推。所有未知结果先查原提交/原 run，不重发。
6. 原 publisher 已静止、旧 pending 全部有结论、正式目标无待回灌旧 blob后，应用选定 patch，并与健康/恢复指向 diff 同窗交接。`git apply --check` 再通过才 `git apply`；只由主会话协调应用。不要注册新时间表，不运行旧注册器 `-Force` 覆盖任务。
7. 恢复普通 RSS publisher 的既有任务（`Enable-ScheduledTask -TaskName 'rss-local-feed-publisher'`），只发布剩余本机源；SoPilot task 保持禁用。原普通入池 dispatch 和 source-map 继续不变。
8. 确认本地 owner 已释放，才打开云生产唯一 owner 的 publish gate；首次写前读最新 runtime HEAD/目标 blob，写后核 readback、改动路径只有白名单、非目标 blob 不变、普通非 force push、capture/publish 回执与 run 一一对应。SoPilot 仍每小时一次，不把 6h 榜单窗口改长/改短，不依赖影子 artifact 作生产真源。
9. 健康必须读取真实 capture + publish 回执；本地 SoPilot task Disabled 被视为该 owner 的正常状态。source-watchdog、producer-health、工作台恢复入口不能再自动恢复旧任务。此项由主会话的健康 diff负责，本包不以改阈值或伪造 recovered 替代。

## Pending 只读核查命令与判定

先在原 WSL 身份和既有认证下确认实际 config 的 state/checkout/lock 路径。默认分别为 `$HOME/.local/state/rss-ingest/publisher-state.json`、`$HOME/.local/share/rss-runtime-data`、同 state 目录 `publisher.lock`；生产若有 `LOCAL_FEED_PUBLISHER_STATE_DIR` / `RSS_DATA_REPO_DIR` 覆盖，使用实际路径。**不 source、cat 或打印 env/auth 文件。** 现行 runner 不主动 source .env；不要为审计擅自补环境或切账号。

以下 Bash 命令是交接时执行的准备清单，本包没有执行；`handoff_dir` 指向本包、`audit` 指向一个全新的审计目录。脚本只打印标识符及安全形态的 GitHub run URL，不打印 receipt 原文或错误正文。

```bash
state="$HOME/.local/state/rss-ingest/publisher-state.json" # 核实际 override
repo="$HOME/.local/share/rss-runtime-data"               # 核实际 override
python3 "$handoff_dir/pending_state_summary.py" --state "$state"
git -C "$repo" status --short
git -C "$repo" rev-parse HEAD
git -C "$repo" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}'
git -C "$repo" rev-list --count '@{upstream}..HEAD'
git -C "$repo" diff --name-status HEAD --
git -C "$repo" diff --cached --name-status --
git -C "$repo" ls-files --others --exclude-standard
# 不打印 remote URL；它可能内嵌认证。以下 remote/branch 按上行实际 upstream 分拆。
git -C "$repo" ls-remote origin refs/heads/main

# 为 ancestry 使用最新远端对象，但不 fetch/pull 到原 publisher checkout。
# audit 目录必须不存在；已有则选一个新名字，不删除/覆盖。
gh repo clone acaiaishizhan/rss-runtime-data "$audit/runtime.git" -- --bare
git -C "$audit/runtime.git" fetch --no-tags "$repo" HEAD
remote_sha=$(git -C "$audit/runtime.git" rev-parse refs/heads/main)
local_sha=$(git -C "$repo" rev-parse HEAD)
git -C "$audit/runtime.git" merge-base --is-ancestor "$local_sha" "$remote_sha"
# exit 0 已发布；1 未被远端包含；>1 无法判断。保留退出码。
# pending_sha/published_sha 来自脚本的已校验标识符，逐项重复检查，不拿空值调用。
git -C "$audit/runtime.git" cat-file -e "$pending_sha^{commit}"
git -C "$audit/runtime.git" merge-base --is-ancestor "$pending_sha" "$remote_sha"
git -C "$audit/runtime.git" rev-list --parents -n 1 "$pending_sha"
git -C "$audit/runtime.git" diff-tree --root -m --no-commit-id --name-status -r "$pending_sha"
git -C "$audit/runtime.git" diff --name-status "$remote_sha" "$pending_sha" --
for target in feeds/sopilot.xml feeds/public-mirror/prompthub-blog.xml feeds/public-mirror/reddit-indiehackers-ai-revenue.xml; do
  git -C "$audit/runtime.git" rev-parse "$pending_sha:$target"
  git -C "$audit/runtime.git" rev-parse "$remote_sha:$target"
  git -C "$repo" hash-object -- "$target"
done
# 只有读调用；原 dispatch_id 与 displayTitle=publish-<id> 对照。
gh run list --repo acaiaishizhan/rss-ingest-action --workflow rss-ingest.yml --event workflow_dispatch --limit 100 --json databaseId,displayTitle,url,headSha,status,conclusion,createdAt
gh run view "$original_run_id" --repo acaiaishizhan/rss-ingest-action --json databaseId,displayTitle,url,headSha,event,status,conclusion,jobs
```

若 pending commit 不是 local HEAD 的祖先，审计 clone 里可能没有该对象：先 `git -C "$repo" cat-file -e "$pending_sha^{commit}"` 核原对象存在，再在**审计副本** `git fetch --no-tags "$repo" "$pending_sha"` 引入该精确对象；失败保留为未核，不能猜。远端默认分支先用 `gh repo view acaiaishizhan/rss-runtime-data --json defaultBranchRef` 核实，上述 `main` 在不符时替换；不得创建/推另一个正式分支。克隆是既有 CLI 认证自行使用，不搬 token。

逐项形成移交表，不能把一个 `push_pending=false` 当所有操作已清：

1. `pending_commit / published_commit / merged_commits`：记录 SHA、父提交、与最新远端的 ancestry、其相对父/远端的全部 changed paths。merge commit 要逐父核，不能仅看默认 `git show`。每个目标同时核 commit blob、远端 blob、本地工作文件 blob，区分“提交已在远端历史但后来被更新”和“当前远端正好同一份”。
2. `push_pending=true` 或本地 ahead>0：若 SHA 已被远端包含，则是原 push 已成功但回执可能未保存；须由原 publisher 恢复这条**已确认**记录。若未包含且混入本机其他源，要在云 gate 关闭时由主会话先核完 blob 并完成既有原提交的非 force 发布，再释放 owner。`push_force=true` 且尚未发布进入人工协调，immutable 路线不能替它 force；不清 state、不换旧备份、不直接重跑 `sync_once` 试运气。
3. 本地未提交/暂存/未跟踪目标：列全路径与 hash；选定迁出路径必须没有可被旧 owner 恢复的待发布版本。其余本机源成果保留；如同一批有 SoPilot 与其他源，主会话决定在 gate 关闭窗口先完成已核版本，不能删除混合成果。无关暂存改动会被 publisher 拒绝，不能借机撤暂存。
4. `dispatch_pending / dispatch_started / dispatch_id / dispatch_receipt`：有 `started` 或不明 receipt，先查 `publish-<id>` 原 run，匹配时间/引用/操作 ID并跟到终态；列表 100 条不足须分页或按时间翻查，查不到保持“未知，不重发”。有多条同 ID 不选一条冒充确认。HTTP 明确拒绝或工具未启动证据才说明未提交，不能凭超时/进程退出判断。`headSha` 是 ingest **代码** SHA，不是 runtime 数据 SHA，核消费版本还要看该 run 的 runtime checkout 回执/日志/产物，不能混用。
5. 有已受理但尚未消费的旧 dispatch，先等原 run 终态并核其消费的 runtime SHA；无新 dispatch。只有所有旧 push/dispatch 的结果与原记录对应，才解除新 owner gate。状态修复由主会话逐条批准执行，本包不自动修改 state 或手写 recovered。

## 游标与回切

SoPilot producer 没有 seen 表/增量游标；榜单每次抓当前 6h 快照，GUID 仍为原 tweet ID URL。迁移续接的是**同一普通 RSS 源 record_id**：`last_fetch_time / last_item_pub_time / last_item_guid / failed_items` 及现行 watch_state。当前 `rss_ingest.py` 优先 `last_item_pub_time`、为空才 `last_fetch_time`，减既有 lookback；失败池可把 cursor 限到最早失败发布时间之前。source-map 只读找出现有 `feeds/sopilot.xml` 对应唯一 record_id，普通 ingest 自己继续推进，不清/重建/回拨来源或评分规则。

回切时先关闭**云生产本件**后续发布并等已受理 push/run 全部核清，保留 shadow。只读记录最新 runtime SHA/三目标 blob 和最新正式来源游标/失败池，而非切换前备份。用已确认有效的现有本机 publisher 认证（不搬云凭据）核 runtime 可访问；先让本机 producer 重新采一份当前有效 SoPilot，再由主会话在 publisher 静止窗口从**最新实际文件**恢复对应 SourceSpec/watch（三源移交则逐源决定 owner，不把另两源一起抢回）。原 checkout/pending 必须与最新远端协调后才恢复任务，不能先 enable 再让它把旧 XML 回灌。

准备命令为 `Enable-ScheduledTask -TaskName 'sopilot-watch-hourly'`，本包未执行。健康和恢复入口同窗改回真实本机 owner，确认一轮 capture→publish→普通消费；不清状态、不还原旧游标/备份。6h 停顿以外的下榜项不能声称自动补回，缺口另走既有受控恢复、另行授权。
