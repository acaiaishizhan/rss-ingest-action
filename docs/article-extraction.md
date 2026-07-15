# 文章全文抽取说明

本文记录 `rss-ingest-local` 当前的正文来源策略，方便后续维护时判断“为什么某些源能拿到网页全文，某些源不能”。

## 目标

RSS feed 经常只给标题、链接或短摘要。主流程需要尽量给 LLM 传入有信息量的正文，但不能对所有来源无脑抓网页，否则会拖慢运行并触发源站风控。

当前策略是：默认信任 RSS，只在正文为空或过短时抓原文；已验证来源走定向 parser，其他公开文章页走保守通用 parser。网页抓取只允许公网 HTTP(S)，每次重定向都会重新做 DNS/IP 校验，并限制响应大小，避免内网探测和异常大页面拖垮任务。

## 当前覆盖范围

已覆盖：

- `Hugging Face Blog`
  - feed：`https://huggingface.co/blog/feed.xml`
  - 问题：feed entry 有 title / link / published / id，但没有 `content` 和 `summary`。
  - 处理：当正文为空或过短时，请求原文页面，抽取 `blog-content` 容器里的正文。
- `IT之家：AI 标签`
  - feed/source：`https://www.ithome.com/tags/AI/`
  - 问题：标签页不是标准 RSS，当前 `rss_parser.py` 会把标签页解析成 entries，但 entry 文本基本只是标题。
  - 处理：当正文为空或过短时，请求文章页，抽取 `#paragraph` / `.post_content` 容器里的正文，并跳过广告声明等噪音块。
- `Hacker News`
  - feed：`https://hnrss.org/frontpage`
  - 问题：普通 HN 外链条目的 RSS 文本通常只是 `Article URL / Comments URL / Points`，不是文章正文。
  - 处理：当识别到这种 HN 链接卡片时，请求 `entry.link` 对应的外部文章页，用轻量 HTML 文本抽取补全文；HN 自帖已有正文时不二跳。
- `Towards AI / Medium`
  - feed：`https://pub.towardsai.net/feed`
  - 问题：Medium feed 经常只给 100-200 字摘要，文本里带 `Continue reading on Towards AI`；这类摘要可能超过 120 字，会被普通 RSS 阈值误判为可用正文。
  - 处理：当 `ENABLE_BROWSER_ARTICLE_FETCH=true` 且检测到 Medium/Towards AI 预览摘要或短正文时，连接 gpt-browser 默认 Chrome profile 的 DevTools endpoint，复用已登录页面抽取 `article` 正文。没有打开开关或浏览器补抓失败时，保留 RSS 原文本，不中断主流程。
- `AI HOT` 中的 X/Twitter 原帖
  - feed：`https://aihot.virxact.com/feed/all.xml` / `https://aihot.virxact.com/feed`
  - 问题：AI HOT 条目可能只给短摘要，链接指向 `x.com/.../status/...` 原帖。
  - 处理：强制补抓时先请求 X 页面内嵌数据，再请求 `publish.twitter.com/oembed`。默认不打开本机浏览器；只有显式设置 `ENABLE_X_BROWSER_FALLBACK=true` 时，才会连接 gpt-browser Chrome profile 做最后兜底。

不覆盖：

- Databricks / KDnuggets / TechCrunch 等：暂未接入专用网页抽取，后续需要单独评估。
- RSSHub Twitter/X 短文本源：默认按短文本处理，不主动抓外链全文。

通用 fallback：

- 非排除域名、非 feed/静态资源后缀的公开 HTTP(S) 文章链接，在 RSS 正文不足 120 字时可走 `generic_article`。
- `localhost`、私网、回环、链路本地、保留地址以及 DNS 解析到这些地址的域名都会被拒绝；跳转目标逐跳复检。
- 正文响应默认最多读取 `ARTICLE_FETCH_MAX_BYTES=2097152` 字节，超过即中止；非文本响应不会进入解析器。

## 主流程位置

入口在 `rss_ingest.split_sources_and_queue()` 构造 `article` 前：

1. `fetch_feed()` 拉 feed。
2. `build_item_key()` 生成去重 key。
3. `extract_article_text(url, source_name, feed_url, entry)` 生成正文。
4. `article["content"]` 使用 extractor 返回的 `text`。
5. 后续 LLM、去重、写飞书逻辑不变。

`rss_parser.entry_text_content()` 仍只负责 RSS entry 内文本提取，不做网页请求。

## 抽取规则

`article_extractor.extract_article_text()` 的基本规则：

1. RSS `content` 清洗后长度大于等于 120 字：直接使用，`method=rss_content`。
2. 没有 `content` 但有 `summary/description`：先使用摘要，`method=rss_summary`。
3. 文本为空或短于 120 字时，重点来源命中特定 parser；其他合格的公开文章链接使用通用 parser 请求原文页补全文。
4. Towards AI / Medium 摘要含 `Continue reading on ...` 时，即使超过 120 字，也可在浏览器补抓开关开启后请求已登录浏览器补全文。
5. AI HOT 中的 X/Twitter 原帖会在 `force_fetch=true` 时走 X 专用补抓；默认只用 HTTP，保留浏览器兜底开关但不开。
6. 网页抽取成功：返回 `method=source_parser:huggingface_blog`、`source_parser:ithome_article`、`source_parser:hacker_news_article`、`source_parser:generic_article`、`source_parser:x_status_browser` 或 `source_parser:browser_medium_article`。
7. 网页抽取失败：保留 RSS 原文本，不中断主流程，`status=fetch_error/parse_error`。

网页抽取 timeout 在主流程中限制为最多 12 秒，响应大小由 `ARTICLE_FETCH_MAX_BYTES` 控制。

## 回填记录

2026-05-10 已对 NEWS 表中当前扫描到的缺全文记录做过一次回填：

- Hugging Face Blog：3 条，全部成功，补完后最短正文 10540 字。
- IT之家：AI 标签：14 条，全部成功，补完后最短正文 511 字。

本次只回填 NEWS/资讯表的 `全文` 字段，没有处理 FILTERED 表，也没有新增飞书字段。

## 维护注意

- 新增来源 parser 前，先用样本确认 feed 是否真的不给全文，以及页面正文容器是否稳定。
- 不要默认打开所有短摘要来源的网页；这是性能和风控风险最大的地方。
- 不要把网页请求放进 `rss_parser.py`，否则 RSS 解析层会变慢且难测。
- gpt-browser 补抓依赖本机 Chrome 已启动且存在 endpoint 文件；本地调度任务启用前先运行 `gpt-browser launch` 并确认 Medium/Towards AI 页面可读。
- X/Twitter 浏览器兜底会影响桌面体验，默认关闭。需要临时追求更高召回时再打开 `ENABLE_X_BROWSER_FALLBACK=true`。
