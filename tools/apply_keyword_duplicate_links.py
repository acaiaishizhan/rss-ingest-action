# -*- coding: utf-8 -*-
"""Apply reviewed duplicate KEYWORD audit plans to NEWS/FILTERED links."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
from feishu_client import get_tenant_access_token, list_bitable_records
from rss_ingest import clean_feishu_value
from tools.apply_keyword_alias_links import batch_apply, build_table_link_updates, single_apply

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


DEFAULT_APPROVED_REASONS = {"duplicate_alias_key", "merged_note_linked", "blocklist_alias_key"}


def parse_approved_reasons(raw: str) -> set[str]:
    if not raw.strip():
        return set(DEFAULT_APPROVED_REASONS)
    return {item.strip() for item in raw.split(",") if item.strip()}


def candidate_target_score(candidate: Dict[str, Any]) -> Tuple[int, int, int, str, str]:
    target_usage = candidate.get("target_usage") if isinstance(candidate.get("target_usage"), dict) else {}
    return (
        int(target_usage.get("total_count") or 0),
        int(target_usage.get("news_count") or 0),
        int(target_usage.get("linked_record_count") or 0),
        clean_feishu_value(candidate.get("target_name")).strip().lower(),
        clean_feishu_value(candidate.get("target_record_id")).strip(),
    )


def choose_candidate(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    if candidate_target_score(right) > candidate_target_score(left):
        return right
    return left


def candidate_to_plan(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "alias_record_id": clean_feishu_value(candidate.get("old_record_id")).strip(),
        "alias_name": clean_feishu_value(candidate.get("old_name")).strip(),
        "main_record_id": clean_feishu_value(candidate.get("target_record_id")).strip(),
        "main_name": clean_feishu_value(candidate.get("target_name")).strip(),
        "type": clean_feishu_value(candidate.get("target_type") or candidate.get("old_type")).strip().lower(),
        "reason": clean_feishu_value(candidate.get("reason")).strip(),
    }


def build_alias_plans_from_audit(
    audit_payload: Dict[str, Any],
    approved_reasons: Optional[set[str]] = None,
) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    approved = approved_reasons or set(DEFAULT_APPROVED_REASONS)
    selected_by_old: Dict[str, Dict[str, Any]] = {}
    conflicts: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for candidate in audit_payload.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        old_id = clean_feishu_value(candidate.get("old_record_id")).strip()
        target_id = clean_feishu_value(candidate.get("target_record_id")).strip()
        reason = clean_feishu_value(candidate.get("reason")).strip()
        if not old_id:
            skipped.append({"candidate": candidate, "reason": "missing_old_record_id"})
            continue
        if not target_id:
            skipped.append({"old_record_id": old_id, "old_name": candidate.get("old_name"), "reason": "missing_target"})
            continue
        if reason not in approved:
            skipped.append({"old_record_id": old_id, "old_name": candidate.get("old_name"), "reason": "unapproved_reason", "audit_reason": reason})
            continue

        existing = selected_by_old.get(old_id)
        if existing and clean_feishu_value(existing.get("target_record_id")).strip() != target_id:
            chosen = choose_candidate(existing, candidate)
            selected_by_old[old_id] = chosen
            conflicts.append(
                {
                    "old_record_id": old_id,
                    "old_name": candidate.get("old_name"),
                    "target_record_ids": [
                        clean_feishu_value(existing.get("target_record_id")).strip(),
                        target_id,
                    ],
                    "chosen_target_record_id": clean_feishu_value(chosen.get("target_record_id")).strip(),
                    "chosen_target_name": clean_feishu_value(chosen.get("target_name")).strip(),
                }
            )
            continue
        selected_by_old[old_id] = candidate

    alias_to_plan = {
        old_id: candidate_to_plan(candidate)
        for old_id, candidate in selected_by_old.items()
    }
    return alias_to_plan, conflicts, skipped


def build_keyword_note_updates_for_plans(
    alias_to_plan: Dict[str, Dict[str, Any]],
    keyword_records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    by_id = {clean_feishu_value(record.get("record_id")).strip(): record for record in keyword_records}
    updates: List[Dict[str, Any]] = []
    for alias_id, plan in sorted(alias_to_plan.items(), key=lambda item: (item[1]["type"], item[1]["alias_name"].lower())):
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


def build_duplicate_link_updates(
    alias_to_plan: Dict[str, Dict[str, Any]],
    keyword_records: List[Dict[str, Any]],
    news_records: List[Dict[str, Any]],
    filtered_records: List[Dict[str, Any]],
    update_limit: int = 0,
) -> Dict[str, Any]:
    news_updates, news_replacements = build_table_link_updates(
        news_records,
        config.NEWS_FIELD_KEYWORD_RECORDS,
        alias_to_plan,
        update_limit=update_limit,
    )
    filtered_updates, filtered_replacements = build_table_link_updates(
        filtered_records,
        config.FILTERED_FIELD_KEYWORD_RECORDS,
        alias_to_plan,
        update_limit=update_limit,
    )
    replacement_counts: Dict[str, int] = {}
    for source in [news_replacements, filtered_replacements]:
        for record_id, count in source.items():
            replacement_counts[record_id] = replacement_counts.get(record_id, 0) + count
    return {
        "news_updates": news_updates,
        "filtered_updates": filtered_updates,
        "replacement_counts": replacement_counts,
        "keyword_note_updates": build_keyword_note_updates_for_plans(alias_to_plan, keyword_records),
    }


def fetch_records(tenant_token: str, table_id: str, page_size: int, max_pages: int) -> List[Dict[str, Any]]:
    if not clean_feishu_value(table_id).strip():
        return []
    return list_bitable_records(
        config.FEISHU_APP_TOKEN,
        table_id,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=page_size,
        max_pages=max(1, max_pages),
    )


def run(
    audit_path: Path,
    output_path: Path,
    dry_run: bool,
    page_size: int,
    max_pages: int,
    record_max_pages: int,
    approved_reasons: set[str],
    update_limit: int,
    link_update_sleep_seconds: float,
) -> Dict[str, Any]:
    audit_payload = json.loads(audit_path.read_text(encoding="utf-8"))
    alias_to_plan, conflicts, skipped = build_alias_plans_from_audit(audit_payload, approved_reasons=approved_reasons)
    tenant_token = get_tenant_access_token(config.FEISHU_APP_ID, config.FEISHU_APP_SECRET, config.HTTP_TIMEOUT, config.HTTP_RETRIES)
    keyword_records = fetch_records(tenant_token, config.FEISHU_KEYWORD_TABLE_ID, page_size, max_pages)
    news_records = fetch_records(tenant_token, config.FEISHU_NEWS_TABLE_ID, page_size, record_max_pages if record_max_pages > 0 else max_pages)
    filtered_records = fetch_records(tenant_token, config.FEISHU_FILTERED_TABLE_ID, page_size, record_max_pages if record_max_pages > 0 else max_pages)
    updates = build_duplicate_link_updates(alias_to_plan, keyword_records, news_records, filtered_records, update_limit=update_limit)

    result: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "keyword-duplicate-link-dry-run" if dry_run else "keyword-duplicate-link-apply",
        "audit_path": str(audit_path),
        "plan_count": len(alias_to_plan),
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
        "skipped_count": len(skipped),
        "skipped_sample": skipped[:50],
        "news_update_count": len(updates["news_updates"]),
        "filtered_update_count": len(updates["filtered_updates"]),
        "keyword_note_update_count": len(updates["keyword_note_updates"]),
        "replacement_counts": updates["replacement_counts"],
        "news_update_sample": updates["news_updates"][:20],
        "filtered_update_sample": updates["filtered_updates"][:20],
        "keyword_note_update_sample": updates["keyword_note_updates"][:20],
        "updated": {"news": 0, "filtered": 0, "keyword_notes": 0},
        "failed": {"news": [], "filtered": [], "keyword_notes": []},
    }

    if not dry_run:
        news_updated, news_failed = single_apply(
            config.FEISHU_NEWS_TABLE_ID,
            tenant_token,
            updates["news_updates"],
            link_update_sleep_seconds,
        )
        filtered_updated, filtered_failed = single_apply(
            config.FEISHU_FILTERED_TABLE_ID,
            tenant_token,
            updates["filtered_updates"],
            link_update_sleep_seconds,
        )
        notes_updated, notes_failed = batch_apply(
            config.FEISHU_KEYWORD_TABLE_ID,
            tenant_token,
            updates["keyword_note_updates"],
        )
        result["updated"] = {"news": news_updated, "filtered": filtered_updated, "keyword_notes": notes_updated}
        result["failed"] = {"news": news_failed, "filtered": filtered_failed, "keyword_notes": notes_failed}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply reviewed duplicate KEYWORD links. Defaults to dry-run.")
    parser.add_argument("--audit-path", default="out/keyword-duplicate-audit.json")
    parser.add_argument("--output", default="")
    parser.add_argument("--apply", action="store_true", help="Write NEWS/FILTERED links and KEYWORD merged notes.")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=300)
    parser.add_argument("--record-max-pages", type=int, default=300)
    parser.add_argument("--approved-reasons", default=",".join(sorted(DEFAULT_APPROVED_REASONS)))
    parser.add_argument("--update-limit", type=int, default=0)
    parser.add_argument("--link-update-sleep-seconds", type=float, default=0.0)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    mode = "apply" if args.apply else "dryrun"
    output_path = Path(args.output) if args.output else Path("out") / f"keyword-duplicate-link-{mode}-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
    result = run(
        audit_path=Path(args.audit_path),
        output_path=output_path,
        dry_run=not args.apply,
        page_size=args.page_size,
        max_pages=args.max_pages,
        record_max_pages=args.record_max_pages,
        approved_reasons=parse_approved_reasons(args.approved_reasons),
        update_limit=args.update_limit,
        link_update_sleep_seconds=args.link_update_sleep_seconds,
    )
    print(
        f"[keyword-duplicate-link] mode={result['mode']} plans={result['plan_count']} "
        f"news_updates={result['news_update_count']} filtered_updates={result['filtered_update_count']} "
        f"keyword_notes={result['keyword_note_update_count']} output={output_path}"
    )
    failed = result.get("failed") or {}
    return 1 if any(failed.get(key) for key in failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
