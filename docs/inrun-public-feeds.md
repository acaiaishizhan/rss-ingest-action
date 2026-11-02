# 本轮 runner 里的公开 feed

`INRUN_FEEDS` 仓库变量默认空，全关。只接受逗号分隔的 `prompthub-blog`、`reddit-indiehackers-ai-revenue`、`sopilot`；可逐源启用或移除。仅普通 RSS lane、非 preflight、runtime checkout 成功且本地 source-map 存在时刷新。

新步骤在 checkout 和安装依赖后、入池前，直接复用影子冻结的抓取/解析/验证函数，仅原子替换本轮 checkout 对应的三个文件。无 runtime push、新凭据、新 artifact 或跨轮生产状态。单源失败留该源 checkout 旧文件，继续其他源和入池；日志只有公开源名、状态、条数、异常类型。

SoPilot 用本步骤启动时的 UTC 分钟 `<15` 判断；与 Worker 相同的分钟判据，但不是 Worker 的 scheduledTime。排队跨过 15 分钟边界可能错过本小时，等下一自然小时。其余轮与失败轮用 checkout 内容，并非上一云轮内容；本轮刷新不会持久化回 runtime。SoPilot 完整采集成功但相关性预滤为零时允许更新为空 feed。

交接需另获批准，逐源执行：本机停该源 → 变量加入该源 → 等自然运行观察源摘要和入池结果。旧 runtime 文件保留，作为未刷新/失败时回退基线；本机已经推送的内容不用删除，不清下游游标。停 publisher 某源前先核清其旧 pending，避免恢复逻辑继续发布旧提交。SoPilot 还需停小时采集，再停 publisher 的该源。

回退逐源反向操作：变量移除该源 → 等在途运行结束 → 本机 fresh 抓取成功后恢复该源。SoPilot 同时恢复小时采集。其他源不动，不清状态、不恢复停用任务。

本变更没有修改既有 ingest 的日志及 artifact；其历史上传范围仍存在，不能据此宣称整个公开 workflow 已完成隐私审计。新步骤不读取其他 feed 内容，不输出 source-map 或私有 runtime 内容，也不新增上传。
