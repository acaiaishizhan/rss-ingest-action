# -*- coding: utf-8 -*-
"""Read-only audit for historical KEYWORD duplicate and dirty-link cleanup."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
import rss_ingest
from feishu_client import get_tenant_access_token, list_bitable_records
from merge_keywords import parse_aliases, parse_keyword_count
from tools.audit_keywords import parse_linked_ids

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


MERGED_TARGET_RE = re.compile(r"\[merged[^\]]*\]\s*(rec[0-9A-Za-z_]+)")


@dataclass(frozen=True)
class KeywordAuditEntry:
    record_id: str
    canonical_name: str
    type: str
    aliases: List[str]
    note: str = ""
    news_count: int = 0
    filtered_count: int = 0


def usage_payload(entry: Optional[KeywordAuditEntry], link_counts: Optional[Dict[str, int]] = None) -> Dict[str, int]:
    if entry is None:
        return {"news_count": 0, "filtered_count": 0, "total_count": 0, "linked_record_count": 0}
    total = max(0, entry.news_count) + max(0, entry.filtered_count)
    return {
        "news_count": max(0, entry.news_count),
        "filtered_count": max(0, entry.filtered_count),
        "total_count": total,
        "linked_record_count": (link_counts or {}).get(entry.record_id, 0),
    }


def is_merged(entry: KeywordAuditEntry) -> bool:
    return rss_ingest.is_merged_keyword_note(entry.note)


def normalized_blocklist(generic_names: Iterable[str]) -> Set[str]:
    return {rss_ingest.normalize_keyword_alias(item) for item in generic_names if str(item or "").strip()}


def is_blocked(entry: KeywordAuditEntry, generic: Set[str]) -> bool:
    name = entry.canonical_name
    return rss_ingest.normalize_keyword_alias(name) in generic or rss_ingest._is_junk_keyword(name)


def alias_keys_for_entry(entry: KeywordAuditEntry, generic: Set[str]) -> List[Tuple[str, str]]:
    keys: List[Tuple[str, str]] = []
    for value in [entry.canonical_name, *entry.aliases]:
        if not str(value or "").strip():
            continue
        for key in rss_ingest.keyword_alias_index_keys(value):
            kind = "compact" if key.startswith("compact:") else "normalized"
            keys.append((kind, key))
    out: List[Tuple[str, str]] = []
    seen = set()
    for item in keys:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def choose_target(entries: List[KeywordAuditEntry], generic: Set[str]) -> Optional[KeywordAuditEntry]:
    eligible = [entry for entry in entries if not is_merged(entry) and not is_blocked(entry, generic)]
    if not eligible:
        return None
    return sorted(
        eligible,
        key=lambda entry: (
            -(max(0, entry.news_count) + max(0, entry.filtered_count)),
            -max(0, entry.news_count),
            -max(0, entry.filtered_count),
            len(entry.canonical_name),
            entry.canonical_name.lower(),
            entry.record_id,
        ),
    )[0]


def link_counts_from_article_links(article_links: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in article_links:
        for record_id in item.get("keyword_ids") or []:
            clean = str(record_id or "").strip()
            if clean:
                counts[clean] = counts.get(clean, 0) + 1
    return counts


def linked_article_samples(article_links: List[Dict[str, Any]], record_id: str, limit: int = 10) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for item in article_links:
        if record_id not in set(item.get("keyword_ids") or []):
            continue
        out.append(
            {
                "table": str(item.get("table") or ""),
                "record_id": str(item.get("record_id") or ""),
            }
        )
        if len(out) >= limit:
            break
    return out


def parse_merged_target_id(note: str) -> str:
    match = MERGED_TARGET_RE.search(str(note or ""))
    return match.group(1) if match else ""


def candidate_payload(
    old: KeywordAuditEntry,
    target: Optional[KeywordAuditEntry],
    reason: str,
    generic: Set[str],
    link_counts: Dict[str, int],
    article_links: List[Dict[str, Any]],
    alias_key: str = "",
    alias_key_kind: str = "",
) -> Dict[str, Any]:
    target_id = target.record_id if target else ""
    linked_count = link_counts.get(old.record_id, 0)
    return {
        "old_record_id": old.record_id,
        "old_name": old.canonical_name,
        "old_type": old.type,
        "target_record_id": target_id,
        "target_name": target.canonical_name if target else "",
        "target_type": target.type if target else "",
        "reason": reason,
        "alias_key": alias_key,
        "alias_key_kind": alias_key_kind,
        "old_is_blocked": is_blocked(old, generic),
        "old_is_merged_note": is_merged(old),
        "target_is_blocked": is_blocked(target, generic) if target else False,
        "old_usage": usage_payload(old, link_counts),
        "target_usage": usage_payload(target, link_counts),
        "needs_relink": bool(linked_count and target_id and target_id != old.record_id),
        "linked_record_count": linked_count,
        "linked_record_samples": linked_article_samples(article_links, old.record_id),
    }


def build_duplicate_audit(
    entries: List[KeywordAuditEntry],
    article_links: List[Dict[str, Any]],
    generic_names: Iterable[str],
) -> Dict[str, Any]:
    generic = normalized_blocklist(generic_names)
    entries_by_id = {entry.record_id: entry for entry in entries if entry.record_id}
    link_counts = link_counts_from_article_links(article_links)
    groups: Dict[Tuple[str, str, str], List[KeywordAuditEntry]] = {}
    for entry in entries:
        for kind, key in alias_keys_for_entry(entry, generic):
            groups.setdefault((entry.type, kind, key), []).append(entry)

    candidates: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    duplicate_group_details: List[Dict[str, Any]] = []
    for (type_, kind, key), group in sorted(groups.items(), key=lambda item: item[0]):
        unique = {entry.record_id: entry for entry in group if entry.record_id}
        if len(unique) <= 1:
            continue
        group_entries = list(unique.values())
        target = choose_target(group_entries, generic)
        duplicate_group_details.append(
            {
                "type": type_,
                "alias_key": key,
                "alias_key_kind": kind,
                "target_record_id": target.record_id if target else "",
                "records": [
                    {
                        "record_id": entry.record_id,
                        "name": entry.canonical_name,
                        "news_count": entry.news_count,
                        "filtered_count": entry.filtered_count,
                        "is_blocked": is_blocked(entry, generic),
                        "is_merged_note": is_merged(entry),
                    }
                    for entry in sorted(group_entries, key=lambda item: item.canonical_name.lower())
                ],
            }
        )
        if not target:
            continue
        for old in group_entries:
            if old.record_id == target.record_id or is_merged(old):
                continue
            reason = "blocklist_alias_key" if is_blocked(old, generic) else "duplicate_alias_key"
            key_tuple = (old.record_id, target.record_id, reason)
            if key_tuple not in candidates:
                candidates[key_tuple] = candidate_payload(
                    old,
                    target,
                    reason,
                    generic,
                    link_counts,
                    article_links,
                    alias_key=key,
                    alias_key_kind=kind,
                )

    for entry in entries:
        if not is_merged(entry) or link_counts.get(entry.record_id, 0) <= 0:
            continue
        target_id = parse_merged_target_id(entry.note)
        target = entries_by_id.get(target_id) if target_id else None
        if target and (is_merged(target) or is_blocked(target, generic)):
            target = None
        key_tuple = (entry.record_id, target.record_id if target else "", "merged_note_linked")
        candidates.setdefault(
            key_tuple,
            candidate_payload(entry, target, "merged_note_linked", generic, link_counts, article_links),
        )

    for entry in entries:
        if not is_blocked(entry, generic) or link_counts.get(entry.record_id, 0) <= 0:
            continue
        if any(candidate["old_record_id"] == entry.record_id for candidate in candidates.values()):
            continue
        candidates[(entry.record_id, "", "blocklist_linked")] = candidate_payload(
            entry,
            None,
            "blocklist_linked",
            generic,
            link_counts,
            article_links,
        )

    candidate_list = sorted(
        candidates.values(),
        key=lambda item: (
            0 if item["needs_relink"] else 1,
            item["reason"],
            -int(item["old_usage"]["total_count"]),
            item["old_name"].lower(),
            item["old_record_id"],
        ),
    )
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "keyword-duplicate-audit-dry-run",
        "keyword_count": len(entries),
        "article_link_count": len(article_links),
        "candidate_count": len(candidate_list),
        "needs_relink_count": sum(1 for item in candidate_list if item["needs_relink"]),
        "duplicate_group_count": len(duplicate_group_details),
        "duplicate_group_details": duplicate_group_details[:200],
        "candidates": candidate_list,
    }


def keyword_entries_from_records(records: List[Dict[str, Any]]) -> List[KeywordAuditEntry]:
    entries: List[KeywordAuditEntry] = []
    for record in records:
        record_id = rss_ingest.clean_feishu_value(record.get("record_id")).strip()
        fields = record.get("fields") or {}
        canonical = rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_CANONICAL_NAME)).strip()
        if not record_id or not canonical:
            continue
        entries.append(
            KeywordAuditEntry(
                record_id=record_id,
                canonical_name=canonical,
                type=rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_TYPE)).strip().lower(),
                aliases=parse_aliases(fields.get(config.KEYWORD_FIELD_ALIASES)),
                note=rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_NOTE)).strip(),
                news_count=parse_keyword_count(fields.get(config.KEYWORD_FIELD_NEWS_COUNT)),
                filtered_count=parse_keyword_count(fields.get(config.KEYWORD_FIELD_FILTERED_COUNT)),
            )
        )
    return entries


def fetch_keyword_entries(tenant_token: str, page_size: int, max_pages: int) -> List[KeywordAuditEntry]:
    records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_KEYWORD_TABLE_ID,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=page_size,
        max_pages=max(1, max_pages),
    )
    return keyword_entries_from_records(records)


def fetch_article_links(tenant_token: str, page_size: int, max_pages: int) -> List[Dict[str, Any]]:
    specs = [
        ("NEWS", config.FEISHU_NEWS_TABLE_ID, config.NEWS_FIELD_KEYWORD_RECORDS),
        ("FILTERED", config.FEISHU_FILTERED_TABLE_ID, config.FILTERED_FIELD_KEYWORD_RECORDS),
    ]
    out: List[Dict[str, Any]] = []
    for table, table_id, field_name in specs:
        if not str(table_id or "").strip():
            continue
        records = list_bitable_records(
            config.FEISHU_APP_TOKEN,
            table_id,
            tenant_token,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
            page_size=page_size,
            max_pages=max(1, max_pages),
        )
        for record in records:
            keyword_ids = parse_linked_ids((record.get("fields") or {}).get(field_name))
            if keyword_ids:
                out.append({"table": table, "record_id": record.get("record_id"), "keyword_ids": keyword_ids})
    return out


def load_generic_names(blocklist_path: str) -> Set[str]:
    if blocklist_path:
        path = Path(blocklist_path)
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        names = set()
        for raw_line in raw.replace("\r\n", "\n").splitlines():
            word = raw_line.strip()
            if word.startswith(("-", "*")):
                word = word[1:].strip()
            if word and not word.startswith("#"):
                names.add(word)
        return names
    return rss_ingest._load_keyword_name_blocklist()


def write_csv(path: Path, candidates: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "old_record_id",
        "old_name",
        "old_type",
        "target_record_id",
        "target_name",
        "reason",
        "old_total_count",
        "target_total_count",
        "linked_record_count",
        "needs_relink",
        "old_is_blocked",
        "old_is_merged_note",
        "alias_key_kind",
        "alias_key",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in candidates:
            writer.writerow(
                {
                    "old_record_id": item["old_record_id"],
                    "old_name": item["old_name"],
                    "old_type": item["old_type"],
                    "target_record_id": item["target_record_id"],
                    "target_name": item["target_name"],
                    "reason": item["reason"],
                    "old_total_count": item["old_usage"]["total_count"],
                    "target_total_count": item["target_usage"]["total_count"],
                    "linked_record_count": item["linked_record_count"],
                    "needs_relink": item["needs_relink"],
                    "old_is_blocked": item["old_is_blocked"],
                    "old_is_merged_note": item["old_is_merged_note"],
                    "alias_key_kind": item["alias_key_kind"],
                    "alias_key": item["alias_key"],
                }
            )


def run(output_path: Path, csv_output_path: Optional[Path], page_size: int, max_pages: int, record_max_pages: int, blocklist_path: str) -> Dict[str, Any]:
    tenant_token = get_tenant_access_token(
        config.FEISHU_APP_ID,
        config.FEISHU_APP_SECRET,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )
    entries = fetch_keyword_entries(tenant_token, page_size, max_pages)
    article_links = fetch_article_links(tenant_token, page_size, record_max_pages if record_max_pages > 0 else max_pages)
    report = build_duplicate_audit(entries, article_links, load_generic_names(blocklist_path))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if csv_output_path:
        write_csv(csv_output_path, report["candidates"])
    return report


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only audit for duplicate/dirty KEYWORD records and old links.")
    parser.add_argument("--output", default="", help="Output JSON path.")
    parser.add_argument("--csv-output", default="", help="Optional CSV candidate path for human review.")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=200, help="Max KEYWORD pages to read.")
    parser.add_argument("--record-max-pages", type=int, default=200, help="Max NEWS/FILTERED pages to scan for linked records.")
    parser.add_argument("--blocklist-path", default="docs/local-keyword-name-blocklist.txt")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    output_path = Path(args.output) if args.output else Path("out") / f"keyword-duplicate-audit-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
    csv_output_path = Path(args.csv_output) if args.csv_output else None
    report = run(
        output_path=output_path,
        csv_output_path=csv_output_path,
        page_size=args.page_size,
        max_pages=args.max_pages,
        record_max_pages=args.record_max_pages,
        blocklist_path=args.blocklist_path,
    )
    print(
        f"[keyword-duplicate-audit] candidates={report['candidate_count']} "
        f"needs_relink={report['needs_relink_count']} duplicate_groups={report['duplicate_group_count']} "
        f"output={output_path}"
    )
    if csv_output_path:
        print(f"[keyword-duplicate-audit] csv={csv_output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
