# -*- coding: utf-8 -*-
"""Expand NEWS/FILTERED keyword links with KEYWORD parent and owner links."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
import rss_ingest
from feishu_client import (
    batch_update_bitable_records,
    get_tenant_access_token,
    list_bitable_records,
    update_bitable_record_fields,
)
from merge_keywords import parse_ts_ms

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def prefetch_keyword_records_by_id(tenant_token: str, page_size: int, max_pages: int) -> Dict[str, rss_ingest.KeywordRecord]:
    records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_KEYWORD_TABLE_ID,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=page_size,
        max_pages=max(1, max_pages),
    )
    out: Dict[str, rss_ingest.KeywordRecord] = {}
    for record in records:
        record_id = rss_ingest.clean_feishu_value(record.get("record_id")).strip()
        if not record_id:
            continue
        fields = record.get("fields") or {}
        note = rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_NOTE)).strip()
        if rss_ingest.is_merged_keyword_note(note):
            continue
        out[record_id] = rss_ingest.KeywordRecord(
            record_id=record_id,
            canonical_name=rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_CANONICAL_NAME)).strip(),
            type=rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_TYPE)).strip().lower(),
            parent_ids=rss_ingest._keyword_record_ids_from_cell(fields.get(config.KEYWORD_FIELD_PARENT)),
            owner_ids=rss_ingest._keyword_record_ids_from_cell(fields.get(config.KEYWORD_FIELD_OWNERS)),
        )
    return out


def build_expanded_link_updates(
    records: List[Dict[str, Any]],
    link_field: str,
    keyword_records_by_id: Dict[str, rss_ingest.KeywordRecord],
    update_limit: int = 0,
) -> List[Dict[str, Any]]:
    updates: List[Dict[str, Any]] = []
    index = {record_id: record for record_id, record in keyword_records_by_id.items()}
    for record in records:
        fields = record.get("fields") or {}
        old_ids = rss_ingest._keyword_record_ids_from_cell(fields.get(link_field))
        if not old_ids:
            continue
        expanded_ids = rss_ingest.expand_keyword_record_ids(old_ids, index)
        if expanded_ids == old_ids:
            continue
        updates.append({"record_id": record.get("record_id"), "fields": {link_field: expanded_ids}})
        if update_limit > 0 and len(updates) >= update_limit:
            break
    return updates


def load_table_records(
    table_id: str,
    tenant_token: str,
    page_size: int,
    max_pages: int,
    published_field: str,
    recent_hours: float,
) -> List[Dict[str, Any]]:
    records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        table_id,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=page_size,
        max_pages=max(1, max_pages),
        sort=[{"field_name": published_field, "desc": True}],
        allow_partial=recent_hours > 0,
    )
    if recent_hours <= 0:
        return records
    since_ms = int((time.time() - recent_hours * 3600) * 1000)
    return [
        record for record in records
        if (parse_ts_ms((record.get("fields") or {}).get(published_field)) or 0) >= since_ms
    ]


def chunks(items: List[Dict[str, Any]], size: int) -> List[List[Dict[str, Any]]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def apply_updates(
    table_id: str,
    tenant_token: str,
    updates: List[Dict[str, Any]],
    sleep_seconds: float,
    batch_size: int = 100,
) -> Tuple[int, List[Dict[str, Any]]]:
    updated = 0
    failed: List[Dict[str, Any]] = []
    for batch in chunks(updates, max(1, batch_size)):
        ok, data = batch_update_bitable_records(
            config.FEISHU_APP_TOKEN,
            table_id,
            tenant_token,
            batch,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
        )
        if ok:
            updated += len(batch)
            continue

        for item in batch:
            single_ok = update_bitable_record_fields(
                config.FEISHU_APP_TOKEN,
                table_id,
                tenant_token,
                item["record_id"],
                item["fields"],
                config.HTTP_TIMEOUT,
                config.HTTP_RETRIES,
            )
            if single_ok:
                updated += 1
            else:
                failed.append(
                    {
                        "record_ids": [item["record_id"]],
                        "error": "batch and single update returned false",
                        "batch_error": data,
                    }
                )
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return updated, failed


def run(
    dry_run: bool,
    output_path: Path,
    page_size: int,
    max_pages: int,
    record_max_pages: int,
    recent_hours: float,
    update_limit: int,
    link_update_sleep_seconds: float,
) -> Dict[str, Any]:
    tenant_token = get_tenant_access_token(
        config.FEISHU_APP_ID,
        config.FEISHU_APP_SECRET,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )
    keyword_records_by_id = prefetch_keyword_records_by_id(tenant_token, page_size, max_pages)
    effective_record_max_pages = record_max_pages if record_max_pages > 0 else max_pages
    news_records = load_table_records(
        config.FEISHU_NEWS_TABLE_ID,
        tenant_token,
        page_size,
        effective_record_max_pages,
        config.NEWS_FIELD_PUBLISHED_MS,
        recent_hours,
    )
    filtered_records = []
    if str(getattr(config, "FEISHU_FILTERED_TABLE_ID", "") or "").strip():
        filtered_records = load_table_records(
            config.FEISHU_FILTERED_TABLE_ID,
            tenant_token,
            page_size,
            effective_record_max_pages,
            config.FILTERED_FIELD_PUBLISHED_MS,
            recent_hours,
        )

    news_updates = build_expanded_link_updates(
        news_records,
        config.NEWS_FIELD_KEYWORD_RECORDS,
        keyword_records_by_id,
        update_limit=update_limit,
    )
    filtered_updates = build_expanded_link_updates(
        filtered_records,
        config.FILTERED_FIELD_KEYWORD_RECORDS,
        keyword_records_by_id,
        update_limit=update_limit,
    )
    result: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "expanded-link-apply" if not dry_run else "expanded-link-dry-run",
        "keyword_count": len(keyword_records_by_id),
        "news_scanned": len(news_records),
        "filtered_scanned": len(filtered_records),
        "recent_hours": recent_hours,
        "record_max_pages": effective_record_max_pages,
        "news_update_count": len(news_updates),
        "filtered_update_count": len(filtered_updates),
        "updated": {"news": 0, "filtered": 0},
        "failed": [],
    }
    if not dry_run:
        updated, failed = apply_updates(config.FEISHU_NEWS_TABLE_ID, tenant_token, news_updates, link_update_sleep_seconds)
        result["updated"]["news"] = updated
        result["failed"].extend({"table": "NEWS", **item} for item in failed)
        if filtered_updates:
            updated, failed = apply_updates(
                config.FEISHU_FILTERED_TABLE_ID,
                tenant_token,
                filtered_updates,
                link_update_sleep_seconds,
            )
            result["updated"]["filtered"] = updated
            result["failed"].extend({"table": "FILTERED", **item} for item in failed)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Expand NEWS/FILTERED keyword record links with parent/owner KEYWORD links.")
    parser.add_argument("--apply", action="store_true", help="Write changes. Without this flag, only dry-run.")
    parser.add_argument("--output", default="", help="Output JSON path.")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--record-max-pages", type=int, default=0, help="Max NEWS/FILTERED pages to scan; defaults to --max-pages.")
    parser.add_argument("--recent-hours", type=float, default=0, help="Only scan NEWS/FILTERED records published within this many hours.")
    parser.add_argument("--update-limit", type=int, default=0, help="Limit updates per table, for testing.")
    parser.add_argument("--link-update-sleep", type=float, default=0.05, help="Sleep seconds between single-record updates.")
    args = parser.parse_args()

    mode = "apply" if args.apply else "dry-run"
    output_path = Path(args.output) if args.output else Path("out") / f"keyword-expanded-link-{mode}-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
    result = run(
        dry_run=not args.apply,
        output_path=output_path,
        page_size=args.page_size,
        max_pages=args.max_pages,
        record_max_pages=args.record_max_pages,
        recent_hours=args.recent_hours,
        update_limit=args.update_limit,
        link_update_sleep_seconds=args.link_update_sleep,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
