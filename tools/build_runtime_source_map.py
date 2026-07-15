# -*- coding: utf-8 -*-
"""Build the private GitHub runtime mapping for local-only RSS sources."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from feishu_client import get_tenant_access_token, list_bitable_records


REQUIRED_FEEDS = {
    "http://localhost:8001/feed/all.rss": "feeds/we-mp-rss.xml",
    "http://127.0.0.1:8787/rss/all.xml": "feeds/private-rss.xml",
    r"F:\coding\rss-ingest-local\data\grok-feeds\deals.xml": "feeds/grok/deals.xml",
    r"F:\coding\rss-ingest-local\data\grok-feeds\rumors.xml": "feeds/grok/rumors.xml",
    r"F:\coding\rss-ingest-local\data\grok-feeds\cases.xml": "feeds/grok/cases.xml",
    r"F:\coding\rss-ingest-local\data\grok-feeds\burst.xml": "feeds/grok/burst.xml",
    r"F:\coding\rss-ingest-local\data\grok-feeds\tips.xml": "feeds/grok/tips.xml",
    r"F:\coding\rss-ingest-local\data\grok-feeds\peers.xml": "feeds/grok/peers.xml",
    r"F:\coding\rss-ingest-local\data\grok-feeds\resources.xml": "feeds/grok/resources.xml",
    r"F:\coding\rss-ingest-local\data\grok-feeds\codex.xml": "feeds/grok/codex.xml",
    r"F:\coding\rss-ingest-local\data\grok-feeds\claude.xml": "feeds/grok/claude.xml",
}


def _field_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("link", "text", "name", "value"):
            if value.get(key):
                return str(value[key]).strip()
    if isinstance(value, list):
        return "".join(_field_text(item) for item in value).strip()
    return str(value).strip()


def build_source_map(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    matches: Dict[str, list[str]] = {url: [] for url in REQUIRED_FEEDS}
    for record in records:
        record_id = str(record.get("record_id") or "").strip()
        fields = record.get("fields") or {}
        feed_url = _field_text(fields.get(config.RSS_FIELD_FEED_URL))
        if record_id and feed_url in matches:
            matches[feed_url].append(record_id)

    sources: Dict[str, str] = {}
    for feed_url, target in REQUIRED_FEEDS.items():
        record_ids = matches[feed_url]
        if not record_ids:
            raise RuntimeError(f"missing required local RSS source: {feed_url}")
        if len(record_ids) > 1:
            raise RuntimeError(f"multiple RSS source records match: {feed_url}")
        sources[record_ids[0]] = target
    return {"version": 1, "sources": sources}


def write_source_map(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    print(f"Wrote runtime source map for {len(payload.get('sources') or {})} sources.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    required = {
        "FEISHU_APP_ID": config.FEISHU_APP_ID,
        "FEISHU_APP_SECRET": config.FEISHU_APP_SECRET,
        "FEISHU_APP_TOKEN": config.FEISHU_APP_TOKEN,
        "FEISHU_RSS_TABLE_ID": config.FEISHU_RSS_TABLE_ID,
    }
    missing = [name for name, value in required.items() if not str(value or "").strip()]
    if missing:
        raise RuntimeError(f"missing required configuration: {', '.join(missing)}")
    tenant_token = get_tenant_access_token(
        config.FEISHU_APP_ID,
        config.FEISHU_APP_SECRET,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )
    records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_RSS_TABLE_ID,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )
    write_source_map(args.output, build_source_map(records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
