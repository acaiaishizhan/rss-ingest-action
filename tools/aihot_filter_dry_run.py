# -*- coding: utf-8 -*-
import argparse
import json
import sys
import time
from collections import Counter
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aihot_filter
import config
from feishu_client import get_tenant_access_token, list_bitable_records
from rss_ingest import normalize_entry_published_ts, normalize_source
from rss_parser import build_item_key, fetch_feed


DEFAULT_OUTPUT = ROOT / ".tmp" / "aihot-filter-dry-run.json"


def parse_pub_ms(entry: Dict[str, Any], now_ms: int) -> int:
    ts = normalize_entry_published_ts(entry, now_ms)
    if ts:
        return ts * 1000
    raw = entry.get("published") or entry.get("updated") or ""
    try:
        parsed = parsedate_to_datetime(str(raw))
        return int(parsed.timestamp() * 1000)
    except Exception:
        return 0


def load_sources() -> List[Dict[str, Any]]:
    tenant_token = get_tenant_access_token(config.FEISHU_APP_ID, config.FEISHU_APP_SECRET, config.HTTP_TIMEOUT, config.HTTP_RETRIES)
    records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_RSS_TABLE_ID,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )
    return [normalize_source(record) for record in records if record.get("record_id")]


def simulate_sources(sources: List[Dict[str, Any]], aihot_url: str, disable_local_rsshub: bool) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    has_aihot = False
    for source in sources:
        copy = dict(source)
        if aihot_filter.is_aihot_source(copy):
            has_aihot = True
            copy["enabled"] = True
            copy["feed_url"] = aihot_url
        elif disable_local_rsshub and aihot_filter.is_local_rsshub_source(copy):
            copy["enabled"] = False
        out.append(copy)
    if not has_aihot:
        out.append(
            {
                "record_id": "__dry_run_aihot__",
                "name": "AI HOT 聚合源",
                "feed_url": aihot_url,
                "enabled": True,
                "item_id_strategy": "link",
                "content_hash_algo": config.DEFAULT_CONTENT_HASH_ALGO,
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run AI HOT shadow filter without writing Feishu or running LLM.")
    parser.add_argument("--url", default="https://aihot.virxact.com/feed/all.xml", help="AI HOT RSS feed URL.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Path to write JSON report.")
    parser.add_argument("--keep-local-rsshub-enabled", action="store_true", help="Use current source enabled flags instead of simulating local RSSHub disabled.")
    parser.add_argument("--limit", type=int, default=0, help="Only inspect the first N feed entries.")
    args = parser.parse_args()

    sources = simulate_sources(load_sources(), args.url, disable_local_rsshub=not args.keep_local_rsshub_enabled)
    aihot_source = {"name": "AI HOT dry-run", "feed_url": args.url, "enabled": True}
    feed = fetch_feed(args.url, config.HTTP_TIMEOUT, config.HTTP_RETRIES, headers={"User-Agent": "NewsDataRSS/aihot-dry-run/1.0"})
    entries = list(getattr(feed, "entries", None) or [])
    if args.limit > 0:
        entries = entries[: args.limit]

    now_ms = int(time.time() * 1000)
    rows: List[Dict[str, Any]] = []
    counts = Counter()
    for entry in entries:
        decision = aihot_filter.decide_aihot_entry(entry, sources, source=aihot_source)
        counts[f"{decision.action}:{decision.reason}"] += 1
        item_key = build_item_key(entry, "link", config.DEFAULT_CONTENT_HASH_ALGO)
        rows.append(
            {
                "action": decision.action,
                "reason": decision.reason,
                "matched_enabled_keys": decision.matched_enabled_keys,
                "matched_disabled_keys": decision.matched_disabled_keys,
                "item_key": item_key,
                "published_ms": parse_pub_ms(entry, now_ms),
                "title": entry.get("title") or "",
                "link": entry.get("link") or "",
                "author": entry.get("author") or "",
            }
        )

    output = {
        "url": args.url,
        "feed_kind": aihot_filter.aihot_feed_kind(args.url),
        "simulate_local_rsshub_disabled": not args.keep_local_rsshub_enabled,
        "entries_checked": len(entries),
        "counts": dict(counts),
        "allowed": [row for row in rows if row["action"] == "allow"],
        "skipped": [row for row in rows if row["action"] != "allow"],
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"entries_checked={len(entries)}")
    for key, value in sorted(counts.items()):
        print(f"{key}={value}")
    print(f"allowed={len(output['allowed'])} skipped={len(output['skipped'])}")
    print(f"wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
