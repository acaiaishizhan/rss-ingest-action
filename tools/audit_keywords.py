# -*- coding: utf-8 -*-
"""Audit keyword table cleanliness and derived heat consistency."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
import rss_ingest
from feishu_client import get_tenant_access_token, list_bitable_records

MERGED_NOTE_TARGET_RE = re.compile(r"\[merged[^\]]*\]\s*(rec[0-9A-Za-z]+)")
ALLOWED_COMPACT_DUPLICATE_GROUPS = {
    ("org", "bai"): "BAI and B.AI are intentionally kept as separate keyword records.",
}


def _keyword_id_set(article_links: List[Dict[str, Any]]) -> Set[str]:
    out: Set[str] = set()
    for item in article_links:
        out.update(str(kw_id) for kw_id in item.get("keyword_ids") or [] if str(kw_id))
    return out


def _keyword_record(record: Dict[str, Any]) -> Dict[str, Any]:
    fields = record.get("fields") or {}
    return {
        "record_id": record.get("record_id"),
        "name": rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_CANONICAL_NAME)).strip(),
        "type": rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_TYPE)).strip().lower(),
        "note": rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_NOTE)).strip(),
    }


def _merged_note_target_ids(note: Any) -> List[str]:
    return [match.group(1) for match in MERGED_NOTE_TARGET_RE.finditer(str(note or ""))]


def _is_active_merged_note(note: Any, existing_record_ids: Set[str]) -> bool:
    if not rss_ingest.is_merged_keyword_note(note):
        return False
    target_ids = _merged_note_target_ids(note)
    if not target_ids:
        return True
    return any(target_id in existing_record_ids for target_id in target_ids)


def _stale_merged_note_details(by_id: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    existing_ids = set(by_id)
    details: List[Dict[str, Any]] = []
    for record_id, record in by_id.items():
        note = record.get("note")
        if not rss_ingest.is_merged_keyword_note(note):
            continue
        target_ids = _merged_note_target_ids(note)
        if not target_ids or any(target_id in existing_ids for target_id in target_ids):
            continue
        details.append(
            {
                "record_id": record_id,
                "name": record.get("name"),
                "type": record.get("type"),
                "note": note,
                "missing_target_ids": target_ids,
            }
        )
    return details


def compact_duplicate_groups(keyword_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for record in keyword_records:
        name = str(record.get("name") or "").strip()
        key = (str(record.get("type") or "").strip().lower(), rss_ingest.compact_keyword_alias(name))
        if key[1]:
            groups.setdefault(key, []).append(record)
    out = []
    for (type_, compact), records in groups.items():
        if len(records) <= 1:
            continue
        if (type_, compact) in ALLOWED_COMPACT_DUPLICATE_GROUPS:
            continue
        if all("duplicate-ok:" in str(record.get("note") or "").lower() for record in records):
            continue
        out.append({"type": type_, "compact": compact, "records": records})
    return out


def allowed_compact_duplicate_groups(keyword_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for record in keyword_records:
        name = str(record.get("name") or "").strip()
        key = (str(record.get("type") or "").strip().lower(), rss_ingest.compact_keyword_alias(name))
        if key[1]:
            groups.setdefault(key, []).append(record)
    out = []
    for (type_, compact), records in groups.items():
        if len(records) <= 1 or (type_, compact) not in ALLOWED_COMPACT_DUPLICATE_GROUPS:
            continue
        out.append(
            {
                "type": type_,
                "compact": compact,
                "reason": ALLOWED_COMPACT_DUPLICATE_GROUPS[(type_, compact)],
                "records": records,
            }
        )
    return out


def build_report(
    keyword_records: List[Dict[str, Any]],
    article_links: List[Dict[str, Any]],
    generic_names: Set[str],
    sample_heat_checks: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    linked_ids = _keyword_id_set(article_links)
    generic = {rss_ingest.normalize_keyword_alias(item) for item in generic_names if item}
    by_id = {str(record.get("record_id") or ""): record for record in keyword_records}
    existing_ids = set(by_id)
    merged_ids = {
        record_id
        for record_id, record in by_id.items()
        if _is_active_merged_note(record.get("note"), existing_ids)
    }
    merged_targets = {
        record_id: _merged_note_target_ids(record.get("note"))
        for record_id, record in by_id.items()
        if record_id in merged_ids
    }
    stale_merged_notes = _stale_merged_note_details(by_id)
    generic_ids = {
        record_id
        for record_id, record in by_id.items()
        if rss_ingest.normalize_keyword_alias(record.get("name")) in generic
    }
    duplicate_groups = compact_duplicate_groups(
        [record for record in keyword_records if str(record.get("record_id") or "") not in merged_ids]
    )
    allowed_duplicate_groups = allowed_compact_duplicate_groups(
        [record for record in keyword_records if str(record.get("record_id") or "") not in merged_ids]
    )
    merged_linked_details = [
        {
            "table": item.get("table"),
            "record_id": item.get("record_id"),
            "merged_keyword_ids": [record_id for record_id in item.get("keyword_ids") or [] if record_id in merged_ids],
            "merged_keyword_targets": [
                {
                    "alias_record_id": record_id,
                    "target_record_ids": merged_targets.get(record_id) or [],
                }
                for record_id in item.get("keyword_ids") or []
                if record_id in merged_ids
            ],
            "keyword_ids": item.get("keyword_ids") or [],
        }
        for item in article_links
        if any(record_id in merged_ids for record_id in item.get("keyword_ids") or [])
    ]
    zero_link_count = len([record_id for record_id in by_id if record_id and record_id not in linked_ids])
    heat_checks = sample_heat_checks or []
    return {
        "keyword_total": len(keyword_records),
        "active_keyword_count": len([record_id for record_id in by_id if record_id in linked_ids]),
        "merged_linked_count": len(merged_ids & linked_ids),
        "merged_linked_details": merged_linked_details,
        "stale_merged_note_count": len(stale_merged_notes),
        "stale_merged_note_details": stale_merged_notes[:50],
        "generic_linked_count": len(generic_ids & linked_ids),
        "exact_generic_keyword_count": len(generic_ids),
        "compact_duplicate_groups": len(duplicate_groups),
        "compact_duplicate_group_details": duplicate_groups[:50],
        "allowed_compact_duplicate_groups": len(allowed_duplicate_groups),
        "allowed_compact_duplicate_group_details": allowed_duplicate_groups[:50],
        "zero_link_keyword_count": zero_link_count,
        "sample_heat_checks": heat_checks,
    }


def report_is_healthy(report: Dict[str, Any]) -> bool:
    if int(report.get("merged_linked_count") or 0):
        return False
    if int(report.get("generic_linked_count") or 0):
        return False
    if int(report.get("exact_generic_keyword_count") or 0):
        return False
    if int(report.get("compact_duplicate_groups") or 0):
        return False
    for check in report.get("sample_heat_checks") or []:
        if check.get("ok") is False:
            return False
    return True


def parse_linked_ids(raw: Any) -> List[str]:
    if isinstance(raw, dict) and isinstance(raw.get("link_record_ids"), list):
        return [str(item or "").strip() for item in raw.get("link_record_ids") or [] if str(item or "").strip()]
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, dict):
                value = item.get("record_id") or item.get("id") or item.get("text")
            else:
                value = item
            clean = str(value or "").strip()
            if clean:
                out.append(clean)
        return out
    return []


def fetch_article_links(tenant_token: str, max_pages: int) -> List[Dict[str, Any]]:
    specs = [
        ("NEWS", config.FEISHU_NEWS_TABLE_ID, config.NEWS_FIELD_KEYWORD_RECORDS),
        ("FILTERED", config.FEISHU_FILTERED_TABLE_ID, config.FILTERED_FIELD_KEYWORD_RECORDS),
    ]
    out: List[Dict[str, Any]] = []
    for table, table_id, field in specs:
        if not str(table_id or "").strip():
            continue
        records = list_bitable_records(
            config.FEISHU_APP_TOKEN,
            table_id,
            tenant_token,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
            page_size=500,
            max_pages=max_pages,
        )
        for record in records:
            keyword_ids = parse_linked_ids((record.get("fields") or {}).get(field))
            if keyword_ids:
                out.append({"table": table, "record_id": record.get("record_id"), "keyword_ids": keyword_ids})
    return out


def fetch_keyword_records(tenant_token: str, max_pages: int) -> List[Dict[str, Any]]:
    records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_KEYWORD_TABLE_ID,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=500,
        max_pages=max_pages,
    )
    return [_keyword_record(record) for record in records]


def load_generic_names() -> Set[str]:
    rss_ingest.load_local_prompt_sections()
    return set(getattr(rss_ingest, "_KEYWORD_NAME_BLOCKLIST", set()) or set())


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit keyword cleanliness.")
    parser.add_argument("--output", default="")
    parser.add_argument("--max-pages", type=int, default=200)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    tenant_token = get_tenant_access_token(config.FEISHU_APP_ID, config.FEISHU_APP_SECRET, config.HTTP_TIMEOUT, config.HTTP_RETRIES)
    report = build_report(fetch_keyword_records(tenant_token, args.max_pages), fetch_article_links(tenant_token, args.max_pages), load_generic_names())
    report["healthy"] = report_is_healthy(report)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
