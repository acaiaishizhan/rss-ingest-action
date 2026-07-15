# html_watch 官方页面监控

`html_watch` 用来补 RSS 缺口：有些 T0 官方信息源没有 RSS，但页面上有稳定的 changelog / news 链接。它不是通用爬虫，只抓 RSS 源表里显式配置为 `type = html_watch` 的单个 URL。

## 飞书 RSS 源表字段

- `type`: 填 `html_watch`。普通 RSS 源保持原值或留空。
- `feed_url`: 要监控的官方页面 URL。
- `enabled`: 勾选后才会运行。
- `watch_state`: 文本字段，代码写入 JSON 状态，例如 `etag`、`last_modified`、`backoff_until`、`recent_keys`。

`watch_state` 只存抓取状态，不存正文。启用第一条 `html_watch` 源之前，需要先在 RSS 源表加这个文本字段。
如果字段暂时没加，代码会在写回失败时自动去掉 `watch_state` 后重试，保证主流程不断；代价是游标无法持久化，下一轮可能多做一次条件外抓取。

## 调度语义

- 默认间隔：`HTML_WATCH_FETCH_INTERVAL_MIN=10` 分钟。
- 每个源有一个很小的确定性 jitter，避免同一轮同时打到同一批官方站。
- 同 host 串行抓取，避免并发压同一个站。
- 遇到 `ETag` / `Last-Modified` 会带条件请求；`304` 视为成功但不入队。
- 遇到 `429` 使用 `Retry-After`；没有 `Retry-After` 时按指数退避。
- 遇到 `401/403` 标记为 blocked 方向的 HTTP 失败，并写入 `backoff_until`，不持续重试轰站。

## 当前解析范围

已加窄解析：

- DeepSeek 官方更新页：优先识别 changelog 结构，兜底只收 `/news/` 链接。
- Kimi / Moonshot changelog：按日期段和列表项生成更新条目。
- 智谱 BigModel releases：按 release label / description / link 生成条目。
- OpenAI API、Claude Platform、Gemini API、xAI API、Mistral：按官方日期段或版本段生成独立更新条目；Claude / xAI 优先使用官方 Markdown 小体积入口。
- 腾讯混元：按产品动态表格逐行生成条目。
- MiniMax Agent：按官方 Markdown 中的版本号生成条目。
- Artificial Analysis、METR：按文章 / 评测卡片生成条目。

新增官方源上线前必须实抓验证。只返回固定的 `Changelog` / `Release notes` 自链接，不代表监控有效；页面更新必须能产生新的条目 ID。对体积大且没有 `ETag` / `Last-Modified` 的页面，要优先寻找官方 Markdown、RSS 或更小的列表页，避免在 10 分钟轮询下造成无意义流量。

其他官方页面走保守通用解析，只收标题或链接明显包含 `release`、`update`、`发布`、`更新`、`公告`、`模型`、`qwen`、`doubao`、`hunyuan`、`ernie` 等信号的链接。后续如果某个国产模型源误报或漏报，优先在 `html_watch.py` 里加站点级窄解析，不把通用网页抓取塞回 `rss_parser.py`。

## 与主流程的关系

`html_watch` 产出的条目会被包装成和 RSS entry 相同的结构，继续走现有去重、正文抽取、LLM 筛选、写 NEWS / FILTERED 的流程。主流程仍以 `item_key` 去重；`watch_state.recent_keys` 只是源状态快照，不替代 NEWS 表去重。
