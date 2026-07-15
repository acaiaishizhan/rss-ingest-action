# LLM Parallel Queue Design

Status: historical/completed. Current code uses the queued LLM processing model described here, including configurable concurrency and progress output.

**Goal:** 将 RSS 抓取流程改为“先全量拉取、后统一处理”，并引入 LLM 并行处理与进度条展示。

**Architecture:** 流程拆为两阶段：阶段一用 `RSS_FETCH_CONCURRENCY` 并发抓取源，再按源表原始顺序做硬去重、入队；阶段二用线程池并行跑 LLM，完成语义去重、写表与统计收集。通过锁保护共享结构，保证并发安全。

**Tech Stack:** Python 3.10+，`concurrent.futures.ThreadPoolExecutor`，标准库 `threading`。

## 1) 数据流
1. 读取 RSS 表（一次性）。
2. 按 `RSS_FETCH_CONCURRENCY` 并发抓取每个源的 feed。
3. 对每条 entry 做硬去重/时间窗过滤/内容判空。
4. 失败重试条目与新条目一起加入队列。
5. 阶段二并行处理队列：LLM →（可选）Vectorize → 写飞书 → 更新统计与失败池。

## 2) 并发策略
- 使用 `ThreadPoolExecutor(max_workers=LLM_CONCURRENCY)`，默认 4。
- 每个任务处理一篇文章的完整流水线。
- 对共享结构加锁：
  - `existing_keys`
  - `secondary_pending_items`
  - 统计计数
- LLM 重试沿用现有逻辑，不额外叠加并发重试。

## 3) 进度条与统计
- 使用纯文本进度条：`处理进度 done/total [####......] ok=.. fail=..`
- 单行刷新（`\r`），不支持时退化为普通日志。
- 输出全局 summary：来源数、抓取数、处理数、成功/失败等。

## 4) 配置项
新增 env：
- `RSS_FETCH_CONCURRENCY`（默认 20）
- `LLM_CONCURRENCY`（默认 4）
- `PROGRESS_BAR_WIDTH`（默认 20）

## 5) 错误处理
- 失败条目继续记录至 failed_items。
- 语义去重仍在 LLM 成功后执行。
- 写入失败不影响其他条目处理。

## 6) 影响范围
- `rss_ingest.py`：重构为“队列 + 并发处理”。
- `config.py`：新增并发/进度条配置。
- `README.md`：增加并发/进度说明。
