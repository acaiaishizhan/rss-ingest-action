# -*- coding: utf-8 -*-
"""Delete stale KEYWORD records whose total 30d heat is zero."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
import rss_ingest
from feishu_client import batch_delete_bitable_records, get_tenant_access_token, list_bitable_records
from tools.keyword_parent_rollup import parse_int

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


HEAT_30D_FIELD = "30d"
MANUAL_FIELD_CANDIDATES = ("manual/auto", "manual", "来源", "创建方式", "创建来源", config.KEYWORD_FIELD_NOTE)


def is_manual_keyword(fields: Dict[str, Any]) -> bool:
    for field_name in MANUAL_FIELD_CANDIDATES:
        value = rss_ingest.clean_feishu_value(fields.get(field_name)).strip().lower()
        if value == "manual" or "[manual]" in value:
            return True
    return False


def _record_summary(record: Dict[str, Any]) -> Dict[str, Any]:
    fields = record.get("fields") or {}
    return {
        "record_id": str(record.get("record_id") or "").strip(),
        "name": rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_CANONICAL_NAME)).strip(),
        "type": rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_TYPE)).strip(),
        "30d": 0,
    }


def _has_zero_heat_and_can_auto_delete(record: Dict[str, Any]) -> bool:
    fields = record.get("fields") or {}
    if not str(record.get("record_id") or "").strip():
        return False
    if is_manual_keyword(fields):
        return False
    if HEAT_30D_FIELD not in fields:
        return False
    return parse_int(fields.get(HEAT_30D_FIELD)) == 0


def build_missing_first_seen_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    missing: List[Dict[str, Any]] = []
    for record in records:
        if not _has_zero_heat_and_can_auto_delete(record):
            continue
        fields = record.get("fields") or {}
        if rss_ingest.parse_ts_ms(fields.get(config.KEYWORD_FIELD_FIRST_SEEN)) <= 0:
            missing.append(_record_summary(record))
    return missing


def parse_record_ids(raw: str) -> set[str]:
    return {item.strip() for item in raw.split(",") if item.strip()}


def build_delete_candidates(
    records: List[Dict[str, Any]],
    min_age_hours: float = 48,
    exclude_record_ids: set[str] | None = None,
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    excluded = exclude_record_ids or set()
    min_first_seen_ms = int((time.time() - max(0, min_age_hours) * 3600) * 1000) if min_age_hours > 0 else 0
    for record in records:
        record_id = str(record.get("record_id") or "").strip()
        if record_id in excluded:
            continue
        fields = record.get("fields") or {}
        if not _has_zero_heat_and_can_auto_delete(record):
            continue
        first_seen_ms = rss_ingest.parse_ts_ms(fields.get(config.KEYWORD_FIELD_FIRST_SEEN))
        if first_seen_ms <= 0:
            continue
        if min_age_hours > 0 and first_seen_ms > min_first_seen_ms:
            continue
        candidates.append(_record_summary(record))
    return candidates


def chunks(items: List[str], size: int) -> List[List[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def apply_deletes(tenant_token: str, record_ids: List[str], batch_size: int) -> Tuple[int, List[Dict[str, Any]]]:
    deleted = 0
    failed: List[Dict[str, Any]] = []
    for batch in chunks(record_ids, max(1, batch_size)):
        ok, data = batch_delete_bitable_records(
            config.FEISHU_APP_TOKEN,
            config.FEISHU_KEYWORD_TABLE_ID,
            tenant_token,
            batch,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
        )
        if ok:
            deleted += len(batch)
        else:
            failed.append({"record_ids": batch, "error": data})
    return deleted, failed


def run(
    dry_run: bool,
    output_path: Path,
    page_size: int,
    max_pages: int,
    batch_size: int,
    delete_limit: int,
    min_age_hours: float,
    exclude_record_ids: set[str] | None = None,
) -> Dict[str, Any]:
    tenant_token = get_tenant_access_token(
        config.FEISHU_APP_ID,
        config.FEISHU_APP_SECRET,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )
    records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_KEYWORD_TABLE_ID,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=page_size,
        max_pages=max(1, max_pages),
    )
    exclusions = exclude_record_ids or set()
    candidates = build_delete_candidates(records, min_age_hours=min_age_hours, exclude_record_ids=exclusions)
    missing_first_seen = build_missing_first_seen_records(records)
    if delete_limit > 0:
        candidates = candidates[:delete_limit]

    result: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "stale-keyword-cleanup-dry-run" if dry_run else "stale-keyword-cleanup-apply",
        "keyword_scanned": len(records),
        "delete_count": len(candidates),
        "deleted": 0,
        "failed": [],
        "missing_first_seen_count": len(missing_first_seen),
        "missing_first_seen_sample": missing_first_seen[:50],
        "rule": f"delete KEYWORD where 30d = 0 and manual != manual and first_seen older than {min_age_hours:g}h",
        "excluded_record_count": len(exclusions),
        "excluded_record_ids": sorted(exclusions)[:100],
        "sample": candidates[:50],
    }
    if not dry_run and candidates:
        deleted, failed = apply_deletes(tenant_token, [item["record_id"] for item in candidates], batch_size)
        result["deleted"] = deleted
        result["failed"] = failed

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delete stale KEYWORD records whose total 30d heat is zero.")
    parser.add_argument("--apply", action="store_true", help="Write changes. Without this flag, only dry-run.")
    parser.add_argument("--output", default="", help="Output JSON path.")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--delete-limit", type=int, default=0)
    parser.add_argument("--min-age-hours", type=float, default=48, help="Do not delete KEYWORD records first seen within this many hours.")
    parser.add_argument("--exclude-record-ids", default="", help="Comma-separated KEYWORD record IDs that must not be deleted.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    mode = "apply" if args.apply else "dry-run"
    output_path = Path(args.output) if args.output else Path("out") / f"stale-keyword-cleanup-{mode}-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
    result = run(
        dry_run=not args.apply,
        output_path=output_path,
        page_size=args.page_size,
        max_pages=args.max_pages,
        batch_size=args.batch_size,
        delete_limit=args.delete_limit,
        min_age_hours=args.min_age_hours,
        exclude_record_ids=parse_record_ids(args.exclude_record_ids),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
