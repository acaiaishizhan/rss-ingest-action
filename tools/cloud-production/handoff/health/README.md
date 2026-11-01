# SoPilot 生产 owner 的健康与恢复兼容包（未应用）

本包只准备健康改向，不启动生产、不启停任务、不重启工作台。当前 monorepo 业务文件未改变。`baseline-manifest.json` 是本轮读取的现文件字节 SHA-256（含其他会话已改过的基线，不以 HEAD 覆盖它们）。`modified/` 是拟修改副本；`health-owner-handoff.patch` 已在 `apply-check/` 隔离目录真实 `git apply --check` 并 apply。应用前须重新核 baseline SHA；变化时重读协调，禁止覆盖。

## 最小变更

- 新 `tools/info-web-runner/sopilot-owner.mjs`：唯一 owner 读取、已验证生产 receipt 规则、只读 `gh run list/download` 观察。既有 gh 认证自行使用，不读取 token。合法既有配置未写 owner 时默认本地；missing/corrupt 配置或已有 cloud cache 却丢失 owner 时 fail-closed invalid，拒绝本地恢复；只有显式 cloud owner 改向。没有新 service、凭据、CI schedule。
- `producer-health.mjs`：原 Info-Producers-Health 15 分钟入口为 cloud SoPilot 拉原 workflow run artifact，正常其余 lane 不变。
- 工作台 `health/upstream.mjs` / `health/index.mjs`：cloud 时不读本机 SoPilot feed/log，不用旧 local health 覆盖；owner/config/receipt 纳入缓存指纹。采集与发布依据仍是同一云 receipt。停用本机 task 是迁云正常状态；未停用报 owner 冲突；云 unknown/failure/stale 独立可见。
- `remediate.mjs`：cloud/invalid owner 拒绝重跑本机 SoPilot，拒绝发生在查询 scheduler 前；不影响其他 task。
- `diagnosis.mjs` 与 watchdog README 同改运行边界：只看生产 workflow 原 run+capture/publish，禁止诊断 agent 误启/重跑/恢复本地 SoPilot，未知 push/dispatch 不重发；cloud 证据不混入旧本地 health/feed/log。
- watchdog `config.json` / `audit.mjs`：保留 SoPilot task XML 审计，额外只审计非秘密 owner 配置的文件 SHA（原通用 .data 排除会漏掉此权限配置）；不审计其他 .data。

未改任务 adapter 白名单与脚本：`Read-WorkbenchTasks.ps1` 实际只有只读状态查询，无 run/enable API。任务行仍展示其真实 disabled/ready；解释由 ownerTaskCard 兼容层提供。现行 watchdog 自动恢复便捷入口和 diagnosis 自主恢复提示词已同时阻断本地误恢复。

## 激活前唯一 owner 配置（现在没有写入）

现有忽略文件 `tools/info-web-runner/.data/producer-monitor-config.json` 只增量合并下列 `sopilot` 对象，其他 lane 不动：

```json
{"enabled":true,"owner":"cloud","workflow":"sopilot-producer.yml","activated_at":"<统一切换时填写真实UTC时刻>"}
```

这不是靠 enabled=false 隐藏失败。生产切换必须和 publisher owner 交接、旧 producer 停机、pending 核对同一窗口完成；具体生产停启和发布归统一交接包，本包不执行。先应用审核通过的兼容代码，再统一停止旧 producer/转移发布所有权/写真实 owner 激活时刻；之后由既有健康任务自然拉取 receipt。部署代码后，工作台服务是否需要重启由主会话按既有服务规矩统一安排，本轮没有重启。不要清空 health、事件簿或 recovered 字段，不手写成功。

## Receipt 契约与故障语义

`acaiaishizhan/rss-ingest-action`，默认分支 main，workflow `sopilot-producer.yml`；artifact 名 `sopilot-production-receipt-<runid>-<attempt>`，根文件 `receipt.json`。

