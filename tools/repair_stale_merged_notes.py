# -*- coding: utf-8 -*-
"""Clear stale merged-note markers whose target KEYWORD records no longer exist."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
from feishu_client import batch_update_bitable_records, get_tenant_access_token

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


MERGED_MARKER_LINE_RE = re.compile(r"^\s*\[merged[^\]]*\]\s*(rec[-_0-9A-Za-z]+)\s*$")


def strip_stale_marker_lines(note: Any, missing_target_ids: Sequence[str]) -> str:
    missing = {str(item or "").strip() for item in missing_target_ids if str(item or "").strip()}
    if not missing:
        return str(note or "").strip()
    kept: List[str] = []
    for line in str(note or "").splitlines():
        match = MERGED_MARKER_LINE_RE.match(line)
        if match and match.group(1) in missing:
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def build_updates(audit_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    updates: List[Dict[str, Any]] = []
    for detail in audit_payload.get("stale_merged_note_details") or []:
        if not isinstance(detail, dict):
            continue
        record_id = str(detail.get("record_id") or "").strip()
        old_note = str(detail.get("note") or "").strip()
        new_note = strip_stale_marker_lines(old_note, detail.get("missing_target_ids") or [])
        if not record_id or new_note == old_note:
            continue
        updates.append({"record_id": record_id, "fields": {config.KEYWORD_FIELD_NOTE: new_note}})
    return updates


def apply_updates(tenant_token: str, updates: List[Dict[str, Any]], batch_size: int) -> Tuple[int, List[Dict[str, Any]]]:
    updated = 0
    failed: List[Dict[str, Any]] = []
    for index in range(0, len(updates), max(1, batch_size)):
        batch = updates[index : index + max(1, batch_size)]
        ok, payload = batch_update_bitable_records(
            config.FEISHU_APP_TOKEN,
            config.FEISHU_KEYWORD_TABLE_ID,
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


def run(audit_path: Path, output_path: Path, dry_run: bool, batch_size: int) -> Dict[str, Any]:
    audit_payload = json.loads(audit_path.read_text(encoding="utf-8"))
    updates = build_updates(audit_payload)
    result: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "stale-merged-note-dry-run" if dry_run else "stale-merged-note-apply",
        "audit_path": str(audit_path),
        "update_count": len(updates),
        "updated": 0,
        "failed": [],
        "sample": updates[:50],
    }
    if not dry_run and updates:
        tenant_token = get_tenant_access_token(
            config.FEISHU_APP_ID,
            config.FEISHU_APP_SECRET,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
        )
        updated, failed = apply_updates(tenant_token, updates, batch_size)
        result["updated"] = updated
        result["failed"] = failed

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clear stale merged-note markers from KEYWORD records.")
    parser.add_argument("--audit-path", default="out/keyword-audit-repair/05-keyword-audit-after.json")
    parser.add_argument("--output", default="")
    parser.add_argument("--apply", action="store_true", help="Write KEYWORD note changes. Without this flag, dry-run only.")
    parser.add_argument("--batch-size", type=int, default=500)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    mode = "apply" if args.apply else "dryrun"
    output_path = Path(args.output) if args.output else Path("out") / f"stale-merged-note-{mode}-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
    result = run(
        audit_path=Path(args.audit_path),
        output_path=output_path,
        dry_run=not args.apply,
        batch_size=args.batch_size,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
