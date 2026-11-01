# SoPilot production takeover package — not activated

This branch prepares capture → immutable private-runtime publication. It does not
change local owner/tasks, RSS ingest, mappings, scoring, cursors, or shadow schedules.
Do not activate it until the coordinated owner handoff and permission approval.

`sopilot_producer.py` imports the existing byte-frozen collector from cloud-shadow,
checks SHA-256 `1c5577a96731a3539285e98e6e689d8f10ee39b7edbf409eff4ddbd8ae123b66`,
and calls its `_fetch_text`, `fetch_sopilot`, `parse_rank_page`, and `render_rss`.
The AI/Creator six-hour window, filter, fields, GUIDs, and snapshot replacement
semantics are unchanged. A failed/incomplete fetch has no publishable candidate.
An empty SoPilot candidate requires successful complete pagination for both categories
and closed discovered/prefiltered/retained counts. Zero entries is not a parse failure.

`runtime_publish.py` shares one publication mechanism for exactly these paths:

- `feeds/sopilot.xml`
- `feeds/public-mirror/prompthub-blog.xml`
- `feeds/public-mirror/reddit-indiehackers-ai-revenue.xml`

This is a code allowlist. GitHub writer credentials remain repository scoped; GitHub
does not provide deploy-key path permissions. Mirror capture integration remains a
separate owner's change; empty-mirror publication is conservatively rejected here.
The XML validation and item-content fingerprint follow the existing local publisher's
`validate_feed_bytes` / `feed_fingerprint`; no local publisher/config/env is imported.
Feed-level metadata-only changes do not create new commits. This publisher creates
no ingest dispatch and has no Feishu client or credential.

## Authentication and activation boundary

The prepared workflow uses the existing public executor `rss-ingest-action`, not a
new private Actions executor. Runtime target is the existing private
`acaiaishizhan/rss-runtime-data`, `main`. `RSS_RUNTIME_WRITER` is an uncreated SSH
writer-secret placeholder, consumed only by `actions/checkout` SSH configuration.
It is distinct from existing read keys. No key value is read, printed or copied by
these scripts. A `contents: read` code-repo GITHUB_TOKEN cannot write across repos.
`actions: read` permits receipt/pending artifact recovery. No Feishu secret is used.

The sole activation gate is default `main` plus `SOPILOT_PRODUCER_OWNER=cloud`; the
new workflow has no branch-push trigger. On this preparation branch it cannot run.
Cron is hourly minute 02, matching the registered local task's intended cadence.
GitHub schedules are best effort and may delay/skip: this is not a guaranteed hourly
execution SLA. Owner gate and new credential must not be enabled before handoff.

## Durable push and recovery

`prepare` reads/fetches actual remote main, validates capture, constructs one ordinary
single-target commit in an independent index, and never pushes. The runtime worktree
and every other path/mode stay untouched. `publish` is dry-run unless `--allow-write`.

Before any push, workflow uploads a pending artifact containing only `candidate.xml`,
`pending.json`, and `pending.commit`. The last is a raw Git commit object containing
hashes, fixed author/message and dates. No bundle, private tree object, other feed
blob, history, checkout, `.git`, source-map or SSH config is uploaded. A fresh
authenticated checkout reconstructs the tree from the recorded base and public
target payload, verifies tree/parent/target, then restores the **same** original
commit SHA. The original pending identity survives runner loss.

Before each push/retry, `ls-remote` and fetch establish current remote SHA. Ancestry
and target tree entry/blob readback prove whether the original pending landed. A
successful push followed by a client timeout is reconciled, without another commit.
Unreachable remote means blocked, never permission to blindly push. Same-target
changes block. Other-target advances preserve all external tree entries and produce
a new pending commit on the latest base; status `reprepare_required` exits before
push so the updated pending is durably uploaded first. Resume that reconciled
artifact explicitly, without capture. Ordinary nonforce push is the only write.

Fresh scheduled capture first checks the previous workflow receipt. Missing receipt,
nonterminal run, unconfirmed push, or unresolved pending blocks capture. A capture
that provably failed before publication does not block a future attempt. A blocked
follow-up is traced back to the original unresolved run. Standard Actions rerun is
rejected: explicitly dispatch `resume_run_id` and exact `resume_artifact_name` from
the original run. Inspect its run/ref/artifact before resuming; do not resubmit capture.
Artifact retention is seven days. Expired/missing evidence requires manual examination
of actual runtime history and confirmed operation identifiers, not clearing state.
Before owner activation, scheduled runs may have no receipt because the `produce`
job was skipped. The barrier ignores one only when actual jobs metadata proves a
completed/skipped `produce` job with no steps; workflow success alone is insufficient.

The postrun artifact `sopilot-production-receipt-<runid>-<attempt>` contains
`receipt.json`. Only actual remote SHA/blob readback sets
`publish.target_blob_verified=true` with status `confirmed` or `unchanged`. That
verification is bound to this run/attempt; a downloaded previous receipt is removed
from the current copy and cannot establish current health. Capture timestamps,
page success/counts, source/input fingerprints and failures are retained. Production
health must consume this receipt, not an Action green check or stale local XML.

## Offline verification

```sh
RSS_INGEST_SKIP_LOCAL_ENV=true PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tools/cloud-production/tests -v
```

Tests create real temporary bare Git remotes under ignored `out/`, exercising normal
push, fresh-checkout exact-commit recovery, concurrent different/same targets,
mode preservation/rejection, push-success/client-timeout reconciliation, unreachable
remote, allowlist/path traversal, valid zero and failure distinctions, and receipt
barrier behavior. These are local Git semantic tests, **not** production cloud writes.

## Review and coordinated handoff

- [Implementation decision and the single proposed permission action](handoff/DECISION-AND-PERMISSION.md)
- [Owner transfer and pending-operation checklist](handoff/owner/OWNER-HANDOFF.md)
- [Health and recovery patch, activation, and rollback](handoff/health/README.md)
- [Validation and remaining acceptance checks](handoff/VALIDATION.md)

The owner/health patches are review artifacts only. They target the current local
producer/monorepo baselines recorded alongside them and have not been applied.
