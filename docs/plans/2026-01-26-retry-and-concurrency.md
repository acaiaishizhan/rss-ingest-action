# Retry/Failure-Pool Implementation Plan

Status: historical/completed. Current code keeps the `failed_items` retry pool, but provider details in this old plan are not current. NVIDIA NIM provider was removed after repeated model EOLs; do not copy NVIDIA-specific environment variables or examples from this file into current config. This file is retained as an implementation record, not an active plan.

> Historical note: the original implementation prompt referenced `superpowers:executing-plans`; that tooling is not required to read or maintain this project.

**Goal:** 实现失败重试池，并提升 API 重试次数默认值到 10。

**Architecture:** 在 RSS 源表中使用 `failed_items`（JSON 字符串）记录失败条目；每次处理时优先重试有限数量的失败条目。

**Tech Stack:** Python 3.x, requests, feedparser.

### Task 1: 配置项与字段定义

**Files:**
- Modify: `config.py`

**Step 1: 定义新环境变量与默认值**

```python
FAILED_ITEMS_MAX = int(os.getenv("FAILED_ITEMS_MAX", "50"))
FAILED_ITEMS_RETRY_LIMIT = int(os.getenv("FAILED_ITEMS_RETRY_LIMIT", "5"))
FAILED_ITEMS_MAX_AGE_DAYS = int(os.getenv("FAILED_ITEMS_MAX_AGE_DAYS", "7"))
FAILED_ITEMS_MAX_MISS = int(os.getenv("FAILED_ITEMS_MAX_MISS", "3"))
```

**Step 2: 统一提高 API 重试默认值**

```python
GEMINI_RETRIES = 10
IFLOW_RETRIES = int(os.getenv("IFLOW_RETRIES", "10"))
OPENAI_RETRIES = int(os.getenv("OPENAI_RETRIES", "10"))
DEEPSEEK_RETRIES = int(os.getenv("DEEPSEEK_RETRIES", "10"))
ZHIPU_RETRIES = int(os.getenv("ZHIPU_RETRIES", "10"))
```

**Step 3: 新增 RSS 失败池字段名**

```python
RSS_FIELD_FAILED_ITEMS = "failed_items"
```

**Step 4: 手工验证**

Run: `python - <<'PY'
import config
print(config.DEEPSEEK_RETRIES)
PY`
Expected: 输出默认值。

### Task 2: 失败重试池的读写与维护

**Files:**
- Modify: `rss_ingest.py`

**Step 1: 新增解析与序列化工具函数**

```python
def parse_failed_items(raw: Any) -> List[Dict[str, Any]]:
    # 允许 str/列表，返回规范化 list
```

```python
def serialize_failed_items(items: List[Dict[str, Any]]) -> str:
    return json.dumps(items, ensure_ascii=False)
```

**Step 2: 新增失败项更新逻辑**

```python
def upsert_failed_item(items, item_key, entry_ts, title, link, reason):
    # 更新 fail_count, last_error, last_seen_ms, miss_count
```

```python
def prune_failed_items(items, now_ms):
    # 限制数量、按时间淘汰、去重
```

**Step 3: 手工验证**

Run: `python - <<'PY'
from rss_ingest import parse_failed_items, serialize_failed_items
print(serialize_failed_items(parse_failed_items('[]')))
PY`
Expected: 输出 `[]`。

### Task 3: 将失败重试池接入处理流程

**Files:**
- Modify: `rss_ingest.py:process_source`

**Step 1: 读取 RSS 表中的失败池**

```python
failed_items = parse_failed_items(source.get("failed_items"))
```

**Step 2: 拉取 feed 后，构建 entry_map**

```python
entry_map = {build_item_key(e, ...): e for e in entries if build_item_key(...)}
```

**Step 3: 优先重试失败池**
- 只重试 `FAILED_ITEMS_RETRY_LIMIT` 条
- 如果条目仍在 `entry_map` 中，重新走 `analyze_with_llm`
  - 成功：写入新闻表并从失败池移除
  - 失败：更新 fail_count + last_error
- 如果条目不在 feed：增加 `miss_count`，超过阈值移除

**Step 4: 正常处理新条目**
- 逻辑保持不变

**Step 5: 写回失败池字段**

```python
update_fields[config.RSS_FIELD_FAILED_ITEMS] = serialize_failed_items(pruned_items)
```

**Step 6: 手工验证**

Run: `python - <<'PY'
from rss_ingest import parse_failed_items
print(parse_failed_items('[]'))
PY`
Expected: 输出 `[]`。
