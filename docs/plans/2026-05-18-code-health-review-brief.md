# Code Health Review Brief

Context date: 2026-05-18

This project has just finished a large keyword-maintenance stabilization round:

- RSS ingest now writes NEWS / FILTERED / KEYWORD with keyword records.
- KEYWORD maintenance first archives NEWS / FILTERED rows with `30d = 0` into quarterly tables, then runs stale cleanup, LLM noise audit, alias discovery, alias metadata writes, NEWS/FILTERED relinking, parent/owner rollup, expanded links, core field sync, audit, and snapshot refresh.
- RSS fetch stability now includes total concurrency, per-host concurrency, and timeout retry.
- Daily keyword maintenance and 10-minute RSS ingest can overlap; the risky stale cleanup case is protected by a 48-hour first-seen grace period.
- KEYWORD reverse-link formulas are approximate for high-frequency terms because Feishu only exposes about 500 visible reverse-linked records; the current quarterly archive reduces pressure but does not replace a script-backed trend system.

Current judgment:

- The codebase is not yet a "mess", but complexity is rising.
- `rss_ingest.py` is the main long-term risk because it owns too many concerns: RSS fetching, article extraction fallback, LLM dispatch, validation, dedup, Feishu writes, keyword creation/linking, secondary sync, and source-state updates.
- Keyword maintenance is better separated into scripts, but it now depends on many scripts, JSON artifacts, and Feishu table contracts. This is operationally workable but has a high onboarding cost.
- Future work should avoid pushing trend, heat, clustering, or more keyword maintenance logic into `rss_ingest.py`.

Desired review posture:

- Do not refactor for aesthetics.
- Do not change code during the review.
- Prioritize high-ROI fixes: places where a small structural change would reduce real operational risk, repeated bugs, long runtime, unclear ownership, or hard-to-debug failure modes.
- Treat Feishu field names, GitHub Action behavior, local env safety, and real write paths as high-risk contracts.

Useful areas to inspect:

- `rss_ingest.py`: responsibility boundaries, keyword write path, LLM provider path, source fetch retry path, secondary sync path.
- `tools/run_keyword_alias_daily.py`: orchestration, dry-run vs apply semantics, step boundaries, failure behavior.
- `tools/cleanup_stale_keywords.py`: stale cleanup safety and 48h protection.
- `alias_discovery.py`, `merge_keywords.py`, `tools/apply_keyword_alias_links.py`: alias discovery/write/relink split.
- `tools/audit_keywords.py`, `tools/sync_keyword_expanded_links.py`, `tools/keyword_parent_rollup.py`: post-maintenance health guarantees.
- `config.py`, `rss-ingest-local.env.example`, `.github/workflows/keyword-alias-daily.yml`: config drift and production defaults.
