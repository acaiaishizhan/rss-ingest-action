# -*- coding: utf-8 -*-
"""Validate the GitHub Actions runtime without reading or writing Feishu data."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.local_feed_publisher import validate_feed_bytes


REQUIRED_ENV = (
    "ARK_API_KEY",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_APP_TOKEN",
    "FEISHU_NEWS_TABLE_ID",
    "FEISHU_RSS_TABLE_ID",
    "FEISHU_FILTERED_TABLE_ID",
    "FEISHU_KEYWORD_TABLE_ID",
)


def validate_runtime(source_map_path: Path, env: Mapping[str, str]) -> tuple[int, int]:
    missing = [name for name in REQUIRED_ENV if not str(env.get(name) or "").strip()]
    if missing:
        raise RuntimeError(f"missing required GitHub Secrets: {', '.join(missing)}")
    if not source_map_path.is_file():
        raise RuntimeError(f"runtime source map is unavailable: {source_map_path}")

    try:
        payload = json.loads(source_map_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"runtime source map has invalid JSON: {exc}") from exc
    sources = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(sources, dict) or not sources:
        raise RuntimeError("runtime source map must contain at least one source")

    base_dir = source_map_path.resolve().parent
    total_items = 0
    for raw_record_id, raw_relative_path in sources.items():
        record_id = str(raw_record_id or "").strip()
        relative_path = str(raw_relative_path or "").strip()
        if not record_id or not relative_path:
            raise RuntimeError("runtime source map entries require non-empty IDs and paths")
        candidate = Path(relative_path)
        if candidate.is_absolute() or re.match(r"^[A-Za-z]:[\\/]", relative_path):
            raise RuntimeError("runtime feed paths must stay inside the private checkout")
        resolved = (base_dir / candidate).resolve()
        try:
            resolved.relative_to(base_dir)
        except ValueError as exc:
            raise RuntimeError("runtime feed paths must stay inside the private checkout") from exc
        if not resolved.is_file():
            raise RuntimeError(f"runtime feed snapshot is unavailable: {relative_path}")
        total_items += validate_feed_bytes(resolved.read_bytes())
    return len(sources), total_items


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-map", required=True, type=Path)
    args = parser.parse_args(argv)
    source_count, item_count = validate_runtime(args.source_map, os.environ)
    print(f"GitHub runtime preflight passed: sources={source_count} items={item_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
