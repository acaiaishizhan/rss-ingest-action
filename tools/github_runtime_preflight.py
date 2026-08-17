# -*- coding: utf-8 -*-
"""Validate the GitHub Actions runtime without reading or writing Feishu data."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.local_feed_publisher import validate_feed_bytes, validate_keyword_snapshot_bytes


REQUIRED_ENV = (
    "ARK_API_KEY",
    "ARK_API_KEY_2",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_APP_TOKEN",
    "FEISHU_NEWS_TABLE_ID",
    "FEISHU_RSS_TABLE_ID",
    "FEISHU_FILTERED_TABLE_ID",
    "FEISHU_KEYWORD_TABLE_ID",
)


def validate_ark_keys(
    env: Mapping[str, str],
    *,
    post: Callable = requests.post,
) -> tuple[str, ...]:
    keys = (
        ("ARK_API_KEY", str(env.get("ARK_API_KEY") or "").strip()),
        ("ARK_API_KEY_2", str(env.get("ARK_API_KEY_2") or "").strip()),
    )
    missing = [name for name, value in keys if not value]
    if missing:
        raise RuntimeError(f"missing required GitHub Secrets: {', '.join(missing)}")
    if keys[0][1] == keys[1][1]:
        raise RuntimeError("ARK_API_KEY and ARK_API_KEY_2 must be distinct")

    base_url = str(
        env.get("ARK_BASE_URL") or "https://ark.cn-beijing.volces.com/api/coding/v3"
    ).rstrip("/")
    model = str(env.get("ARK_MODEL") or "ark-code-latest").strip()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly OK and nothing else."}],
        "stream": False,
        "thinking": {"type": "disabled"},
        "max_tokens": 8,
    }

    healthy = []
    for name, api_key in keys:
        response = post(
            f"{base_url}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json=payload,
            timeout=60,
        )
        if response.status_code != 200:
            try:
                error_code = str((response.json().get("error") or {}).get("code") or "").strip()
            except Exception:
                error_code = ""
            suffix = f" {error_code}" if error_code else ""
            raise RuntimeError(f"{name} failed: HTTP {response.status_code}{suffix}")
        try:
            choices = response.json().get("choices") or []
            content = str((((choices[0] if choices else {}).get("message") or {}).get("content") or "")).strip()
        except Exception as exc:
            raise RuntimeError(f"{name} returned invalid JSON") from exc
        if not content:
            raise RuntimeError(f"{name} returned empty content")
        healthy.append(name)
    return tuple(healthy)


def validate_runtime(
    source_map_path: Path,
    env: Mapping[str, str],
    keyword_snapshot_path: Path,
) -> tuple[int, int, int]:
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
    if not keyword_snapshot_path.is_file():
        raise RuntimeError(f"keyword snapshot is unavailable: {keyword_snapshot_path}")
    keyword_count = validate_keyword_snapshot_bytes(keyword_snapshot_path.read_bytes())
    return len(sources), total_items, keyword_count


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-map", required=True, type=Path)
    parser.add_argument("--keyword-snapshot", required=True, type=Path)
    args = parser.parse_args(argv)
    source_count, item_count, keyword_count = validate_runtime(
        args.source_map,
        os.environ,
        args.keyword_snapshot,
    )
    healthy_ark_keys = validate_ark_keys(os.environ)
    print(
        "GitHub runtime preflight passed: "
        f"sources={source_count} items={item_count} keywords={keyword_count} "
        f"ark_keys={len(healthy_ark_keys)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
