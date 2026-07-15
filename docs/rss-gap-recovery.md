# RSS 断层恢复 Runbook

本文记录资讯表出现时间断层时的恢复步骤。典型场景是主流程抓源和 LLM 正常运行，但写飞书失败，导致 RSS 源表游标前进而 NEWS / FILTERED 没有对应记录。

## 判断断层

先确认是写入断层，不是源完全没跑：

1. 看 NEWS 最新 `创建时间` 和 `发布时间`。
2. 看 FILTERED 最新记录。
3. 看 RSS 源表 `last_fetch_time` 是否还在前进。
4. 本地跑一次 `.\.venv\Scripts\python.exe rss_ingest.py`，关注 `[Summary]` 里的 `feishu_failed`、`written`、`filtered_logged`。

如果 RSS 源表仍在更新，但 NEWS / FILTERED 不写，优先排查飞书字段格式或字段缺失。

## 会挡住重抓的字段

`rss_ingest.py` 主要用下面两个字段决定是否跳过旧 entry：

- `last_fetch_time`
- `last_item_pub_time`

补跑时通常需要把这两个字段一起回拨到断层开始时间。`last_item_guid` 当前不参与过滤判断，但建议清空，避免后续维护误判。

不要清空 `failed_items`，它是失败重试池，清掉会丢失补救线索。

## 恢复步骤

以 2026-05-10 11:31:00 +08:00 为例：

1. 先备份 RSS 源表当前游标到本地 `.tmp/`，包括 `record_id`、`name`、`last_fetch_time`、`last_item_pub_time`、`last_item_guid`、`failed_items`。
2. 只更新启用源，把以下字段改到断层开始时间：
   - `last_fetch_time = 2026-05-10 11:31:00 +08:00`
   - `last_item_pub_time = 2026-05-10 11:31:00 +08:00`
   - `last_item_guid = ""`
   - `consecutive_fail_count = 0`
3. 运行：

   ```powershell
   .\.venv\Scripts\python.exe rss_ingest.py
   ```

4. 检查 `[Summary]`：
   - `queue_total` 应大于 0
   - `feishu_failed` 应为 0
   - `written` / `filtered_logged` 应有补写量
5. 抽查 NEWS / FILTERED 最新记录，确认 `发布时间` 覆盖断层区间。

## 2026-05-11 恢复记录

2026-05-11 已执行一次断层恢复：

- 断层起点：2026-05-10 11:31:00 +08:00
- 回拨启用 RSS 源：135 个
- 本地备份：`.tmp/rss-cursors-before-gap-reset-20260511122422.json`
- 补跑结果：`queue_total=137`，`written=93`，`filtered_logged=43`，`feishu_failed=0`，`sync_ok=93`

本次断层根因是 `关键词记录` 关联字段写入格式错误。飞书 Bitable v1 API 要求关联字段值为 record_id 字符串数组，例如：

```json
["recxxx"]
```

不要写成：

```json
[{"id": "recxxx"}]
```
