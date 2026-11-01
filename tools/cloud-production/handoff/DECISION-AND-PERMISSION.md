# SoPilot 生产接管包：实现选择与唯一待批权限

状态：仅准备。未创建 key/secret、未改权限或认证、未停本机 task、未开云生产 owner gate、未写正式 runtime、未新增 ingest dispatch。默认 main 和 24h readonly shadow 由另一路统一，本分支不重复排期。

## 最短正式链路

保留已有 public `acaiaishizhan/rss-ingest-action` 执行器，调用现行 SoPilot 采集器，每小时 `:02` 抓当前 6h AI/Creator 公开榜，按原过滤、字段、GUID 生成快照，再非 force 发布到已有 private `acaiaishizhan/rss-runtime-data` 的 `feeds/sopilot.xml`。名义频率保持每小时一次；GitHub schedule 可能延迟，`:02` 不代表准点承诺。抓取失败保留上一份有效快照；完整解析成功、计数闭合的合法零条与失败分别记录，不偷偷扩大消费窗口或追加历史状态。

正式 consumer 继续读取原 source-map、原 record_id 和当前正式游标，普通 RSS 入池、评分与触发不变。共享发布实现的代码白名单只有三条：

- `feeds/sopilot.xml`
- `feeds/public-mirror/prompthub-blog.xml`
- `feeds/public-mirror/reddit-indiehackers-ai-revenue.xml`

当前 SoPilot workflow 只发布第一条；发布模块供两个已通过镜像源复用，不每源造一套状态机。workflow 自身只需 `contents: read` 与 `actions: read`，后者用于原 run/artifact 核查与恢复；跨仓写由专用 runtime writer 单独承担。

`GITHUB_TOKEN` 的权限限于 workflow 所在仓库，给本仓 `contents: write` 不能写另一个 private runtime 仓。[GitHub 官方说明](https://docs.github.com/en/actions/concepts/security/github_token)

runtime 当前没有 workflow、历史 run 数为 0。把生产器搬进 private runtime 新建 workflow，既不属于“同一已有执行器”，也会改标准 hosted runner 的计费路线：public 标准 runner compute 免费，private 使用套餐包含的 compute，超出后计费。本包保留现有 public 执行位置，不换账号/路线，不新建服务或租机；这不等于保证 artifacts/storage 没有独立费用。[GitHub Actions 计费说明](https://docs.github.com/en/billing/concepts/product-billing/github-actions)

因此，当前选定链路实际需要跨仓 writer；不为省权限改频率、窗口、单一输入真源或持久状态。

## 已有凭据可见元数据复核

观察时间：`2026-11-01T18:22:05Z`。本地审计证据位于本件 temp 的 `credential-audit.json` 及相邻原始 metadata JSON；审计只看名称/权限，从未读取或复制值。

- `rss-ingest-action` repository secrets：14 个；`rss-runtime-data`：0 个；`hotvideo-cloud-runner`：6 个，合计 20 个名称。
- 三仓 environments 均为 0；当前账号可见 organizations 为 0；三仓默认 workflow token policy 均 `read`。
- runtime 两个现有 deploy keys：`157322899 / rss-ingest-action-read`、`157354772 / rss-ingest-scheduler-read`，均 `read_only: true`。
- 现有名称包括 RSS checkout 使用的 `RSS_DATA_DEPLOY_KEY`；其余已列名称未识别出配置可用的 runtime writer。不能从名称识别加密值，更不能仅靠两个只读 key 断言不存在 PAT。
- App repository installation / user installation inventories 分别返回 HTTP 401 / 403，当前既有 gh 登录 token 形态不足以枚举这些接口。该不可见范围没有通过新授权补查。

准确结论是：**当前可见的已配置云端 metadata 中，未找到可复用的 runtime writer**；不证明其他位置绝不存在 PAT/App。若主会话随后查到符合权限和认证边界的既有云 writer，先核目标仓权限与部署入口，复用后本节新增权限动作不再需要；不能暗中换账号、计费路线或搬本机 gh 登录 token。

## 唯一待用户批准的动作（现在不执行）

为现有 private `acaiaishizhan/rss-runtime-data` 增加一个**专用、可写的 CI deploy key**，title 为 `rss-cloud-producers-write`；其私钥只经正常私密工具/配置写入现有 public `acaiaishizhan/rss-ingest-action` 的 Actions secret `RSS_RUNTIME_WRITER`，用途仅为上述共享云发布模块向 runtime 普通 push。保留旧 `RSS_DATA_DEPLOY_KEY` 和两枚旧 read keys 原样。

GitHub 给该 deploy key 的权限是**整个 runtime 仓库可写**，不是 GitHub 路径级权限；三 feed 路径边界由本包发布代码白名单、changed-path/readback 检查执行，不能把它宣传成平台路径级授权。凭据不进入 Dot、聊天、命令值、仓库、artifact 或日志。本包没有生成/创建命令，也没有生成或读取任何私钥值。

回退只撤销本次新 key 和 `RSS_RUNTIME_WRITER` secret，旧 key/secret 不动。若尚未切生产，本地 owner 不变、云 gate 继续关闭；若已切过，先核原 run/push 终态并按 owner 文档携**最新** runtime 和来源游标回切，不清状态、不还原旧备份。撤新凭据不能代替 owner 回切。

批准这一项仅授权创建上述 key 并配置对应 CI secret。部署、共享 owner/健康 diff 应用、停本机 task、启云 gate 均仍待主会话收齐各路包统一审核和必要授权，不能把凭据批准解释成生产切换许可。

## Owner 与健康交接范围

`owner/` 内两个 patch 二选一：SoPilot-only 或三源 combined；保留所有映射及另 8 个本机公开镜像，Grok 和旧停用路径不动。`OWNER-HANDOFF.md` 给出具体窗口、禁下次触发且活 run 等终态、混合 pending SHA/ancestry/changed-path/blob/原 dispatch 核查和回切顺序。两个 patch 已在实际有未提交改动的 publisher 基线上 `git apply --check`，未应用。

生产完整 baseline 与 modified 副本只保存在本轮 temp `sopilot-production/owner-diff/`，未复制到此分支；此处只带局部 patch 与指纹，避免夹带共享 publisher 的他人改动。真实 pending/task/runtime HEAD 未核，不声称 pending 为 0。

健康与恢复入口的最小兼容 diff 必须同窗交接：本机 task 已迁云而 Disabled 属正常，健康取真实 cloud capture+publish 回执，禁止 watchdog/producer-health/工作台恢复旧 task。相关 diff 由主会话合包，本文件不以放宽阈值或手写 recovered 代替真实证据。

上线前还须核清原 pending 与 dispatch、首次正式只改目标路径的 readback、非目标不变、最新游标续接、云 owner 健康与回切。临时 bare 远端验证只能证明发布语义；当前没有 cloud 正式 push 结果，不把本地验证或已通过 shadow 冒称生产迁移完成。
