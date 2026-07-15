"""Apply KEYWORD alias links to historical NEWS/FILTERED records."""
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
from feishu_client import batch_update_bitable_records, get_tenant_access_token, list_bitable_records, update_bitable_record_fields
from merge_keywords import KeywordEntry, keyword_entries_from_records, normalize_alias_seed_name, parse_linked_record_ids, parse_ts_ms
from rss_ingest import clean_feishu_value
from tools.keyword_snapshot import keyword_entries_to_records, load_snapshot_entries


def unique_record_ids(values: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        record_id = clean_feishu_value(value).strip()
        if record_id and record_id not in seen:
            seen.add(record_id)
            out.append(record_id)
    return out


def build_alias_link_plans(entries: List[KeywordEntry]) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    entries_by_key: Dict[Tuple[str, str], List[KeywordEntry]] = {}
    for entry in entries:
        key = (entry.type, normalize_alias_seed_name(entry.canonical_name))
        entries_by_key.setdefault(key, []).append(entry)

    alias_to_plan: Dict[str, Dict[str, Any]] = {}
    conflicts: List[Dict[str, Any]] = []
    conflicted_ids = set()

    for main in entries:
        if "[merged" in main.note:
            continue
        for alias in main.aliases:
            alias_key = normalize_alias_seed_name(alias)
            if not alias_key:
                continue
            for alias_entry in entries_by_key.get((main.type, alias_key), []):
                if alias_entry.record_id == main.record_id:
                    continue
                if alias_entry.record_id in conflicted_ids:
                    continue
                plan = {
                    "alias_record_id": alias_entry.record_id,
                    "alias_name": alias_entry.canonical_name,
                    "main_record_id": main.record_id,
                    "main_name": main.canonical_name,
                    "type": main.type,
                }
                existing = alias_to_plan.get(alias_entry.record_id)
                if existing and existing["main_record_id"] != main.record_id:
                    conflicts.append(
                        {
                            "alias_record_id": alias_entry.record_id,
                            "alias_name": alias_entry.canonical_name,
                            "plans": [existing, plan],
                        }
                    )
                    alias_to_plan.pop(alias_entry.record_id, None)
                    conflicted_ids.add(alias_entry.record_id)
                    continue
                if not existing:
                    alias_to_plan[alias_entry.record_id] = plan

    return alias_to_plan, conflicts


def remap_link_ids(link_ids: List[str], alias_to_plan: Dict[str, Dict[str, Any]]) -> Tuple[List[str], bool, List[Dict[str, Any]]]:
    changed = False
    replacements: List[Dict[str, Any]] = []
    out: List[str] = []
    for record_id in link_ids:
        plan = alias_to_plan.get(record_id)
        if plan:
            new_id = plan["main_record_id"]
            changed = True
            replacements.append(plan)
        else:
            new_id = record_id
        out.append(new_id)
    return unique_record_ids(out), changed, replacements


def explicit_alias_plans_from_audit_detail(
    detail: Dict[str, Any],
    entries_by_id: Dict[str, KeywordEntry],
) -> Dict[str, Dict[str, Any]]:
    plans: Dict[str, Dict[str, Any]] = {}
    targets = detail.get("merged_keyword_targets") or []
    if not isinstance(targets, list):
        return plans
    for target in targets:
        if not isinstance(target, dict):
            continue
        alias_id = clean_feishu_value(target.get("alias_record_id")).strip()
        target_ids = [clean_feishu_value(value).strip() for value in target.get("target_record_ids") or []]
        target_ids = [value for value in target_ids if value]
        if not alias_id or len(target_ids) != 1:
            continue
        main_id = target_ids[0]
        if alias_id == main_id or main_id not in entries_by_id:
            continue
        alias_entry = entries_by_id.get(alias_id)
        main_entry = entries_by_id[main_id]
        plans[alias_id] = {
            "alias_record_id": alias_id,
            "alias_name": alias_entry.canonical_name if alias_entry else alias_id,
            "main_record_id": main_id,
            "main_name": main_entry.canonical_name,
            "type": main_entry.type,
        }
    return plans


def build_table_link_updates(
    records: List[Dict[str, Any]],
    link_field: str,
    alias_to_plan: Dict[str, Dict[str, Any]],
    update_limit: int = 0,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    updates: List[Dict[str, Any]] = []
    replacement_counts: Dict[str, int] = {}
    for record in records:
        fields = record.get("fields") or {}
        old_ids = parse_linked_record_ids(fields.get(link_field))
        if not old_ids:
            continue
        new_ids, changed, replacements = remap_link_ids(old_ids, alias_to_plan)
        if not changed:
            continue
        updates.append({"record_id": record.get("record_id"), "fields": {link_field: new_ids}})
        for plan in replacements:
            alias_id = plan["alias_record_id"]
            replacement_counts[alias_id] = replacement_counts.get(alias_id, 0) + 1
        if update_limit > 0 and len(updates) >= update_limit:
            break
    return updates, replacement_counts


def build_keyword_note_updates(
    alias_to_plan: Dict[str, Dict[str, Any]],
    keyword_records: List[Dict[str, Any]],
    replacement_counts: Dict[str, int],
) -> List[Dict[str, Any]]:
    by_id = {record.get("record_id"): record for record in keyword_records}
    updates: List[Dict[str, Any]] = []
    for alias_id, plan in sorted(alias_to_plan.items(), key=lambda item: (item[1]["type"], item[1]["alias_name"].lower())):
        if replacement_counts.get(alias_id, 0) <= 0:
            continue
        record = by_id.get(alias_id)
        if not record:
            continue
        fields = record.get("fields") or {}
        old_note = clean_feishu_value(fields.get(config.KEYWORD_FIELD_NOTE)).strip()
        marker = f"[merged→{plan['main_name']}] {plan['main_record_id']}"
        if marker in old_note:
            continue
        new_note = f"{old_note}\n{marker}".strip() if old_note else marker
        updates.append({"record_id": alias_id, "fields": {config.KEYWORD_FIELD_NOTE: new_note}})
    return updates


def batch_apply(table_id: str, tenant_token: str, updates: List[Dict[str, Any]]) -> Tuple[int, List[Dict[str, Any]]]:
    updated = 0
    failed: List[Dict[str, Any]] = []
    for i in range(0, len(updates), 500):
        batch = updates[i:i + 500]
        ok, payload = batch_update_bitable_records(
            config.FEISHU_APP_TOKEN,
            table_id,
            tenant_token,
            batch,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
        )
        if ok:
            updated += len(batch)
        else:
            failed.append({"record_ids": [item["record_id"] for item in batch], "error": payload})
    return updated, failed


def is_record_not_found_error(payload: Dict[str, Any]) -> bool:
    msg = str(payload.get("msg") or "").lower()
    return payload.get("code") == 1254043 or "record not found" in msg


def apply_keyword_note_updates(
    table_id: str,
    tenant_token: str,
    updates: List[Dict[str, Any]],
) -> Tuple[int, List[Dict[str, Any]], List[Dict[str, Any]]]:
    updated = 0
    failed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    def apply_batch(batch: List[Dict[str, Any]]) -> None:
        nonlocal updated
        ok, payload = batch_update_bitable_records(
            config.FEISHU_APP_TOKEN,
            table_id,
            tenant_token,
            batch,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
        )
        if ok:
            updated += len(batch)
            return
        if len(batch) > 1:
            for item in batch:
                apply_batch([item])
            return
        record_ids = [item["record_id"] for item in batch]
        if is_record_not_found_error(payload):
            skipped.append({"record_ids": record_ids, "reason": "record_not_found"})
        else:
            failed.append({"record_ids": record_ids, "error": payload})

    for i in range(0, len(updates), 500):
        apply_batch(updates[i:i + 500])
    return updated, failed, skipped


def single_apply(
    table_id: str,
    tenant_token: str,
    updates: List[Dict[str, Any]],
    sleep_seconds: float,
) -> Tuple[int, List[Dict[str, Any]]]:
    updated = 0
    failed: List[Dict[str, Any]] = []
    for item in updates:
        ok = update_bitable_record_fields(
            config.FEISHU_APP_TOKEN,
            table_id,
            tenant_token,
            item["record_id"],
            item["fields"],
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
        )
        if ok:
            updated += 1
        else:
            failed.append({"record_ids": [item["record_id"]], "error": "single update returned false"})
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return updated, failed


def summarize_plans(alias_to_plan: Dict[str, Dict[str, Any]], limit: int = 50) -> List[Dict[str, Any]]:
    plans = sorted(alias_to_plan.values(), key=lambda item: (item["type"], item["main_name"].lower(), item["alias_name"].lower()))
    return plans[:limit]


def table_spec_from_audit_name(table: str) -> Tuple[str, str]:
    normalized = str(table or "").strip().upper()
    if normalized == "NEWS":
        return config.FEISHU_NEWS_TABLE_ID, config.NEWS_FIELD_KEYWORD_RECORDS
    if normalized == "FILTERED":
        return config.FEISHU_FILTERED_TABLE_ID, config.FILTERED_FIELD_KEYWORD_RECORDS
    return "", ""


def load_alias_entries(
    tenant_token: str,
    page_size: int,
    max_pages: int,
    keyword_snapshot_path: str = "",
) -> Tuple[List[KeywordEntry], List[Dict[str, Any]]]:
    if keyword_snapshot_path and Path(keyword_snapshot_path).exists():
        entries = load_snapshot_entries(Path(keyword_snapshot_path))
        return entries, keyword_entries_to_records(entries)
    keyword_records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_KEYWORD_TABLE_ID,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=page_size,
        max_pages=max_pages,
    )
    return keyword_entries_from_records(keyword_records), keyword_records


def repair_from_audit_report(
    audit_path: Path,
    output_path: Path,
    page_size: int,
    max_pages: int,
    keyword_snapshot_path: str,
    link_update_sleep_seconds: float,
    dry_run: bool,
) -> Dict[str, Any]:
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    details = audit.get("merged_linked_details") or []
    if not isinstance(details, list):
        details = []
    tenant_token = get_tenant_access_token(
        config.FEISHU_APP_ID,
        config.FEISHU_APP_SECRET,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )
    entries, _keyword_records = load_alias_entries(tenant_token, page_size, max_pages, keyword_snapshot_path)
    entries_by_id = {entry.record_id: entry for entry in entries}
    alias_to_plan, conflicts = build_alias_link_plans(entries)

    result: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "alias-link-repair-apply" if not dry_run else "alias-link-repair-dry-run",
        "audit_path": str(audit_path),
        "input_count": len(details),
        "alias_record_plan_count": len(alias_to_plan),
        "conflict_count": len(conflicts),
        "planned": {"news": 0, "filtered": 0},
        "sample_updates": [],
        "updated": {"news": 0, "filtered": 0},
        "skipped": [],
        "failed": [],
    }
    seen: set = set()
    total = len(details)
    print(f"[alias-link] repair start input={total}", flush=True)
    for index, item in enumerate(details, start=1):
        if not isinstance(item, dict):
            continue
        table = str(item.get("table") or "").strip().upper()
        record_id = str(item.get("record_id") or "").strip()
        table_id, link_field = table_spec_from_audit_name(table)
        key = (table, record_id)
        if not table_id or not link_field or not record_id or key in seen:
            result["skipped"].append({"table": table, "record_id": record_id, "reason": "invalid_or_duplicate"})
            continue
        seen.add(key)
        old_ids = [str(value).strip() for value in item.get("keyword_ids") or [] if str(value).strip()]
        repair_plans = dict(alias_to_plan)
        repair_plans.update(explicit_alias_plans_from_audit_detail(item, entries_by_id))
        new_ids, changed, replacements = remap_link_ids(old_ids, repair_plans)
        if not changed:
            result["skipped"].append({"table": table, "record_id": record_id, "reason": "no_change"})
            continue
        bucket = "news" if table == "NEWS" else "filtered"
        result["planned"][bucket] += 1
        if len(result["sample_updates"]) < 20:
            result["sample_updates"].append(
                {
                    "table": table,
                    "record_id": record_id,
                    "old_keyword_ids": old_ids,
                    "new_keyword_ids": new_ids,
                    "replacements": replacements,
                }
            )
        if dry_run:
            if index % 25 == 0 or index == total:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                print(
                    f"[alias-link] repair progress {index}/{total} "
                    f"news_planned={result['planned']['news']} filtered_planned={result['planned']['filtered']} "
                    f"failed={len(result['failed'])}",
                    flush=True,
                )
            continue
        ok = update_bitable_record_fields(
            config.FEISHU_APP_TOKEN,
            table_id,
            tenant_token,
            record_id,
            {link_field: new_ids},
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
        )
        if ok:
            result["updated"][bucket] += 1
            if link_update_sleep_seconds > 0:
                time.sleep(link_update_sleep_seconds)
        else:
            result["failed"].append({"table": table, "record_id": record_id, "replacements": replacements})
        if index % 25 == 0 or index == total:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(
                f"[alias-link] repair progress {index}/{total} "
                f"news_planned={result['planned']['news']} filtered_planned={result['planned']['filtered']} "
                f"news_updated={result['updated']['news']} filtered_updated={result['updated']['filtered']} "
                f"failed={len(result['failed'])}",
                flush=True,
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def run(
    dry_run: bool,
    output_path: Path,
    page_size: int,
    max_pages: int,
    update_limit: int,
    link_update_sleep_seconds: float,
    keyword_snapshot_path: str = "",
    recent_hours: float = 0,
    record_max_pages: int = 0,
) -> Dict[str, Any]:
    tenant_token = get_tenant_access_token(
        config.FEISHU_APP_ID,
        config.FEISHU_APP_SECRET,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )
    entries, keyword_records = load_alias_entries(tenant_token, page_size, max_pages, keyword_snapshot_path)
    alias_to_plan, conflicts = build_alias_link_plans(entries)

    effective_record_max_pages = record_max_pages if record_max_pages > 0 else max_pages
    news_records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_NEWS_TABLE_ID,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=page_size,
        max_pages=effective_record_max_pages,
        sort=[{"field_name": config.NEWS_FIELD_PUBLISHED_MS, "desc": True}],
        allow_partial=recent_hours > 0,
    )
    filtered_records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_FILTERED_TABLE_ID,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=page_size,
        max_pages=effective_record_max_pages,
        sort=[{"field_name": config.FILTERED_FIELD_PUBLISHED_MS, "desc": True}],
        allow_partial=recent_hours > 0,
    )
    since_ms = int((time.time() - recent_hours * 3600) * 1000) if recent_hours > 0 else 0
    if since_ms:
        news_records = [
            record for record in news_records
            if (parse_ts_ms((record.get("fields") or {}).get(config.NEWS_FIELD_PUBLISHED_MS)) or 0) >= since_ms
        ]
        filtered_records = [
            record for record in filtered_records
            if (parse_ts_ms((record.get("fields") or {}).get(config.FILTERED_FIELD_PUBLISHED_MS)) or 0) >= since_ms
        ]

    news_updates, news_counts = build_table_link_updates(
        news_records, config.NEWS_FIELD_KEYWORD_RECORDS, alias_to_plan, update_limit=update_limit
    )
    filtered_updates, filtered_counts = build_table_link_updates(
        filtered_records, config.FILTERED_FIELD_KEYWORD_RECORDS, alias_to_plan, update_limit=update_limit
    )
    replacement_counts: Dict[str, int] = {}
    for source in (news_counts, filtered_counts):
        for alias_id, count in source.items():
            replacement_counts[alias_id] = replacement_counts.get(alias_id, 0) + count
    keyword_note_updates = build_keyword_note_updates(alias_to_plan, keyword_records, replacement_counts)

    result: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "alias-link-apply" if not dry_run else "alias-link-dry-run",
        "keyword_count": len(entries),
        "alias_record_plan_count": len(alias_to_plan),
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
        "news_scanned": len(news_records),
        "filtered_scanned": len(filtered_records),
        "recent_hours": recent_hours,
        "record_max_pages": effective_record_max_pages,
        "news_update_count": len(news_updates),
        "filtered_update_count": len(filtered_updates),
        "keyword_note_update_count": len(keyword_note_updates),
        "sample_plans": summarize_plans(alias_to_plan),
        "updated": {"news": 0, "filtered": 0, "keyword_notes": 0},
        "skipped": {"keyword_notes": []},
        "failed": [],
    }

    if not dry_run:
        updated, failed = single_apply(config.FEISHU_NEWS_TABLE_ID, tenant_token, news_updates, link_update_sleep_seconds)
        result["updated"]["news"] = updated
        result["failed"].extend({"table": "NEWS", **item} for item in failed)
        updated, failed = single_apply(
            config.FEISHU_FILTERED_TABLE_ID,
            tenant_token,
            filtered_updates,
            link_update_sleep_seconds,
        )
        result["updated"]["filtered"] = updated
        result["failed"].extend({"table": "FILTERED", **item} for item in failed)
        updated, failed, skipped = apply_keyword_note_updates(
            config.FEISHU_KEYWORD_TABLE_ID,
            tenant_token,
            keyword_note_updates,
        )
        result["updated"]["keyword_notes"] = updated
        result["skipped"]["keyword_notes"] = skipped
        result["failed"].extend({"table": "KEYWORD", **item} for item in failed)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Relink historical NEWS/FILTERED keyword records by KEYWORD aliases.")
    parser.add_argument("--apply", action="store_true", help="Write changes. Without this flag, only dry-run.")
    parser.add_argument("--output", default="", help="Output JSON path.")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--update-limit", type=int, default=0, help="Limit NEWS/FILTERED updates per table, for testing.")
    parser.add_argument("--link-update-sleep", type=float, default=0.05, help="Sleep seconds between NEWS/FILTERED single-record link updates.")
    parser.add_argument("--keyword-snapshot-path", default="", help="Use a local KEYWORD snapshot instead of reading the full KEYWORD table.")
    parser.add_argument("--recent-hours", type=float, default=0, help="Only relink NEWS/FILTERED records published within this many hours.")
    parser.add_argument("--record-max-pages", type=int, default=0, help="Max NEWS/FILTERED pages to scan; defaults to --max-pages.")
    parser.add_argument("--repair-audit-path", default="", help="Repair only merged links listed in an audit_keywords JSON report.")
    args = parser.parse_args()

    mode = "apply" if args.apply else "dry-run"
    output_path = Path(args.output) if args.output else Path("out") / f"keyword-alias-link-{mode}-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
    if args.repair_audit_path:
        result = repair_from_audit_report(
            Path(args.repair_audit_path),
            output_path,
            args.page_size,
            args.max_pages,
            args.keyword_snapshot_path,
            args.link_update_sleep,
            dry_run=not args.apply,
        )
        print(
            f"[alias-link] mode={result['mode']} input={result['input_count']} "
            f"news_planned={result['planned']['news']} filtered_planned={result['planned']['filtered']} "
            f"news_updated={result['updated']['news']} filtered_updated={result['updated']['filtered']} "
            f"failed={len(result['failed'])} output={output_path}"
        )
        return 1 if result["failed"] else 0
    result = run(
        dry_run=not args.apply,
        output_path=output_path,
        page_size=args.page_size,
        max_pages=args.max_pages,
        update_limit=args.update_limit,
        link_update_sleep_seconds=args.link_update_sleep,
        keyword_snapshot_path=args.keyword_snapshot_path,
        recent_hours=args.recent_hours,
        record_max_pages=args.record_max_pages,
    )
    print(
        f"[alias-link] mode={result['mode']} plans={result['alias_record_plan_count']} "
        f"conflicts={result['conflict_count']} news_updates={result['news_update_count']} "
        f"filtered_updates={result['filtered_update_count']} keyword_notes={result['keyword_note_update_count']} "
        f"failed={len(result['failed'])} output={output_path}"
    )
    if result["conflict_count"]:
        print("[alias-link] conflicts were skipped; inspect output before applying.", flush=True)
    return 0 if not result["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
