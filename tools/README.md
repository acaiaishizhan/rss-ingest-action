# Tools

这些脚本不是 RSS 主流程入口。主流程仍然是项目根目录的 `rss_ingest.py`。

运行工具前先确认两件事：

1. 是否会写飞书。
2. 是否应该先用 `--dry-run`。

## 常用工具

- `run_keyword_alias_daily.py`：每天 04:00 的关键词维护流水线。先归档 NEWS / FILTERED 中 `30d=0` 的旧资讯，再删 `30d=0`、非 manual、且首次出现超过保护期的 KEYWORD，然后做归一、改链、父级 / 归属补链、核心字段同步和巡检，真实写入成功后更新快照。
- `run_keyword_alias_daily_local.ps1` / `register_keyword_alias_daily_task.ps1`：本机 Windows 任务计划入口，用火山 Ark `deepseek-v4-pro`（subagent `dsp` 同款 Pro lane）跑同一套关键词维护流水线。
- `run_keyword_audit_repair.py`：专项修复 KEYWORD 重复和 zero-link 老词。流程是 audit -> duplicate audit -> duplicate relink/merged note -> stale zero-link cleanup -> parent rollup -> audit；默认 dry-run，传 `--apply` 才写飞书。本机入口是 `run_keyword_audit_repair_local.ps1`，计划任务注册脚本是 `register_keyword_audit_repair_task.ps1`。
- `archive_old_records.py`：把 NEWS / FILTERED 中 `30d=0` 的旧资讯归档到季度表（如 `2026Q2` / `2026Q2回收站`）。默认 dry-run；真实 `--apply` 时先写归档表再删主表，归档字段不带 `关键词记录`。
- `cleanup_stale_keywords.py`：清理 KEYWORD 表里 `30d=0`、非 manual、且首次出现超过保护期的关键词；没有 `30d` 字段时不删。
- `apply_keyword_alias_links.py`：只处理 NEWS / FILTERED 的关键词记录，把别名记录改链到规范词；支持 `--recent-hours` 限制最近记录。
- `keyword_parent_rollup.py`：维护 KEYWORD 的 `父关键词`、`归属关键词` 和自身 / 总热度公式。
- `sync_keyword_expanded_links.py`：把 NEWS / FILTERED 的 `关键词记录` 补成原始关键词 + 父关键词 + 归属关键词。
- `keyword_snapshot.py`：KEYWORD 快照读写工具，被日常归一 workflow 使用；schema v2 会写入父关键词 / 归属关键词链接，供 RSS 主流程快速建索引。
- `backfill_keywords.py`：给历史文章补关键词记录。
- `dedup_prompt_eval.py`：本地测试去重提示词，不写飞书。
- `export_feishu_recent.py`：导出最近新闻，默认写到 `out/feishu_news_last_12h.txt`。
- `rss_field_cleanup.py`：清理 RSS 源表旧字段，会写飞书，谨慎运行。

## Legacy

`tools/legacy/` 里是旧的一次性脚本。默认不要跑，除非你已经确认当前主流程没有同等替代能力。
