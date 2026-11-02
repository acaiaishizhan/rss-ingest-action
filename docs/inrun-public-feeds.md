# 本轮 runner 里的公开 feed

`INRUN_FEEDS` 仓库变量默认空，全关。只接受逗号分隔的 `prompthub-blog`、`reddit-indiehackers-ai-revenue`、`sopilot`；可逐源启用或移除。仅普通 RSS lane、非 preflight、runtime checkout 成功且本地 source-map 存在时刷新。

新步骤在 checkout 和安装依赖后、入池前，直接复用影子冻结的抓取/解析/验证函数，仅原子替换本轮 checkout 对应的三个文件。无 runtime push、新凭据、新 artifact 或跨轮生产状态。单源失败留该源 checkout 旧文件，继续其他源和入池；日志只有公开源名、状态、条数、异常类型。

三源用线程并行抓，整步共用 `INRUN_DEADLINE_SECONDS`，默认 60 秒；可用同名环境变量调整。模块先由主线程加载，worker 只生成候选，主线程在截止前用临时文件和 `os.replace` 写入。到期未完源记 `timeout`，保留 checkout 原文件；全部摘要 `flush` 后 CLI 立即 `os._exit(0)`，不等待迟到线程。workflow 本步骤另设 `timeout-minutes: 2`，保留 `continue-on-error: true`（`tools/inrun_public_feeds.py:18,44–48,59–115,123–130`；`.github/workflows/rss-ingest.yml:99–100`）。

开工时仅清理三个固定 target 的两个父目录里的 `.inrun-*` 残留；空开关仍零行为（`tools/inrun_public_feeds.py:37–58`）。入池把 source-map 对应目标改成本地明确文件路径，再 `open` 该文件，不扫描目录（`source_runtime.py:69–84,123–134`；`rss_parser.py:243–251`），所以残留临时文件不会被误读。

SoPilot 用本步骤启动时的 UTC 分钟 `<15` 判断；与 Worker 相同的分钟判据，但不是 Worker 的 scheduledTime。排队跨过 15 分钟边界可能错过本小时，等下一自然小时。其余轮与失败轮用 checkout 内容，并非上一云轮内容；本轮刷新不会持久化回 runtime。SoPilot 完整采集成功但相关性预滤为零时允许更新为空 feed。

交接需另获批准，逐源执行：本机停该源 → 变量加入该源 → 等自然运行观察源摘要和入池结果。旧 runtime 文件保留，作为未刷新/失败时回退基线；本机已经推送的内容不用删除，不清下游游标。停 publisher 某源前先核清其旧 pending，避免恢复逻辑继续发布旧提交。SoPilot 还需停小时采集，再停 publisher 的该源。

回退逐源反向操作：变量移除该源 → 等在途运行结束 → 本机 fresh 抓取成功后恢复该源。SoPilot 同时恢复小时采集。其他源不动，不清状态、不恢复停用任务。

本变更没有修改既有 ingest 的日志及 artifact；其历史上传范围仍存在，不能据此宣称整个公开 workflow 已完成隐私审计。新步骤不读取其他 feed 内容，不输出 source-map 或私有 runtime 内容，也不新增上传。

离线验证：`python out/run_offline_tests.py` 禁用所有 socket 连接，共 725 passed（本入口 21 项）；`python out/replay_inrun_freeze.py` 再次通过三源冻结对比。新增两条真实 CLI 子进程测试：deadline 1 秒、一个 reader 睡 5 秒时约 1.1 秒退出，其他两源正常写入，5.2 秒后迟到源仍是旧文件；三源全部失败仍退出 0、原文件不变（`tests/test_inrun_public_feeds.py:175,210`）。
