# 独立流水线与恢复

```text
┌ 普通RSS → rss-ingest.yml ────────┐
├ Grok feed → grok-ingest.yml ────┼→ NEWS → 独立Info终筛
└ SoPilot → sopilot-info.yml ─────┘
```

普通RSS受`RSS_INGEST_ENABLED`控制，Grok入表受`GROK_INGEST_ENABLED`控制；SoPilot由独立Windows小时任务定向dispatch。不恢复SoPilot作为Info前置条件。共享`rss_ingest.py`只复用实现，来源集合、并发组、健康文件和成败互不决定。

`RSS_INGEST_LANE=rss/grok`在处理前选择互斥来源。Grok feed路径为`grok/`或`grok-feeds/`下的XML。部署拆分时，先让使用旧代码的普通RSS运行结束，再开启Grok新schedule，避免旧普通入口仍包含Grok源。

生产workflow设置`RSS_TASK_ALERTS_ONLY=true`，逐条模型/网络错误只留日志，不额外写通知表。任务完成后记录一次健康结果，首次可恢复失败静默，成功清零，连续失败或明确需要人工动作才告警。每条lane使用独立、每run唯一的缓存key，避免同小时旧cache覆盖新健康结果。SoPilot由本地编排器承担唯一告警责任，云端保留receipt和日志。

新闻流水线告警由本地独立观察器统一发送，云端和Grok采集端只记录健康结果。每个生产者有独立失败计数和告警凭证；同一失败run重复观察不累计。通知发前落盘，未知结果不重发。

Grok网页消费者先保存topic到`GROK_BROWSER_RECEIPT_FILE`的持久指针；跨小时仍复用原回执和原prompt。完成则读缓存，提交不明只读原会话的唯一请求标识，不能开始新请求。明确终止的无效响应保留证据，下一正常周期才生成新查询。浏览器不可用时不启动或重启浏览器。

RSS和Grok快照发布分别使用独立checkout、状态和锁，保留不可变Git提交，以merge衔接互斥路径。发布成功后主动触发各自workflow；GitHub schedule只作补充，不能保证及时触发。发前保存唯一publish_id和提交版本，未知结果按run标题只读查回、不重发；新快照可建立新的请求，旧请求证据独立归档。

任何云端终止失败不证明写表全部回滚。`RSS_DURABLE_WRITES=true`在写表前把意图持久保存到来源的failed_items，并使用稳定client_token。未知结果只通过原目标表中唯一正证据确认，查不到不能重发。含未知写入的条目不受普通失败过期规则清理；其他条目可以继续。普通过期失败先归档证据再退休。SoPilot schema2回执绑定原run、attempt、head、workflow和来源快照；旧协议不明写入保持held，不能冒充安全重试。

测试只是部署门槛。最终验收需要至少三个实际小时的独立Info/SoPilot结果，以及真实消息凭证、NEWS回写、剩余数量和无重复写入证据。
