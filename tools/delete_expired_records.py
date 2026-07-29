# -*- coding: utf-8 -*-
"""Delete expired NEWS/FILTERED records without archiving them.

The retention decision uses each source row's existing 30d formula field:
30d = 1 stays in the live table, 30d = 0 is eligible for deletion.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
from feishu_client import (
    batch_delete_bitable_records,
    get_tenant_access_token,
    list_bitable_records,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


WINDOW_FIELD = "30d"
FEISHU_INVALID_TOKEN_CODE = 99991663


@dataclass(frozen=True)
class DeletePlanItem:
    source_kind: str
    source_table_id: str
    source_record_id: str
    item_key: str = ""


def _cell_scalar(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        raw = value.get("value")
        if isinstance(raw, list) and len(raw) == 1:
            return _cell_scalar(raw[0])
        return raw
    if isinstance(value, list) and len(value) == 1:
        return _cell_scalar(value[0])
    if isinstance(value, dict) and "text" in value:
        return value.get("text")
    return value


def parse_int(value: Any) -> Optional[int]:
    value = _cell_scalar(value)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def is_expired_candidate(fields: Mapping[str, Any]) -> bool:
    if WINDOW_FIELD not in fields:
        return False
    return parse_int(fields.get(WINDOW_FIELD)) == 0


def item_key_from_fields(fields: Mapping[str, Any], source_kind: str) -> str:
    field_name = config.NEWS_FIELD_ITEM_KEY if source_kind == "NEWS" else config.FILTERED_FIELD_ITEM_KEY
    value = _cell_scalar(fields.get(field_name))
    return str(value or "").strip()


def build_delete_plan(
    source_records: Sequence[Dict[str, Any]],
    source_kind: str,
    source_table_id: str,
) -> List[DeletePlanItem]:
    plan: List[DeletePlanItem] = []
    for record in source_records:
        fields = record.get("fields") or {}
        record_id = str(record.get("record_id") or "").strip()
        if not record_id or not is_expired_candidate(fields):
            continue
        plan.append(
            DeletePlanItem(
                source_kind=source_kind,
                source_table_id=source_table_id,
                source_record_id=record_id,
                item_key=item_key_from_fields(fields, source_kind),
            )
        )
    return plan


def _plan_summary(plan: Sequence[DeletePlanItem]) -> Dict[str, Any]:
    by_source: Dict[str, int] = {}
    for item in plan:
        by_source[item.source_kind] = by_source.get(item.source_kind, 0) + 1
    return {
        "count": len(plan),
        "by_source": dict(sorted(by_source.items())),
        "sample": [
            {
                "source": item.source_kind,
                "source_record_id": item.source_record_id,
                "item_key": item.item_key,
            }
            for item in plan[:30]
        ],
    }


def is_invalid_token_error(payload: Any) -> bool:
    return isinstance(payload, Mapping) and payload.get("code") == FEISHU_INVALID_TOKEN_CODE


def fetch_fresh_tenant_token() -> str:
    return get_tenant_access_token(
        config.FEISHU_APP_ID,
        config.FEISHU_APP_SECRET,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )


def apply_plan(
    tenant_token: str,
    plan: Sequence[DeletePlanItem],
    delete_batch_size: int,
    token_refresher: Optional[Callable[[], str]] = None,
) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
    stats = {"deleted": 0}
    failed: List[Dict[str, Any]] = []
    delete_ids_by_source: Dict[Tuple[str, str], List[str]] = {}
    current_tenant_token = tenant_token

    def refresh_token() -> str:
        nonlocal current_tenant_token
        current_tenant_token = token_refresher() if token_refresher else fetch_fresh_tenant_token()
        return current_tenant_token

    for item in plan:
        delete_ids_by_source.setdefault((item.source_kind, item.source_table_id), []).append(item.source_record_id)

    batch_size = max(1, delete_batch_size)
    for (source_kind, source_table_id), record_ids in delete_ids_by_source.items():
        for index in range(0, len(record_ids), batch_size):
            batch = record_ids[index : index + batch_size]
            ok, payload = batch_delete_bitable_records(
                config.FEISHU_APP_TOKEN,
                source_table_id,
                current_tenant_token,
                batch,
                config.HTTP_TIMEOUT,
                config.HTTP_RETRIES,
            )
            if not ok and is_invalid_token_error(payload):
                ok, payload = batch_delete_bitable_records(
                    config.FEISHU_APP_TOKEN,
                    source_table_id,
                    refresh_token(),
                    batch,
                    config.HTTP_TIMEOUT,
                    config.HTTP_RETRIES,
                )
            if ok:
                stats["deleted"] += len(batch)
            else:
                failed.append({"source": source_kind, "record_ids": batch, "error": payload})
    return stats, failed


def _source_specs() -> List[Tuple[str, str, str]]:
    specs = [("NEWS", config.FEISHU_NEWS_TABLE_ID, config.NEWS_FIELD_PUBLISHED_MS)]
    if str(getattr(config, "FEISHU_FILTERED_TABLE_ID", "") or "").strip():
        specs.append(("FILTERED", config.FEISHU_FILTERED_TABLE_ID, config.FILTERED_FIELD_PUBLISHED_MS))
    return specs


def source_expired_filter() -> Dict[str, Any]:
    return {
        "conjunction": "and",
        "conditions": [{"field_name": WINDOW_FIELD, "operator": "is", "value": [0]}],
    }


def fetch_source_records(
    tenant_token: str,
    source_kind: str,
    table_id: str,
    sort_field: str,
    page_size: int,
    max_pages: int,
    scan_all: bool = False,
) -> List[Dict[str, Any]]:
    return list_bitable_records(
        config.FEISHU_APP_TOKEN,
        table_id,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=page_size,
        max_pages=max_pages,
        filter_obj=None if scan_all else source_expired_filter(),
        sort=[{"field_name": sort_field, "desc": False}],
    )


def run(
    dry_run: bool,
    output_path: Path,
    page_size: int,
    max_pages: int,
    apply_limit: int,
    delete_batch_size: int,
    scan_all: bool = False,
) -> Dict[str, Any]:
    tenant_token = get_tenant_access_token(
        config.FEISHU_APP_ID,
        config.FEISHU_APP_SECRET,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )
    source_scanned: Dict[str, int] = {}
    all_plan: List[DeletePlanItem] = []

    for source_kind, table_id, sort_field in _source_specs():
        records = fetch_source_records(
            tenant_token,
            source_kind,
            table_id,
            sort_field,
            page_size=page_size,
            max_pages=max_pages,
            scan_all=scan_all,
        )
        source_scanned[source_kind] = len(records)
        all_plan.extend(build_delete_plan(records, source_kind, table_id))

    if apply_limit > 0:
        all_plan = all_plan[:apply_limit]

    result: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "delete-expired-records-dry-run" if dry_run else "delete-expired-records-apply",
        "rule": "delete source records where source 30d = 0; do not copy or sync them to archive tables",
        "source_query": "scan-all" if scan_all else "filtered-30d-zero",
        "source_scanned": source_scanned,
        "plan": _plan_summary(all_plan),
        "applied": {"deleted": 0},
        "failed": [],
    }

    if not dry_run:
        applied, failed = apply_plan(tenant_token, all_plan, delete_batch_size)
        result["applied"] = applied
        result["failed"] = failed

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delete NEWS/FILTERED rows whose source 30d field is zero.")
    parser.add_argument("--apply", action="store_true", help="Delete expired source rows. Without this flag, dry-run only.")
    parser.add_argument("--output", default="", help="Output JSON path.")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=80, help="Max pages to scan in live NEWS/FILTERED tables.")
    parser.add_argument("--apply-limit", type=int, default=0, help="Limit planned deletions, useful for a small apply test.")
    parser.add_argument("--delete-batch-size", type=int, default=500)
    parser.add_argument("--scan-all", action="store_true", help="Diagnostic mode: scan live tables without the 30d=0 server-side filter.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    mode = "apply" if args.apply else "dry-run"
    output_path = (
        Path(args.output)
        if args.output
        else Path("out") / f"delete-expired-records-{mode}-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
    )
    result = run(
        dry_run=not args.apply,
        output_path=output_path,
        page_size=args.page_size,
        max_pages=args.max_pages,
        apply_limit=args.apply_limit,
        delete_batch_size=args.delete_batch_size,
        scan_all=args.scan_all,
    )
    print(
        f"[delete-expired-records] mode={result['mode']} "
        f"planned={result['plan']['count']} "
        f"deleted={result['applied']['deleted']} "
        f"failed={len(result['failed'])} "
        f"output={output_path}",
        flush=True,
    )
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
