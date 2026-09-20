# RSS 生产约定

- 修改前阅读 `docs/rss-screening-baseline.md`。2026-09-20 用户锁定了原 RSS 提示词；仅保留已确认的最终内容入库阈值 6.0。未经用户明确要求不得改写筛选提示词、去留规则、评分含义或 QA 契约。
- 普通 RSS“独立增量判断 / value-first / 低增量”路线已否决，不得继续或 resume；禁止用代码、重试提示词或上下文注入间接恢复。
- 本机 publisher 定时主动触发为普通 RSS 主触发，GitHub schedule 只作 best-effort 兜底；不得恢复 `publisher --no-dispatch + GitHub 定时独占发车`。
- 不得输出或提交真实密钥与 env。测试设置 `RSS_INGEST_SKIP_LOCAL_ENV=true`。
