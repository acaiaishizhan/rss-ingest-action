# -*- coding: utf-8 -*-
"""Utilities for persisting KEYWORD table snapshots between daily runs."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import config
from feishu_client import get_tenant_access_token, list_bitable_records
from merge_keywords import KeywordEntry, keyword_entries_from_records, parse_keyword_count, parse_linked_record_ids, parse_ts_ms


SNAPSHOT_SCHEMA_VERSION = 2


def keyword_entry_to_snapshot(entry: KeywordEntry) -> Dict[str, Any]:
    return {
        "record_id": entry.record_id,
        "canonical_name": entry.canonical_name,
        "type": entry.type,
        "aliases": list(entry.aliases),
        "news_count": entry.news_count,
        "filtered_count": entry.filtered_count,
        "note": entry.note,
        "parent_ids": list(entry.parent_ids),
        "owner_ids": list(entry.owner_ids),
    }


def keyword_entry_from_snapshot(item: Dict[str, Any]) -> KeywordEntry:
    aliases = item.get("aliases") or []
    if not isinstance(aliases, list):
        aliases = []
    return KeywordEntry(
        record_id=str(item.get("record_id") or "").strip(),
        canonical_name=str(item.get("canonical_name") or "").strip(),
        type=str(item.get("type") or "").strip().lower(),
        aliases=[str(alias).strip() for alias in aliases if str(alias or "").strip()],
        news_count=parse_keyword_count(item.get("news_count")),
        filtered_count=parse_keyword_count(item.get("filtered_count")),
        note=str(item.get("note") or "").strip(),
        parent_ids=parse_linked_record_ids(item.get("parent_ids")),
        owner_ids=parse_linked_record_ids(item.get("owner_ids")),
    )


def load_snapshot_entries(path: Path) -> List[KeywordEntry]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("keyword snapshot must contain entries list")
    entries = [keyword_entry_from_snapshot(item) for item in items if isinstance(item, dict)]
    return [entry for entry in entries if entry.record_id and entry.canonical_name]


def write_snapshot_entries(path: Path, entries: Iterable[KeywordEntry], source: str = "") -> None:
    sorted_entries = sorted(entries, key=lambda item: (item.type, item.canonical_name.lower(), item.record_id))
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "entry_count": len(sorted_entries),
        "entries": [keyword_entry_to_snapshot(entry) for entry in sorted_entries],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_keyword_entries(base: Iterable[KeywordEntry], updates: Iterable[KeywordEntry]) -> List[KeywordEntry]:
    by_id: Dict[str, KeywordEntry] = {}
    for entry in base:
        if entry.record_id:
            by_id[entry.record_id] = entry
    for entry in updates:
        if entry.record_id:
            by_id[entry.record_id] = entry
    return list(by_id.values())


def keyword_entries_to_records(entries: Iterable[KeywordEntry]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for entry in entries:
        fields: Dict[str, Any] = {
            config.KEYWORD_FIELD_CANONICAL_NAME: entry.canonical_name,
            config.KEYWORD_FIELD_TYPE: entry.type,
            config.KEYWORD_FIELD_ALIASES: "\n".join(entry.aliases),
            config.KEYWORD_FIELD_NEWS_COUNT: entry.news_count,
            config.KEYWORD_FIELD_FILTERED_COUNT: entry.filtered_count,
            config.KEYWORD_FIELD_NOTE: entry.note,
            config.KEYWORD_FIELD_PARENT: list(entry.parent_ids),
            config.KEYWORD_FIELD_OWNERS: list(entry.owner_ids),
        }
        records.append({"record_id": entry.record_id, "fields": fields})
    return records


def fetch_all_keyword_records(page_size: int, max_pages: int, tenant_token: Optional[str] = None) -> List[Dict[str, Any]]:
    if tenant_token is None:
        tenant_token = get_tenant_access_token(
            config.FEISHU_APP_ID,
            config.FEISHU_APP_SECRET,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
        )
    return list_bitable_records(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_KEYWORD_TABLE_ID,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=page_size,
        max_pages=max_pages,
    )


def fetch_recent_keyword_records(
    page_size: int,
    max_pages: int,
    since_ms: int,
    tenant_token: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if tenant_token is None:
        tenant_token = get_tenant_access_token(
            config.FEISHU_APP_ID,
            config.FEISHU_APP_SECRET,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
        )
    records: List[Dict[str, Any]] = []
    page_token_stop = False
    for page in range(max_pages):
        page_records = list_bitable_records(
            config.FEISHU_APP_TOKEN,
            config.FEISHU_KEYWORD_TABLE_ID,
            tenant_token,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
            page_size=page_size,
            max_pages=1,
            sort=[{"field_name": config.KEYWORD_FIELD_FIRST_SEEN, "desc": True}],
            allow_partial=True,
        )
        # list_bitable_records does not expose page_token, so this fallback intentionally
        # fetches one sorted page. In normal daily mode, new keywords are expected to fit
        # inside the first page. Full refresh remains available for baseline repair.
        for record in page_records:
            fields = record.get("fields") or {}
            first_seen = parse_ts_ms(fields.get(config.KEYWORD_FIELD_FIRST_SEEN)) or 0
            if first_seen >= since_ms:
                records.append(record)
            else:
                page_token_stop = True
        break
    return records


def apply_alias_preview_to_entries(entries: Iterable[KeywordEntry], preview: Dict[str, Any]) -> List[KeywordEntry]:
    by_id: Dict[str, KeywordEntry] = {entry.record_id: entry for entry in entries if entry.record_id}
    for item in preview.get("updates") or []:
        record_id = str(item.get("canonical_record_id") or "").strip()
        if not record_id or record_id not in by_id:
            continue
        aliases = item.get("new_aliases") or []
        if not isinstance(aliases, list):
            aliases = []
        entry = by_id[record_id]
        by_id[record_id] = KeywordEntry(
            record_id=entry.record_id,
            canonical_name=entry.canonical_name,
            type=entry.type,
            aliases=[str(alias).strip() for alias in aliases if str(alias or "").strip()],
            news_count=entry.news_count,
            filtered_count=entry.filtered_count,
            note=entry.note,
            parent_ids=list(entry.parent_ids),
            owner_ids=list(entry.owner_ids),
        )
    return list(by_id.values())
