# -*- coding: utf-8 -*-
"""Runtime RSS source routing for local and GitHub-hosted ingest runs."""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.parse import urlparse


class SourceRuntimeConfigError(ValueError):
    """Raised when runtime source routing is configured unsafely."""


@dataclass(frozen=True)
class SkippedSource:
    record_id: str
    name: str
    feed_url: str
    reason: str


@dataclass(frozen=True)
class RuntimeSourceSelection:
    sources: List[Dict[str, Any]]
    skipped: List[SkippedSource]
    overrides_applied: int


def _load_overrides(path_value: str) -> Dict[str, Path]:
    cleaned = str(path_value or "").strip()
    if not cleaned:
        return {}

    mapping_path = Path(cleaned).expanduser()
    if not mapping_path.is_file():
        raise SourceRuntimeConfigError(f"RSS source override file does not exist: {mapping_path}")
    try:
        payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SourceRuntimeConfigError(f"RSS source override file has invalid JSON: {exc}") from exc
    except OSError as exc:
        raise SourceRuntimeConfigError(f"RSS source override file cannot be read: {exc}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), dict):
        raise SourceRuntimeConfigError("RSS source override file must contain an object field named 'sources'")

    base_dir = mapping_path.resolve().parent
    overrides: Dict[str, Path] = {}
    for raw_record_id, raw_relative_path in payload["sources"].items():
        record_id = str(raw_record_id or "").strip()
        relative_path = str(raw_relative_path or "").strip()
        if not record_id or not relative_path:
            raise SourceRuntimeConfigError("RSS source override entries require non-empty record IDs and paths")
        candidate = Path(relative_path)
        if candidate.is_absolute() or re.match(r"^[A-Za-z]:[\\/]", relative_path):
            raise SourceRuntimeConfigError("RSS source override paths must stay inside the mapping directory")
        resolved = (base_dir / candidate).resolve()
        try:
            resolved.relative_to(base_dir)
        except ValueError as exc:
            raise SourceRuntimeConfigError("RSS source override paths must stay inside the mapping directory") from exc
        overrides[record_id] = resolved
    return overrides


def _is_public_http_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return False
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "host.docker.internal"} or hostname.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return address.is_global


def prepare_sources_for_runtime(
    sources: Iterable[Dict[str, Any]],
    *,
    mode: str,
    override_file: str,
) -> RuntimeSourceSelection:
    """Select sources for this runtime and replace explicitly mapped local feeds."""

    normalized_mode = str(mode or "all").strip().lower() or "all"
    source_list = list(sources)
    if normalized_mode == "all":
        return RuntimeSourceSelection(source_list, [], 0)
    if normalized_mode != "github":
        raise SourceRuntimeConfigError(f"unsupported RSS_SOURCE_MODE: {normalized_mode}")

    overrides = _load_overrides(override_file)
    selected: List[Dict[str, Any]] = []
    skipped: List[SkippedSource] = []
    overrides_applied = 0

    for source in source_list:
        record_id = str(source.get("record_id") or "").strip()
        name = str(source.get("name") or record_id).strip()
        feed_url = str(source.get("feed_url") or "").strip()
        override_path = overrides.get(record_id)
        if override_path is not None:
            if override_path.is_file():
                copy = dict(source)
                copy["feed_url"] = str(override_path)
                selected.append(copy)
                overrides_applied += 1
            else:
                skipped.append(SkippedSource(record_id, name, feed_url, "override target missing"))
            continue
        if _is_public_http_url(feed_url):
            selected.append(source)
            continue
        skipped.append(SkippedSource(record_id, name, feed_url, "local or non-public source is unavailable on GitHub"))

    return RuntimeSourceSelection(selected, skipped, overrides_applied)
