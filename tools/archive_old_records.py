# -*- coding: utf-8 -*-
"""Archive old NEWS/FILTERED records into quarter tables.

The retention decision uses each source row's existing 30d formula field:
30d = 1 stays in the live table, 30d = 0 is eligible for archive.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
from feishu_client import (
    batch_delete_bitable_records,
    create_bitable_record_with_id,
    get_tenant_access_token,
    list_bitable_fields,
    list_bitable_records,
    list_bitable_tables,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


BJ_TZ = timezone(timedelta(hours=8))
WINDOW_FIELD = "30d"
FEISHU_INVALID_TOKEN_CODE = 99991663

NEWS_ARCHIVE_FIELDS = [
    config.NEWS_FIELD_TITLE,
    config.NEWS_FIELD_SCORE,
    config.NEWS_FIELD_CATEGORIES,
    config.NEWS_FIELD_SUMMARY,
    config.NEWS_FIELD_BRIEF_SUMMARY,
    config.NEWS_FIELD_PUBLISHED_MS,
    config.NEWS_FIELD_SOURCE,
    config.NEWS_FIELD_FULL_CONTENT,
    config.NEWS_FIELD_ITEM_KEY,
    config.NEWS_FIELD_KEYWORDS,
    config.NEWS_FIELD_READ,
]

FILTERED_ARCHIVE_FIELDS = [
    config.FILTERED_FIELD_TITLE,
    config.FILTERED_FIELD_FILTER_METHOD,
    config.FILTERED_FIELD_FILTER_REASON,
    config.FILTERED_FIELD_SUMMARY,
    config.FILTERED_FIELD_PUBLISHED_MS,
    config.FILTERED_FIELD_SOURCE,
    config.FILTERED_FIELD_FULL_CONTENT,
    config.FILTERED_FIELD_ITEM_KEY,
    config.FILTERED_FIELD_KEYWORDS,
]


@dataclass(frozen=True)
class ArchivePlanItem:
    source_kind: str
    source_table_id: str
    source_record_id: str
    target_table_name: str
    item_key: str
    fields: Dict[str, Any]
    already_archived: bool = False


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


def parse_ts_ms(value: Any) -> int:
    value = _cell_scalar(value)
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def item_key_from_fields(fields: Mapping[str, Any], source_kind: str) -> str:
    field_name = config.NEWS_FIELD_ITEM_KEY if source_kind == "NEWS" else config.FILTERED_FIELD_ITEM_KEY
    value = _cell_scalar(fields.get(field_name))
    return str(value or "").strip()


def is_archive_candidate(fields: Mapping[str, Any]) -> bool:
    if WINDOW_FIELD not in fields:
        return False
    return parse_int(fields.get(WINDOW_FIELD)) == 0


def archive_time_ms(fields: Mapping[str, Any], source_kind: str) -> int:
    if source_kind == "NEWS":
        published = parse_ts_ms(fields.get(config.NEWS_FIELD_PUBLISHED_MS))
        created = parse_ts_ms(fields.get(config.NEWS_FIELD_CREATED_TIME))
    else:
        published = parse_ts_ms(fields.get(config.FILTERED_FIELD_PUBLISHED_MS))
        created = parse_ts_ms(fields.get(config.FILTERED_FIELD_CREATED_TIME))
    return published or created


def quarter_label(ts_ms: int) -> str:
    if ts_ms <= 0:
        raise ValueError("cannot derive quarter from empty timestamp")
    dt = datetime.fromtimestamp(ts_ms / 1000, BJ_TZ)
    quarter = ((dt.month - 1) // 3) + 1
    return f"{dt.year}Q{quarter}"


def archive_table_name(fields: Mapping[str, Any], source_kind: str) -> str:
    base = quarter_label(archive_time_ms(fields, source_kind))
    return f"{base}回收站" if source_kind == "FILTERED" else base


def build_archive_fields(fields: Mapping[str, Any], source_kind: str) -> Dict[str, Any]:
    allowed = NEWS_ARCHIVE_FIELDS if source_kind == "NEWS" else FILTERED_ARCHIVE_FIELDS
    out: Dict[str, Any] = {}
    for name in allowed:
        if name in fields and fields.get(name) not in (None, ""):
            out[name] = fields.get(name)
    return out


def build_archive_plan(
    source_records: Sequence[Dict[str, Any]],
    source_kind: str,
    existing_item_keys_by_table: Mapping[str, Set[str]],
) -> List[ArchivePlanItem]:
    source_table_id = config.FEISHU_NEWS_TABLE_ID if source_kind == "NEWS" else config.FEISHU_FILTERED_TABLE_ID
    plan: List[ArchivePlanItem] = []
    for record in source_records:
        fields = record.get("fields") or {}
        record_id = str(record.get("record_id") or "").strip()
        if not record_id or not is_archive_candidate(fields):
            continue
        item_key = item_key_from_fields(fields, source_kind)
        if not item_key:
            continue
        try:
            target_table = archive_table_name(fields, source_kind)
        except ValueError:
            continue
        plan.append(
            ArchivePlanItem(
                source_kind=source_kind,
                source_table_id=source_table_id,
                source_record_id=record_id,
                target_table_name=target_table,
                item_key=item_key,
                fields=build_archive_fields(fields, source_kind),
                already_archived=item_key in existing_item_keys_by_table.get(target_table, set()),
            )
        )
    return plan


def _table_name(item: Dict[str, Any]) -> str:
    return str(item.get("name") or item.get("table_name") or "").strip()


def load_table_ids_by_name(tenant_token: str) -> Dict[str, str]:
    tables = list_bitable_tables(
        config.FEISHU_APP_TOKEN,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )
    out: Dict[str, str] = {}
    for table in tables:
        name = _table_name(table)
        table_id = str(table.get("table_id") or "").strip()
        if name and table_id:
            out[name] = table_id
    return out


def load_table_fields_by_name(
    tenant_token: str,
    table_ids_by_name: Mapping[str, str],
    table_names: Iterable[str],
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for table_name in sorted(set(table_names)):
        table_id = table_ids_by_name.get(table_name)
        if not table_id:
            out[table_name] = {}
            continue
        fields = list_bitable_fields(
            config.FEISHU_APP_TOKEN,
            table_id,
            tenant_token,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
        )
        table_fields: Dict[str, Dict[str, Any]] = {}
        for field in fields:
            name = str(field.get("field_name") or field.get("name") or "").strip()
            if name:
                table_fields[name] = field
        out[table_name] = table_fields
    return out


def plain_text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        if "text" in value:
            return str(value.get("text") or "")
        if "value" in value:
            return plain_text_value(value.get("value"))
        return str(value)
    if isinstance(value, list):
        if not value:
            return ""
        if all(isinstance(item, dict) and "text" in item for item in value):
            return "".join(str(item.get("text") or "") for item in value)
        if all(isinstance(item, str) for item in value):
            return ", ".join(item for item in value if item)
        if len(value) == 1:
            return plain_text_value(value[0])
        return "\n".join(part for part in (plain_text_value(item) for item in value) if part)
    return str(value)


def archive_write_fields(fields: Mapping[str, Any], target_fields: Mapping[str, Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, value in fields.items():
        target = target_fields.get(name)
        if target_fields and not target:
            continue
        if target and str(target.get("ui_type") or "") == "Text":
            out[name] = plain_text_value(value)
        else:
            out[name] = value
    return out


def is_invalid_token_error(payload: Any) -> bool:
    return isinstance(payload, Mapping) and payload.get("code") == FEISHU_INVALID_TOKEN_CODE


def fetch_fresh_tenant_token() -> str:
    return get_tenant_access_token(
        config.FEISHU_APP_ID,
        config.FEISHU_APP_SECRET,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )


def load_existing_item_keys(
    tenant_token: str,
    table_ids_by_name: Mapping[str, str],
    target_table_names: Iterable[str],
    page_size: int,
    max_pages: int,
    source_kind: str,
) -> Dict[str, Set[str]]:
    item_key_field = config.NEWS_FIELD_ITEM_KEY if source_kind == "NEWS" else config.FILTERED_FIELD_ITEM_KEY
    existing: Dict[str, Set[str]] = {}
    for table_name in sorted(set(target_table_names)):
        table_id = table_ids_by_name.get(table_name)
        if not table_id:
            existing[table_name] = set()
            continue
        records = list_bitable_records(
            config.FEISHU_APP_TOKEN,
            table_id,
            tenant_token,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
            page_size=page_size,
            max_pages=max_pages,
        )
        keys = set()
        for record in records:
            value = _cell_scalar((record.get("fields") or {}).get(item_key_field))
            if value:
                keys.add(str(value).strip())
        existing[table_name] = keys
    return existing


def _plan_summary(plan: Sequence[ArchivePlanItem]) -> Dict[str, Any]:
    by_target: Dict[str, int] = {}
    already = 0
    for item in plan:
        by_target[item.target_table_name] = by_target.get(item.target_table_name, 0) + 1
        already += 1 if item.already_archived else 0
    return {
        "count": len(plan),
        "already_archived": already,
        "needs_create": len(plan) - already,
        "by_target": dict(sorted(by_target.items())),
        "sample": [
            {
                "source": item.source_kind,
                "source_record_id": item.source_record_id,
                "target_table": item.target_table_name,
                "item_key": item.item_key,
                "already_archived": item.already_archived,
            }
            for item in plan[:30]
        ],
    }


def apply_plan(
    tenant_token: str,
    plan: Sequence[ArchivePlanItem],
    table_ids_by_name: Mapping[str, str],
    fields_by_table_name: Mapping[str, Dict[str, Dict[str, Any]]],
    delete_batch_size: int,
    token_refresher: Optional[Callable[[], str]] = None,
) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
    stats = {"created": 0, "deleted": 0, "already_archived_deleted": 0}
    failed: List[Dict[str, Any]] = []
    delete_ids_by_source: Dict[Tuple[str, str], List[str]] = {}
    current_tenant_token = tenant_token

    def refresh_token() -> str:
        nonlocal current_tenant_token
        current_tenant_token = token_refresher() if token_refresher else fetch_fresh_tenant_token()
        return current_tenant_token

    for item in plan:
        target_table_id = table_ids_by_name.get(item.target_table_name)
        if not target_table_id:
            failed.append({"source_record_id": item.source_record_id, "error": f"missing target table: {item.target_table_name}"})
            continue
        if not item.already_archived:
            target_fields = fields_by_table_name.get(item.target_table_name) or {}
            fields = archive_write_fields(item.fields, target_fields)
            ok, new_record_id = create_bitable_record_with_id(
                config.FEISHU_APP_TOKEN,
                target_table_id,
                current_tenant_token,
                fields,
                config.HTTP_TIMEOUT,
                config.HTTP_RETRIES,
            )
            if not ok and is_invalid_token_error(new_record_id):
                ok, new_record_id = create_bitable_record_with_id(
                    config.FEISHU_APP_TOKEN,
                    target_table_id,
                    refresh_token(),
                    fields,
                    config.HTTP_TIMEOUT,
                    config.HTTP_RETRIES,
                )
            if not ok:
                failed.append(
                    {
                        "source_record_id": item.source_record_id,
                        "target_table": item.target_table_name,
                        "error": "create failed",
                        "detail": new_record_id if isinstance(new_record_id, Mapping) else None,
                    }
                )
                continue
            stats["created"] += 1
        else:
            stats["already_archived_deleted"] += 1
        delete_ids_by_source.setdefault((item.source_kind, item.source_table_id), []).append(item.source_record_id)

    for (source_kind, source_table_id), record_ids in delete_ids_by_source.items():
        for index in range(0, len(record_ids), max(1, delete_batch_size)):
            batch = record_ids[index : index + max(1, delete_batch_size)]
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


def source_archive_filter() -> Dict[str, Any]:
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
        filter_obj=None if scan_all else source_archive_filter(),
        sort=[{"field_name": sort_field, "desc": False}],
    )


def run(
    dry_run: bool,
    output_path: Path,
    page_size: int,
    max_pages: int,
    archive_max_pages: int,
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
    table_ids_by_name = load_table_ids_by_name(tenant_token)
    all_plan: List[ArchivePlanItem] = []
    source_scanned: Dict[str, int] = {}

    # First pass builds rough target names without loading archive tables.
    rough_by_kind: Dict[str, List[Dict[str, Any]]] = {}
    target_names_by_kind: Dict[str, Set[str]] = {}
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
        rough_by_kind[source_kind] = records
        names = set()
        for record in records:
            fields = record.get("fields") or {}
            if is_archive_candidate(fields) and item_key_from_fields(fields, source_kind):
                try:
                    names.add(archive_table_name(fields, source_kind))
                except ValueError:
                    pass
        target_names_by_kind[source_kind] = names

    target_table_names = {name for names in target_names_by_kind.values() for name in names}
    missing_tables = sorted(name for name in target_table_names if name not in table_ids_by_name)
    target_fields = load_table_fields_by_name(tenant_token, table_ids_by_name, target_table_names)

    for source_kind, records in rough_by_kind.items():
        existing = load_existing_item_keys(
            tenant_token,
            table_ids_by_name,
            target_names_by_kind.get(source_kind, set()),
            page_size,
            archive_max_pages,
            source_kind,
        )
        all_plan.extend(build_archive_plan(records, source_kind, existing))

    if apply_limit > 0:
        all_plan = all_plan[:apply_limit]

    result: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "archive-old-records-dry-run" if dry_run else "archive-old-records-apply",
        "rule": "archive source records where source 30d = 0; target quarter is derived from published time, falling back to created time",
        "source_query": "scan-all" if scan_all else "filtered-30d-zero",
        "source_scanned": source_scanned,
        "missing_tables": missing_tables,
        "plan": _plan_summary(all_plan),
        "applied": {"created": 0, "deleted": 0, "already_archived_deleted": 0},
        "failed": [],
    }

    if not dry_run:
        if missing_tables:
            result["failed"].append({"error": "missing archive tables", "tables": missing_tables})
        else:
            applied, failed = apply_plan(tenant_token, all_plan, table_ids_by_name, target_fields, delete_batch_size)
            result["applied"] = applied
            result["failed"] = failed

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive NEWS/FILTERED rows whose source 30d field is zero.")
    parser.add_argument("--apply", action="store_true", help="Write archive rows and delete source rows. Without this flag, dry-run only.")
    parser.add_argument("--output", default="", help="Output JSON path.")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=80, help="Max pages to scan in live NEWS/FILTERED tables.")
    parser.add_argument("--archive-max-pages", type=int, default=80, help="Max pages to scan in archive tables for item_key dedup.")
    parser.add_argument("--apply-limit", type=int, default=0, help="Limit planned migrations, useful for a small apply test.")
    parser.add_argument("--delete-batch-size", type=int, default=500)
    parser.add_argument("--scan-all", action="store_true", help="Diagnostic mode: scan live tables without the 30d=0 server-side filter.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    mode = "apply" if args.apply else "dry-run"
    output_path = Path(args.output) if args.output else Path("out") / f"archive-old-records-{mode}-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
    result = run(
        dry_run=not args.apply,
        output_path=output_path,
        page_size=args.page_size,
        max_pages=args.max_pages,
        archive_max_pages=args.archive_max_pages,
        apply_limit=args.apply_limit,
        delete_batch_size=args.delete_batch_size,
        scan_all=args.scan_all,
    )
    print(
        f"[archive-old-records] mode={result['mode']} "
        f"planned={result['plan']['count']} "
        f"needs_create={result['plan']['needs_create']} "
        f"already_archived={result['plan']['already_archived']} "
        f"missing_tables={len(result['missing_tables'])} "
        f"failed={len(result['failed'])} "
        f"output={output_path}",
        flush=True,
    )
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