严格核验 schema_version=1、owner=cloud、mode=production、source=sopilot、workflow；run.repository/id/attempt/url/code_sha 与 gh 元数据一致；只接纳激活之后的 run。capture.status=success，时间和非负计数存在，source SHA-256 须为已冻结采集器 `1c5577a96731a3539285e98e6e689d8f10ee39b7edbf409eff4ddbd8ae123b66`，RSS SHA-256 存在，discovered=entries+prefiltered；AI/Creator 每页的 HTTP+parse 都成功且分页完整。publish.status 只能 confirmed/unchanged，target=feeds/sopilot.xml，target_blob_verified=true，remote_sha/target_blob 均为 Git SHA，capture/publish 时间先后有效。entries=0 只有完整页证据+成功发布才是正常空快照。Action success 本身、shadow receipt、missing receipt 都不算完成。

验证失败/未知不继承本地成功。保留的 last_success 只能来自激活后已验证 cloud receipt。连续已证失败仍按原策略至少两次判 failed；缺首个成功以实际 owner 激活时间计算原 **2 小时**无成功阈值。latest receipt 只有 unknown 时展示 unknown，超过原阈值 timeout；不会靠每15分钟检查刷新成功时间。GitHub 查询失败保留原缓存，缓存/成功超过两小时仍 timeout。未知 push 只查原 run；不会 rerun、dispatch、推送或写 runtime。

## 离线验证（已执行）

从 monorepo 根：

```powershell
node --loader ./temp/cloud-migration-20261102/sopilot-production/health-diff/offline-loader.mjs --test temp/cloud-migration-20261102/sopilot-production/health-diff/modified/tools/info-web-runner/test-sopilot-owner.mjs tools/info-web-runner/test-producer-health.mjs tools/info-web-runner/test-upstream-observations.mjs tools/content-workbench/tests/portable/upstream-health.test.mjs tools/source-watchdog/remediate.test.mjs tools/source-watchdog/audit.test.mjs
```

新测试作为 `tools/info-web-runner/test-sopilot-owner.mjs` 已包含在 patch，应用后直接从 monorepo 根运行，无需 temp driver 或 loader：

```powershell
node --test tools/info-web-runner/test-sopilot-owner.mjs
```

35/35 通过：10 个新增行为测试+25 个现有回归。loader 仅为 temp 测试，将相关模块解析为修改副本，未改依赖继续只读使用主树；未执行正式 gh 读取、业务发送、计划任务动作或模型调用。`tests.log` 有真实输出。`validation.json` 记录 apply-check 与主树 baseline drift=[]。

新增测试覆盖：合法空轮、confirmed/unchanged、shadow/错 SHA/错 attempt/失败分页/漏页/未知 publish 拒绝、最新失败只保留此前 cloud 成功、本地旧 success 不污染、原2h过期、cloud 本机重跑硬拒、disabled正常/Ready冲突、只读原run下载与实际 producer-health 写出测试目录 receipt observation。没有真实服务、Windows scheduler、真实生产artifact 或 cloud publish 的健康端到端验证；这些待主会话统一授权切换后执行。

## 验收与回切清单

1. 核配置真实owner、工作台所读 infoRunnerRoot 与健康任务所在路径一致；核兼容patch基线仍相同。
2. 由统一交接停止旧本机 producer并确认无活进程/未知pending；将共享publisher SoPilot抓取/发布/watch权移交，映射不删。
3. 确认云生产实际 capture + remote readback publish receipt，检查原 run id/attempt/headSha/target_blob 与artifact；失败/未知不从shadow填健康。
4. 读取 /api/v1/workflow-health：SoPilot采集发布指向生产云receipt；本机disabled正常；模拟只在离线夹具做，真实失败不得屏蔽。既有2h阈值与源/游标/评分不变。
5. 回切先查原cloud run和runtime最新commit/blob/pending，携最新已确认状态/源游标；先暂停cloud唯一owner再按统一交接恢复local owner。本机任务只能在owner显式local且待处理凭证已核清后恢复；不回滚旧状态、不清健康、不恢复旧Info任务。
