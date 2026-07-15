# -*- coding: utf-8 -*-
"""Backup, clear, and verify the keyword-derived layer."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Sequence

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
import rss_ingest
from feishu_client import (
    batch_update_bitable_records,
    get_tenant_access_token,
    http_post,
    list_bitable_fields,
    list_bitable_records,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


PAGE_SIZE = 500
BATCH_SIZE = 50


@dataclass(frozen=True)
class TableSpec:
    name: str
    table_id: str
    keywords_field: str
    keyword_records_field: str


def article_table_specs() -> List[TableSpec]:
    specs = [
        TableSpec("NEWS", config.FEISHU_NEWS_TABLE_ID, config.NEWS_FIELD_KEYWORDS, config.NEWS_FIELD_KEYWORD_RECORDS)
    ]
    if str(getattr(config, "FEISHU_FILTERED_TABLE_ID", "") or "").strip():
        specs.append(
            TableSpec(
                "FILTERED",
                config.FEISHU_FILTERED_TABLE_ID,
                config.FILTERED_FIELD_KEYWORDS,
                config.FILTERED_FIELD_KEYWORD_RECORDS,
            )
        )
    return specs


def build_clear_link_payload(spec: TableSpec, record_id: str) -> Dict[str, Any]:
    return {
        "record_id": record_id,
        "fields": {
            spec.keywords_field: "",
            spec.keyword_records_field: [],
        },
    }


def linked_keyword_ids(raw: Any) -> List[str]:
    if isinstance(raw, dict) and isinstance(raw.get("link_record_ids"), list):
        return [str(item or "").strip() for item in raw.get("link_record_ids") or [] if str(item or "").strip()]
    if isinstance(raw, list):
        out: List[str] = []
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


def extract_article_links(records: List[Dict[str, Any]], spec: TableSpec) -> List[Dict[str, Any]]:
    links: List[Dict[str, Any]] = []
    for record in records:
        fields = record.get("fields") or {}
        keyword_ids = linked_keyword_ids(fields.get(spec.keyword_records_field))
        if keyword_ids:
            links.append({"table": spec.name, "record_id": record.get("record_id"), "keyword_ids": keyword_ids})
    return links


def build_delete_plan(
    keyword_records: List[Dict[str, Any]],
    news_links: List[Dict[str, Any]],
    filtered_links: List[Dict[str, Any]],
) -> Dict[str, Any]:
    link_count = sum(len(item.get("keyword_ids") or []) for item in news_links + filtered_links)
    return {
        "keyword_total": len(keyword_records),
        "delete_count": len(keyword_records),
        "preserves_field_structure": True,
        "article_keyword_link_count": link_count,
        "apply_allowed": link_count == 0,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_records(table_id: str, tenant_token: str, max_pages: int) -> List[Dict[str, Any]]:
    return list_bitable_records(
        config.FEISHU_APP_TOKEN,
        table_id,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=PAGE_SIZE,
        max_pages=max_pages,
    )


def fetch_fields(table_id: str, tenant_token: str) -> List[Dict[str, Any]]:
    return list_bitable_fields(
        config.FEISHU_APP_TOKEN,
        table_id,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=200,
        max_pages=20,
    )


def backup(tenant_token: str, output_dir: Path, max_pages: int) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    keyword_records = fetch_records(config.FEISHU_KEYWORD_TABLE_ID, tenant_token, max_pages)
    write_json(output_dir / "keyword_records.json", keyword_records)
    write_json(output_dir / "keyword_fields.json", fetch_fields(config.FEISHU_KEYWORD_TABLE_ID, tenant_token))

    manifest = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "files": ["keyword_records.json", "keyword_fields.json"],
        "tables": {"KEYWORD": len(keyword_records)},
    }
    for spec in article_table_specs():
        records = fetch_records(spec.table_id, tenant_token, max_pages)
        fields = fetch_fields(spec.table_id, tenant_token)
        links = extract_article_links(records, spec)
        file_prefix = spec.name.lower()
        write_json(output_dir / f"{file_prefix}_keyword_links.json", links)
        write_json(output_dir / f"{file_prefix}_fields.json", fields)
        manifest["files"].extend([f"{file_prefix}_keyword_links.json", f"{file_prefix}_fields.json"])
        manifest["tables"][spec.name] = {"records": len(records), "linked_records": len(links)}

    write_json(output_dir / "manifest.json", manifest)
    return manifest


def clear_links(tenant_token: str, dry_run: bool, output: Optional[Path], max_pages: int) -> Dict[str, Any]:
    report: Dict[str, Any] = {"mode": "clear-links", "dry_run": dry_run, "tables": [], "failed": 0}
    for spec in article_table_specs():
        records = fetch_records(spec.table_id, tenant_token, max_pages)
        linked = [record for record in records if extract_article_links([record], spec)]
        payloads = [build_clear_link_payload(spec, str(record.get("record_id") or "")) for record in linked]
        table_report = {
            "table": spec.name,
            "to_clear": len(payloads),
            "sample": payloads[:20],
            "fields_touched": [spec.keywords_field, spec.keyword_records_field],
        }
        if not dry_run:
            for start in range(0, len(payloads), BATCH_SIZE):
                ok, data = batch_update_bitable_records(
                    config.FEISHU_APP_TOKEN,
                    spec.table_id,
                    tenant_token,
                    payloads[start : start + BATCH_SIZE],
                    config.HTTP_TIMEOUT,
                    config.HTTP_RETRIES,
                )
                if not ok:
                    report["failed"] += len(payloads[start : start + BATCH_SIZE])
                    table_report.setdefault("errors", []).append(data)
        report["tables"].append(table_report)
    if output:
        write_json(output, report)
    return report


def delete_bitable_record(table_id: str, record_id: str, tenant_token: str) -> bool:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{config.FEISHU_APP_TOKEN}/tables/{table_id}/records/batch_delete"
    headers = {"Authorization": f"Bearer {tenant_token}", "Content-Type": "application/json; charset=utf-8"}
    resp = http_post(url, headers, {"records": [record_id]}, config.HTTP_TIMEOUT, config.HTTP_RETRIES)
    data = resp.json()
    return data.get("code") == 0


def delete_keywords(tenant_token: str, dry_run: bool, output: Optional[Path], max_pages: int) -> Dict[str, Any]:
    keyword_records = fetch_records(config.FEISHU_KEYWORD_TABLE_ID, tenant_token, max_pages)
    links_by_table: Dict[str, List[Dict[str, Any]]] = {}
    for spec in article_table_specs():
        links_by_table[spec.name] = extract_article_links(fetch_records(spec.table_id, tenant_token, max_pages), spec)
    plan = build_delete_plan(keyword_records, links_by_table.get("NEWS", []), links_by_table.get("FILTERED", []))
    plan["mode"] = "delete-keywords"
    plan["dry_run"] = dry_run
    plan["failed"] = 0
    if not dry_run:
        if not plan["apply_allowed"]:
            raise RuntimeError("refusing to delete KEYWORD records while NEWS/FILTERED links remain")
        for record in keyword_records:
            record_id = str(record.get("record_id") or "").strip()
            if record_id and not delete_bitable_record(config.FEISHU_KEYWORD_TABLE_ID, record_id, tenant_token):
                plan["failed"] += 1
    if output:
        write_json(output, plan)
    return plan


def audit(tenant_token: str, output: Optional[Path], max_pages: int) -> Dict[str, Any]:
    keyword_records = fetch_records(config.FEISHU_KEYWORD_TABLE_ID, tenant_token, max_pages)
    report: Dict[str, Any] = {"keyword_total": len(keyword_records), "article_keyword_link_count": 0, "tables": []}
    for spec in article_table_specs():
        links = extract_article_links(fetch_records(spec.table_id, tenant_token, max_pages), spec)
        report["article_keyword_link_count"] += sum(len(item["keyword_ids"]) for item in links)
        report["tables"].append({"table": spec.name, "linked_records": len(links)})
    if output:
        write_json(output, report)
    return report


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild keyword derived data safely.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--audit", action="store_true")
    mode.add_argument("--backup", action="store_true")
    mode.add_argument("--plan-delete", action="store_true")
    mode.add_argument("--clear-links", action="store_true")
    mode.add_argument("--delete-keywords", action="store_true")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--backup-dir", default="")
    parser.add_argument("--max-pages", type=int, default=200)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    tenant_token = get_tenant_access_token(config.FEISHU_APP_ID, config.FEISHU_APP_SECRET, config.HTTP_TIMEOUT, config.HTTP_RETRIES)
    output = Path(args.output) if args.output else None
    if args.backup:
        out_dir = Path(args.output_dir) if args.output_dir else Path("out") / f"keyword-backup-{dt.datetime.now():%Y%m%d-%H%M%S}"
        result = backup(tenant_token, out_dir, args.max_pages)
    elif args.clear_links:
        result = clear_links(tenant_token, dry_run=not args.apply, output=output, max_pages=args.max_pages)
    elif args.delete_keywords or args.plan_delete:
        result = delete_keywords(tenant_token, dry_run=not args.apply, output=output, max_pages=args.max_pages)
    else:
        result = audit(tenant_token, output, args.max_pages)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
