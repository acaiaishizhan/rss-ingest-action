# -*- coding: utf-8 -*-
"""Backfill keyword links for recent NEWS and FILTERED records."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
import rss_ingest
from feishu_client import (
    batch_update_bitable_records,
    get_tenant_access_token,
    http_get,
    list_bitable_records,
    update_bitable_record_fields,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


DEFAULT_DAYS = 30
DEFAULT_MAX_RECORDS = 50
DEFAULT_PAGE_SIZE = 200
DEFAULT_MAX_PAGES = 50
DEFAULT_SCHEMA_RETRIES = 3
DEFAULT_LLM_CONCURRENCY = max(1, int(getattr(config, "LLM_CONCURRENCY", 4) or 4))
DEFAULT_KEYWORD_MAX_PAGES = 200
DEFAULT_STATE_PATH = Path("out/backfill-state.json")
WRITE_DELAY_SECONDS = 0.5
BATCH_UPDATE_SIZE = 50
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
RETRY_SCHEMA_HINT = """

【重试纠错】
上一轮输出未通过系统校验。请只返回合法 JSON，并严格遵守：
- keywords 数量 1-3 个；
- 每个 keyword.name 必须 ≤20 个字符；
- keyword.type 必须是提示词中列出的 9 个小写英文枚举之一；
- 宁可只输出 1 个短专有名词，也不要输出长标题式短语。
"""


@dataclass(frozen=True)
class TableSpec:
    name: str
    table_id: str
    title_field: str
    summary_field: str
    full_content_field: str
    published_field: str
    created_field: str
    keywords_field: str
    keyword_records_field: str


@dataclass
class BackfillStats:
    scanned: int = 0
    processed: int = 0
    skipped: int = 0
    updated: int = 0
    failed: int = 0


@dataclass
class AnalyzeResult:
    spec: TableSpec
    record: Dict[str, Any]
    record_id: str
    title: str
    names: List[str]
    keywords: List[Dict[str, Any]]


@dataclass
class PendingUpdate:
    spec: TableSpec
    record_id: str
    keyword_record_ids: List[str]
    keyword_names: List[str]
    overwrite_empty: bool = False


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill keyword records for recent NEWS and FILTERED records."
    )
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="Analyze and print only.")
    parser.add_argument("--apply", dest="dry_run", action="store_false", help="Write keyword links to Feishu.")
    parser.set_defaults(dry_run=True)
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS, help="Total records to process.")
    parser.add_argument("--provider", default="", help="Override LLM provider for the screen stage.")
    parser.add_argument("--model", default="", help="Override LLM model for the screen stage.")
    parser.add_argument("--date", default="", help="Process one local date only, formatted as YYYY-MM-DD.")
    parser.add_argument("--since-date", default="", help="Process records on or after this local date, YYYY-MM-DD.")
    parser.add_argument("--before-date", default="", help="Process records before this local date, YYYY-MM-DD.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Backfill records published in the last N days.")
    parser.add_argument("--record-ids", default="", help="JSON file limiting backfill to specific record IDs.")
    parser.add_argument(
        "--state-path",
        default=str(DEFAULT_STATE_PATH),
        help="JSON state file used to skip records already successfully processed.",
    )
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="Feishu records/search page size.")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="Feishu records/search max pages.")
    parser.add_argument(
        "--llm-concurrency",
        type=int,
        default=DEFAULT_LLM_CONCURRENCY,
        help="Number of concurrent LLM keyword extraction calls. Feishu writes stay serial.",
    )
    parser.add_argument(
        "--schema-retries",
        type=int,
        default=DEFAULT_SCHEMA_RETRIES,
        help="Retry LLM calls when screen result fails schema validation.",
    )
    parser.add_argument(
        "--sync-keyword-names",
        action="store_true",
        help="Fill the visible keywords field from existing keyword record links without calling LLM.",
    )
    parser.add_argument(
        "--rebuild-existing",
        action="store_true",
        help=(
            "Re-analyze records that already have keyword links. With --apply, overwrite "
            "both keyword fields in the selected window."
        ),
    )
    parser.add_argument(
        "--clear-when-empty",
        action="store_true",
        help="With --rebuild-existing, clear old keyword fields when the model returns no valid keywords.",
    )
    parser.add_argument(
        "--keyword-max-pages",
        type=int,
        default=DEFAULT_KEYWORD_MAX_PAGES,
        help="Max pages to read from the KEYWORD table when building the local keyword index.",
    )
    return parser.parse_args(argv)


def table_specs() -> List[TableSpec]:
    specs = [
        TableSpec(
            name="NEWS",
            table_id=config.FEISHU_NEWS_TABLE_ID,
            title_field=config.NEWS_FIELD_TITLE,
            summary_field=config.NEWS_FIELD_SUMMARY,
            full_content_field=config.NEWS_FIELD_FULL_CONTENT,
            published_field=config.NEWS_FIELD_PUBLISHED_MS,
            created_field=config.NEWS_FIELD_CREATED_TIME,
            keywords_field=config.NEWS_FIELD_KEYWORDS,
            keyword_records_field=config.NEWS_FIELD_KEYWORD_RECORDS,
        )
    ]
    if str(getattr(config, "FEISHU_FILTERED_TABLE_ID", "") or "").strip():
        specs.append(
            TableSpec(
                name="FILTERED",
                table_id=config.FEISHU_FILTERED_TABLE_ID,
                title_field=config.FILTERED_FIELD_TITLE,
                summary_field=config.FILTERED_FIELD_SUMMARY,
                full_content_field=config.FILTERED_FIELD_FULL_CONTENT,
                published_field=config.FILTERED_FIELD_PUBLISHED_MS,
                created_field=config.FILTERED_FIELD_CREATED_TIME,
                keywords_field=config.FILTERED_FIELD_KEYWORDS,
                keyword_records_field=config.FILTERED_FIELD_KEYWORD_RECORDS,
            )
        )
    return specs


def cutoff_ms(days: int) -> int:
    now = dt.datetime.now(dt.timezone.utc)
    return int((now - dt.timedelta(days=max(1, days))).timestamp() * 1000)


def date_window_ms(date_text: str) -> Tuple[int, int]:
    try:
        day = dt.date.fromisoformat(str(date_text or "").strip())
    except ValueError as exc:
        raise ValueError("--date must be formatted as YYYY-MM-DD") from exc
    start = dt.datetime.combine(day, dt.time.min, tzinfo=LOCAL_TZ)
    end = start + dt.timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def date_start_ms(date_text: str, arg_name: str) -> int:
    try:
        day = dt.date.fromisoformat(str(date_text or "").strip())
    except ValueError as exc:
        raise ValueError(f"{arg_name} must be formatted as YYYY-MM-DD") from exc
    start = dt.datetime.combine(day, dt.time.min, tzinfo=LOCAL_TZ)
    return int(start.timestamp() * 1000)


def resolve_window(args: argparse.Namespace) -> Tuple[int, Optional[int], str]:
    if args.date and (args.since_date or args.before_date):
        raise ValueError("--date cannot be combined with --since-date or --before-date")
    if args.date:
        since_ms, before_ms = date_window_ms(args.date)
        return since_ms, before_ms, f"date={args.date}"
    since_ms = date_start_ms(args.since_date, "--since-date") if args.since_date else cutoff_ms(args.days)
    before_ms = date_start_ms(args.before_date, "--before-date") if args.before_date else None
    if before_ms is not None and since_ms >= before_ms:
        raise ValueError("--since-date must be earlier than --before-date")
    if args.since_date or args.before_date:
        return since_ms, before_ms, f"since_date={args.since_date or f'last-{args.days}-days'} before_date={args.before_date or '-'}"
    return since_ms, before_ms, f"days={args.days}"


def parse_ts_ms(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        if "value" in value:
            return parse_ts_ms(value.get("value"))
        return 0
    if isinstance(value, list):
        for item in value:
            parsed = parse_ts_ms(item)
            if parsed:
                return parsed
        return 0
    text = str(value).strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def record_first_seen_ms(fields: Dict[str, Any], spec: TableSpec) -> int:
    return parse_ts_ms(fields.get(spec.published_field)) or parse_ts_ms(fields.get(spec.created_field))


def has_keyword_records(fields: Dict[str, Any], field_name: str) -> bool:
    return bool(keyword_record_ids_from_fields(fields, field_name))


def keyword_record_ids_from_fields(fields: Dict[str, Any], field_name: str) -> List[str]:
    raw = fields.get(field_name)
    ids: List[str] = []
    if isinstance(raw, dict) and isinstance(raw.get("link_record_ids"), list):
        ids = [str(item or "").strip() for item in raw.get("link_record_ids") or []]
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                value = item.get("record_id") or item.get("id") or item.get("text")
            else:
                value = item
            clean = str(value or "").strip()
            if clean:
                ids.append(clean)
    return rss_ingest.keyword_link_values(ids)


def has_keyword_names(fields: Dict[str, Any], field_name: str) -> bool:
    raw = fields.get(field_name)
    if raw is None:
        return False
    if isinstance(raw, list):
        return len(raw) > 0
    text = rss_ingest.clean_feishu_value(raw).strip()
    return bool(text and text != "[]")


def keyword_names_from_fields(fields: Dict[str, Any], field_name: str) -> List[str]:
    raw = fields.get(field_name)
    if raw is None:
        return []
    if isinstance(raw, list):
        out: List[str] = []
        for item in raw:
            if isinstance(item, dict):
                value = item.get("text") or item.get("name") or item.get("value")
            else:
                value = item
            clean = str(value or "").strip()
            if clean:
                out.append(clean)
        return out
    text = rss_ingest.clean_feishu_value(raw).strip()
    if not text or text == "[]":
        return []
    return rss_ingest.split_keyword_text(text)


def load_record_id_filter(path_text: str) -> Dict[str, Set[str]]:
    path_text = str(path_text or "").strip()
    if not path_text:
        return {}
    path = Path(path_text)
    data = json.loads(path.read_text(encoding="utf-8"))
    out: Dict[str, Set[str]] = {}
    if isinstance(data, list):
        out["*"] = {str(item or "").strip() for item in data if str(item or "").strip()}
    elif isinstance(data, dict):
        for table_name, raw_ids in data.items():
            if isinstance(raw_ids, list):
                ids = {str(item or "").strip() for item in raw_ids if str(item or "").strip()}
            else:
                clean = str(raw_ids or "").strip()
                ids = {clean} if clean else set()
            if ids:
                out[str(table_name or "").strip()] = ids
    else:
        raise ValueError("--record-ids must contain a JSON list or object")
    return out


def record_allowed_by_filter(record: Dict[str, Any], spec: TableSpec, record_id_filter: Dict[str, Set[str]]) -> bool:
    if not record_id_filter:
        return True
    record_id = str(record.get("record_id") or "").strip()
    allowed = set(record_id_filter.get("*") or set())
    allowed.update(record_id_filter.get(spec.name) or set())
    allowed.update(record_id_filter.get(spec.table_id) or set())
    return record_id in allowed


def allowed_record_ids_for_spec(spec: TableSpec, record_id_filter: Dict[str, Set[str]]) -> Set[str]:
    if not record_id_filter:
        return set()
    allowed = set(record_id_filter.get("*") or set())
    allowed.update(record_id_filter.get(spec.name) or set())
    allowed.update(record_id_filter.get(spec.table_id) or set())
    return {item for item in allowed if item}


def load_state(path_text: str) -> Dict[str, Set[str]]:
    path_text = str(path_text or "").strip()
    if not path_text:
        return {}
    path = Path(path_text)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    processed = data.get("processed") if isinstance(data, dict) else {}
    if not isinstance(processed, dict):
        return {}
    out: Dict[str, Set[str]] = {}
    for table_name, raw_ids in processed.items():
        if not isinstance(raw_ids, list):
            continue
        ids = {str(item or "").strip() for item in raw_ids if str(item or "").strip()}
        if ids:
            out[str(table_name or "").strip()] = ids
    return out


def save_state(path_text: str, state: Dict[str, Set[str]]) -> None:
    path_text = str(path_text or "").strip()
    if not path_text:
        return
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"processed": {name: sorted(ids) for name, ids in sorted(state.items())}}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def filter_candidates_for_record_ids_and_state(
    candidates: List[Dict[str, Any]],
    spec: TableSpec,
    record_id_filter: Dict[str, Set[str]],
    state: Dict[str, Set[str]],
    stats: BackfillStats,
) -> List[Dict[str, Any]]:
    done = state.get(spec.name) or set()
    out: List[Dict[str, Any]] = []
    for record in candidates:
        record_id = str(record.get("record_id") or "").strip()
        if not record_allowed_by_filter(record, spec, record_id_filter):
            stats.skipped += 1
            continue
        if record_id in done:
            stats.skipped += 1
            continue
        out.append(record)
    return out


def mark_state_success(state: Dict[str, Set[str]], successes: Sequence[Tuple[str, str]]) -> None:
    for table_name, record_id in successes:
        table_name = str(table_name or "").strip()
        record_id = str(record_id or "").strip()
        if table_name and record_id:
            state.setdefault(table_name, set()).add(record_id)


def build_time_filter(field_name: str, since_ms: int, before_ms: Optional[int] = None) -> Dict[str, Any]:
    conditions = [
        {
            "field_name": field_name,
            "operator": "isGreater",
            "value": ["ExactDate", str(since_ms)],
        }
    ]
    if before_ms is not None:
        conditions.append(
            {
                "field_name": field_name,
                "operator": "isLess",
                "value": ["ExactDate", str(before_ms)],
            }
        )
    return {
        "conjunction": "and",
        "conditions": conditions,
    }


def build_recent_filter(spec: TableSpec, since_ms: int, before_ms: Optional[int] = None) -> Dict[str, Any]:
    return build_time_filter(spec.published_field, since_ms, before_ms)


def build_recent_empty_filter(spec: TableSpec, since_ms: int) -> Dict[str, Any]:
    return build_recent_filter(spec, since_ms)


def fetch_records_by_time_field(
    spec: TableSpec,
    tenant_token: str,
    field_name: str,
    since_ms: int,
    before_ms: Optional[int],
    page_size: int,
    max_pages: int,
) -> List[Dict[str, Any]]:
    return list_bitable_records(
        config.FEISHU_APP_TOKEN,
        spec.table_id,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=page_size,
        max_pages=max_pages,
        filter_obj=build_time_filter(field_name, since_ms, before_ms),
        sort=[{"field_name": field_name, "desc": True}],
    )


def fetch_window_records(
    spec: TableSpec,
    tenant_token: str,
    since_ms: int,
    before_ms: Optional[int],
    limit: int,
    page_size: int,
    max_pages: int,
) -> List[Dict[str, Any]]:
    records_by_id: Dict[str, Dict[str, Any]] = {}
    for field_name in dict.fromkeys([spec.published_field, spec.created_field]):
        if not field_name:
            continue
        for record in fetch_records_by_time_field(
            spec,
            tenant_token,
            field_name,
            since_ms,
            before_ms,
            page_size,
            max_pages,
        ):
            record_id = str(record.get("record_id") or "").strip()
            if record_id and record_id not in records_by_id:
                records_by_id[record_id] = record

    def sort_key(record: Dict[str, Any]) -> int:
        return record_first_seen_ms(record.get("fields") or {}, spec)

    return sorted(records_by_id.values(), key=sort_key, reverse=True)[:limit]


def fetch_candidates(
    spec: TableSpec,
    tenant_token: str,
    since_ms: int,
    before_ms: Optional[int],
    limit: int,
    page_size: int,
    max_pages: int,
    include_existing: bool = False,
) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    records = fetch_window_records(spec, tenant_token, since_ms, before_ms, limit, page_size, max_pages)

    out: List[Dict[str, Any]] = []
    for record in records:
        fields = record.get("fields") or {}
        if not include_existing and has_keyword_records(fields, spec.keyword_records_field):
            continue
        published_ms = record_first_seen_ms(fields, spec)
        if not published_ms or published_ms < since_ms:
            continue
        if before_ms is not None and published_ms >= before_ms:
            continue
        out.append(record)
        if len(out) >= limit:
            break
    return out


def get_bitable_record(table_id: str, record_id: str, tenant_token: str) -> Dict[str, Any]:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{config.FEISHU_APP_TOKEN}/tables/{table_id}/records/{record_id}"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    resp = http_get(url, headers, config.HTTP_TIMEOUT, config.HTTP_RETRIES)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"[Feishu] get record error: {data}")
    return (data.get("data") or {}).get("record") or {}


def fetch_record_id_candidates(
    spec: TableSpec,
    tenant_token: str,
    record_id_filter: Dict[str, Set[str]],
    include_existing: bool,
    limit: int,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for record_id in sorted(allowed_record_ids_for_spec(spec, record_id_filter)):
        if len(out) >= limit:
            break
        record = get_bitable_record(spec.table_id, record_id, tenant_token)
        if not record:
            continue
        fields = record.get("fields") or {}
        if not include_existing and has_keyword_records(fields, spec.keyword_records_field):
            continue
        out.append(record)
    return out


def title_and_link(raw_title: Any) -> Tuple[str, str]:
    title = rss_ingest.clean_feishu_value(raw_title).strip()
    link = ""
    if isinstance(raw_title, dict):
        link = str(raw_title.get("link") or "").strip()
    elif isinstance(raw_title, list):
        for item in raw_title:
            if isinstance(item, dict) and item.get("link"):
                link = str(item.get("link") or "").strip()
                break
    return title, link


def article_from_record(record: Dict[str, Any], spec: TableSpec) -> Dict[str, Any]:
    fields = record.get("fields") or {}
    title, link = title_and_link(fields.get(spec.title_field))
    summary = rss_ingest.clean_feishu_value(fields.get(spec.summary_field)).strip()
    full_content = rss_ingest.clean_feishu_value(fields.get(spec.full_content_field)).strip()
    content_parts = []
    if summary:
        content_parts.append(f"summary:\n{summary}")
    if full_content:
        content_parts.append(f"content:\n{full_content}")
    published_ms = record_first_seen_ms(fields, spec)
    return {
        "title": title or "(untitled)",
        "content": "\n\n".join(content_parts) or title,
        "link": link,
        "published": published_ms / 1000 if published_ms else 0,
        "source": spec.name,
    }


def load_screen_prompt() -> str:
    prompt_config = rss_ingest.load_local_prompt_sections()
    rss_ingest._KEYWORD_NAME_BLOCKLIST = prompt_config.get("keyword_name_blocklist") or set()
    screen_prompt = str(prompt_config.get("screen_prompt") or "").strip()
    if not screen_prompt:
        raise RuntimeError("screen prompt is empty")
    return screen_prompt


def prefetch_keyword_index_full(
    tenant_token: str,
    max_pages: int,
) -> Dict[str, rss_ingest.KeywordRecord]:
    table_id = rss_ingest.clean_feishu_value(getattr(config, "FEISHU_KEYWORD_TABLE_ID", "")).strip()
    if not table_id:
        return {}
    records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        table_id,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=500,
        max_pages=max(1, max_pages),
    )
    index: Dict[str, rss_ingest.KeywordRecord] = {}
    for record in records:
        record_id = rss_ingest.clean_feishu_value(record.get("record_id")).strip()
        if not record_id:
            continue
        fields = record.get("fields") or {}
        note = rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_NOTE)).strip()
        if rss_ingest.is_merged_keyword_note(note):
            continue
        canonical = rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_CANONICAL_NAME)).strip()
        if not canonical or rss_ingest._is_keyword_name_blocked(canonical):
            continue
        aliases_text = rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_ALIASES))
        aliases = [canonical]
        aliases.extend(line.strip() for line in aliases_text.splitlines() if line.strip())
        keyword_record = rss_ingest.keyword_record_from_fields(record_id, fields)
        for alias in aliases:
            if rss_ingest._is_keyword_name_blocked(alias):
                continue
            for key in rss_ingest.keyword_alias_index_keys(alias):
                rss_ingest.put_keyword_index_record(index, key, keyword_record)
    print(f"[Keyword] indexed={len({item.record_id for item in index.values()})}", flush=True)
    return index


def prefetch_keyword_record_names_full(
    tenant_token: str,
    max_pages: int,
) -> Dict[str, str]:
    table_id = rss_ingest.clean_feishu_value(getattr(config, "FEISHU_KEYWORD_TABLE_ID", "")).strip()
    if not table_id:
        return {}
    records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        table_id,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=500,
        max_pages=max(1, max_pages),
    )
    out: Dict[str, str] = {}
    for record in records:
        record_id = rss_ingest.clean_feishu_value(record.get("record_id")).strip()
        fields = record.get("fields") or {}
        canonical = rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_CANONICAL_NAME)).strip()
        if record_id and canonical:
            out[record_id] = canonical
    print(f"[Keyword] record_names={len(out)}", flush=True)
    return out


def prefetch_keyword_records_by_id_full(
    tenant_token: str,
    max_pages: int,
) -> Dict[str, rss_ingest.KeywordRecord]:
    table_id = rss_ingest.clean_feishu_value(getattr(config, "FEISHU_KEYWORD_TABLE_ID", "")).strip()
    if not table_id:
        return {}
    records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        table_id,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=500,
        max_pages=max(1, max_pages),
    )
    out: Dict[str, rss_ingest.KeywordRecord] = {}
    for record in records:
        record_id = rss_ingest.clean_feishu_value(record.get("record_id")).strip()
        fields = record.get("fields") or {}
        if not record_id:
            continue
        note = rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_NOTE)).strip()
        if rss_ingest.is_merged_keyword_note(note):
            continue
        out[record_id] = rss_ingest.keyword_record_from_fields(record_id, fields)
    print(f"[Keyword] records_by_id={len(out)}", flush=True)
    return out


def provider_and_model(provider_arg: str, model_arg: str) -> Tuple[str, str]:
    provider = rss_ingest.normalize_provider_name(provider_arg or config.LLM_PROVIDER)
    model = str(model_arg or "").strip() or rss_ingest.provider_model_for_stage(provider, "screen")
    return provider, model


def keywords_for_analysis(analysis: Dict[str, Any]) -> Tuple[List[str], List[Dict[str, Any]]]:
    names = rss_ingest.keyword_names_from_analysis(analysis)
    allowed_names = set(names)
    keywords = [
        dict(kw)
        for kw in analysis.get("keywords") or []
        if isinstance(kw, dict) and str(kw.get("name") or "").strip() in allowed_names
    ]
    return names, keywords


def validate_or_repair_screen_result(raw_analysis: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return rss_ingest.validate_screen_result(raw_analysis)
    except ValueError:
        pass

    keywords: List[Dict[str, str]] = []
    for kw in raw_analysis.get("keywords") or []:
        if not isinstance(kw, dict):
            continue
        name = str(kw.get("name") or "").strip()
        type_ = str(kw.get("type") or "").strip().lower()
        if not name or len(name) > 20:
            continue
        if type_ not in config.KEYWORD_TYPE_OPTIONS:
            continue
        keywords.append({"name": name, "type": type_})
        if len(keywords) >= 3:
            break
    if not keywords:
        return {
            "action": str(raw_analysis.get("action") or "pass").strip().lower() or "pass",
            "reason": str(raw_analysis.get("reason") or "keyword backfill found no keywords").strip(),
            "keywords": [],
        }
    return {
        "action": str(raw_analysis.get("action") or "pass").strip().lower() or "pass",
        "reason": str(raw_analysis.get("reason") or "keyword backfill repaired schema").strip(),
        "keywords": keywords,
    }


def analyze_record(
    record: Dict[str, Any],
    spec: TableSpec,
    screen_prompt: str,
    provider: str,
    model: str,
    schema_retries: int = DEFAULT_SCHEMA_RETRIES,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    article = article_from_record(record, spec)
    attempts = max(1, schema_retries)
    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            attempt_prompt = screen_prompt if attempt == 0 else f"{screen_prompt}{RETRY_SCHEMA_HINT}"
            raw_analysis = rss_ingest.analyze_with_provider_prompt(
                article,
                provider,
                attempt_prompt,
                model,
            )
            analysis = validate_or_repair_screen_result(raw_analysis)
            return keywords_for_analysis(analysis)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"screen result invalid after {attempts} attempt(s): {last_error}") from last_error


def update_record_keyword_links(
    spec: TableSpec,
    tenant_token: str,
    record_id: str,
    keyword_record_ids: List[str],
    keyword_names: List[str],
) -> bool:
    if not keyword_record_ids and not keyword_names:
        return False
    fields: Dict[str, Any] = {}
    if keyword_record_ids:
        fields[spec.keyword_records_field] = keyword_record_ids
    if keyword_names:
        fields[spec.keywords_field] = rss_ingest.format_keyword_names_text(keyword_names)
    return update_bitable_record_fields(
        config.FEISHU_APP_TOKEN,
        spec.table_id,
        tenant_token,
        record_id,
        fields,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )


def required_config_errors(dry_run: bool) -> List[str]:
    required = ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_APP_TOKEN", "FEISHU_NEWS_TABLE_ID"]
    if not dry_run:
        required.append("FEISHU_KEYWORD_TABLE_ID")
    return [name for name in required if not str(getattr(config, name, "") or "").strip()]


def process_record(
    record: Dict[str, Any],
    spec: TableSpec,
    tenant_token: str,
    screen_prompt: str,
    provider: str,
    model: str,
    dry_run: bool,
    schema_retries: int,
    keyword_index: Dict[str, rss_ingest.KeywordRecord],
    keyword_lock: threading.Lock,
) -> bool:
    record_id = str(record.get("record_id") or "").strip()
    title, _ = title_and_link((record.get("fields") or {}).get(spec.title_field))
    print(f"[{spec.name}] {record_id} analyzing title={title[:80]}", flush=True)
    names, keywords = analyze_record(record, spec, screen_prompt, provider, model, schema_retries=schema_retries)
    print(f"[{spec.name}] {record_id} keywords={', '.join(names) or '-'}", flush=True)
    if dry_run:
        return True
    if not keywords:
        return False
    keyword_record_ids = rss_ingest.ensure_keyword_records(
        keywords,
        tenant_token,
        keyword_index,
        keyword_lock,
        first_seen_ms=record_first_seen_ms(record.get("fields") or {}, spec),
    )
    if not keyword_record_ids:
        return False
    return update_record_keyword_links(spec, tenant_token, record_id, keyword_record_ids, names)


def analyze_candidate(
    record: Dict[str, Any],
    spec: TableSpec,
    screen_prompt: str,
    provider: str,
    model: str,
    schema_retries: int,
) -> AnalyzeResult:
    record_id = str(record.get("record_id") or "").strip()
    title, _ = title_and_link((record.get("fields") or {}).get(spec.title_field))
    print(f"[{spec.name}] {record_id} analyzing title={title[:80]}", flush=True)
    names, keywords = analyze_record(
        record,
        spec,
        screen_prompt,
        provider,
        model,
        schema_retries=schema_retries,
    )
    print(f"[{spec.name}] {record_id} keywords={', '.join(names) or '-'}", flush=True)
    return AnalyzeResult(
        spec=spec,
        record=record,
        record_id=record_id,
        title=title,
        names=names,
        keywords=keywords,
    )


def write_analyze_result(
    result: AnalyzeResult,
    tenant_token: str,
    dry_run: bool,
    keyword_index: Dict[str, rss_ingest.KeywordRecord],
    keyword_lock: threading.Lock,
) -> bool:
    if dry_run:
        return True
    if not result.keywords:
        return False
    keyword_record_ids = rss_ingest.ensure_keyword_records(
        result.keywords,
        tenant_token,
        keyword_index,
        keyword_lock,
        first_seen_ms=record_first_seen_ms(result.record.get("fields") or {}, result.spec),
    )
    if not keyword_record_ids:
        return False
    return update_record_keyword_links(result.spec, tenant_token, result.record_id, keyword_record_ids, result.names)


def keyword_ids_for_result(
    result: AnalyzeResult,
    tenant_token: str,
    keyword_index: Dict[str, rss_ingest.KeywordRecord],
    keyword_lock: threading.Lock,
) -> List[str]:
    if not result.keywords:
        return []
    return rss_ingest.ensure_keyword_records(
        result.keywords,
        tenant_token,
        keyword_index,
        keyword_lock,
        first_seen_ms=record_first_seen_ms(result.record.get("fields") or {}, result.spec),
    )


def keyword_record_id_to_name(keyword_index: Dict[str, rss_ingest.KeywordRecord]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for record in keyword_index.values():
        if record.record_id and record.canonical_name:
            out[record.record_id] = record.canonical_name
    return out


def fetch_keyword_name_sync_candidates(
    spec: TableSpec,
    tenant_token: str,
    since_ms: int,
    before_ms: Optional[int],
    limit: int,
    page_size: int,
    max_pages: int,
) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    records = fetch_window_records(spec, tenant_token, since_ms, before_ms, limit, page_size, max_pages)

    out: List[Dict[str, Any]] = []
    for record in records:
        fields = record.get("fields") or {}
        if not has_keyword_records(fields, spec.keyword_records_field):
            continue
        published_ms = record_first_seen_ms(fields, spec)
        if not published_ms or published_ms < since_ms:
            continue
        if before_ms is not None and published_ms >= before_ms:
            continue
        out.append(record)
        if len(out) >= limit:
            break
    return out


def process_keyword_name_sync(
    candidates: List[Dict[str, Any]],
    spec: TableSpec,
    tenant_token: str,
    keyword_records_by_id: Dict[str, rss_ingest.KeywordRecord],
    stats: BackfillStats,
    success_callback: Optional[Callable[[List[Tuple[str, str]]], None]] = None,
) -> None:
    pending_updates: List[PendingUpdate] = []
    for record in candidates:
        record_id = str(record.get("record_id") or "").strip()
        fields = record.get("fields") or {}
        link_ids = keyword_record_ids_from_fields(fields, spec.keyword_records_field)
        original_ids = rss_ingest.original_keyword_record_ids_from_expanded(link_ids, keyword_records_by_id)
        names = [
            keyword_records_by_id[item].canonical_name
            for item in original_ids
            if item in keyword_records_by_id
        ]
        names = list(dict.fromkeys(name for name in names if name))
        stats.processed += 1
        if not names:
            stats.skipped += 1
            continue
        current_names = keyword_names_from_fields(fields, spec.keywords_field)
        if current_names == names:
            stats.skipped += 1
            continue
        pending_updates.append(
            PendingUpdate(
                spec=spec,
                record_id=record_id,
                keyword_record_ids=[],
                keyword_names=names,
            )
        )
        if len(pending_updates) >= BATCH_UPDATE_SIZE:
            successful = flush_updates(pending_updates, tenant_token, stats)
            if success_callback and successful:
                success_callback(successful)
            pending_updates.clear()
    if pending_updates:
        successful = flush_updates(pending_updates, tenant_token, stats)
        if success_callback and successful:
            success_callback(successful)


def flush_updates(
    updates: List[PendingUpdate],
    tenant_token: str,
    stats: BackfillStats,
) -> List[Tuple[str, str]]:
    successful: List[Tuple[str, str]] = []
    grouped: Dict[str, List[PendingUpdate]] = {}
    specs_by_table: Dict[str, TableSpec] = {}
    for update in updates:
        grouped.setdefault(update.spec.table_id, []).append(update)
        specs_by_table[update.spec.table_id] = update.spec

    for table_id, table_updates in grouped.items():
        spec = specs_by_table[table_id]
        for start in range(0, len(table_updates), BATCH_UPDATE_SIZE):
            chunk = table_updates[start:start + BATCH_UPDATE_SIZE]
            payload = [
                {
                    "record_id": item.record_id,
                    "fields": {
                        **(
                            {spec.keyword_records_field: item.keyword_record_ids}
                            if item.overwrite_empty or item.keyword_record_ids
                            else {}
                        ),
                        **(
                            {spec.keywords_field: rss_ingest.format_keyword_names_text(item.keyword_names)}
                            if item.overwrite_empty or item.keyword_names
                            else {}
                        ),
                    },
                }
                for item in chunk
            ]
            ok, data = batch_update_bitable_records(
                config.FEISHU_APP_TOKEN,
                table_id,
                tenant_token,
                payload,
                config.HTTP_TIMEOUT,
                config.HTTP_RETRIES,
            )
            if ok:
                stats.updated += len(chunk)
                successful.extend((item.spec.name, item.record_id) for item in chunk)
                print(f"[{spec.name}] batch_updated={len(chunk)}", flush=True)
            else:
                stats.failed += len(chunk)
                print(f"[{spec.name}] batch update failed: {data}", file=sys.stderr, flush=True)
            time.sleep(WRITE_DELAY_SECONDS)
    return successful


def process_candidates(
    candidates: List[Dict[str, Any]],
    spec: TableSpec,
    tenant_token: str,
    screen_prompt: str,
    provider: str,
    model: str,
    dry_run: bool,
    schema_retries: int,
    llm_concurrency: int,
    keyword_index: Dict[str, rss_ingest.KeywordRecord],
    keyword_lock: threading.Lock,
    stats: BackfillStats,
    rebuild_existing: bool = False,
    clear_when_empty: bool = False,
    success_callback: Optional[Callable[[List[Tuple[str, str]]], None]] = None,
) -> None:
    workers = max(1, llm_concurrency)
    pending_updates: List[PendingUpdate] = []
    print(f"[{spec.name}] llm_concurrency={workers}", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                analyze_candidate,
                record,
                spec,
                screen_prompt,
                provider,
                model,
                schema_retries,
            )
            for record in candidates
        ]
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                stats.failed += 1
                print(f"[{spec.name}] failed: {exc}", file=sys.stderr, flush=True)
            else:
                stats.processed += 1
                if dry_run:
                    continue
                if not result.keywords:
                    if rebuild_existing and clear_when_empty:
                        pending_updates.append(
                            PendingUpdate(
                                spec=result.spec,
                                record_id=result.record_id,
                                keyword_record_ids=[],
                                keyword_names=[],
                                overwrite_empty=True,
                            )
                        )
                    else:
                        stats.skipped += 1
                    continue

                keyword_record_ids = keyword_ids_for_result(result, tenant_token, keyword_index, keyword_lock)
                if keyword_record_ids:
                    pending_updates.append(
                        PendingUpdate(
                            spec=result.spec,
                            record_id=result.record_id,
                            keyword_record_ids=keyword_record_ids,
                            keyword_names=result.names,
                            overwrite_empty=rebuild_existing,
                        )
                    )
                    if len(pending_updates) >= BATCH_UPDATE_SIZE:
                        successful = flush_updates(pending_updates, tenant_token, stats)
                        if success_callback and successful:
                            success_callback(successful)
                        pending_updates.clear()
                else:
                    stats.failed += 1
    if pending_updates:
        successful = flush_updates(pending_updates, tenant_token, stats)
        if success_callback and successful:
            success_callback(successful)


def run(args: argparse.Namespace) -> int:
    if args.max_records <= 0:
        print("[Backfill] max-records must be positive", file=sys.stderr, flush=True)
        return 2

    missing = required_config_errors(args.dry_run)
    if missing:
        print(f"[Config] missing: {', '.join(missing)}", file=sys.stderr, flush=True)
        return 2

    try:
        since_ms, before_ms, window_label = resolve_window(args)
        record_id_filter = load_record_id_filter(args.record_ids)
        state = load_state(args.state_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[Backfill] {exc}", file=sys.stderr, flush=True)
        return 2
    provider, model = provider_and_model(args.provider, args.model)
    mode_base = "sync-keyword-names" if args.sync_keyword_names else ("dry-run" if args.dry_run else "apply")
    mode = f"{mode_base}+rebuild-existing" if args.rebuild_existing and not args.sync_keyword_names else mode_base
    print(
        f"[Backfill] mode={mode} {window_label} max_records={args.max_records} "
        f"llm_concurrency={max(1, args.llm_concurrency)} provider={provider} model={model}",
        flush=True,
    )
    if args.dry_run and not args.sync_keyword_names:
        print("[Backfill] dry-run: Feishu keyword records and article links will not be changed.", flush=True)

    tenant_token = get_tenant_access_token(
        config.FEISHU_APP_ID,
        config.FEISHU_APP_SECRET,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )
    keyword_index: Dict[str, rss_ingest.KeywordRecord] = {}
    screen_prompt = ""
    if not args.sync_keyword_names:
        screen_prompt = load_screen_prompt()
    if not args.dry_run and not args.sync_keyword_names:
        keyword_index = prefetch_keyword_index_full(tenant_token, args.keyword_max_pages)
    keyword_lock = threading.Lock()

    stats = BackfillStats()
    remaining = args.max_records

    def on_success(successes: List[Tuple[str, str]]) -> None:
        if args.dry_run:
            return
        mark_state_success(state, successes)
        save_state(args.state_path, state)

    if args.sync_keyword_names:
        keyword_records_by_id = prefetch_keyword_records_by_id_full(tenant_token, args.keyword_max_pages)
        for spec in table_specs():
            if remaining <= 0:
                break
            candidates = fetch_keyword_name_sync_candidates(
                spec,
                tenant_token,
                since_ms,
                before_ms,
                remaining,
                args.page_size,
                args.max_pages,
            )
            if record_id_filter:
                candidates = fetch_record_id_candidates(
                    spec,
                    tenant_token,
                    record_id_filter,
                    include_existing=True,
                    limit=remaining,
                )
            candidates = filter_candidates_for_record_ids_and_state(
                candidates,
                spec,
                record_id_filter,
                state,
                stats,
            )
            stats.scanned += len(candidates)
            print(f"[{spec.name}] keyword_name_sync_candidates={len(candidates)}", flush=True)
            process_keyword_name_sync(candidates, spec, tenant_token, keyword_records_by_id, stats, success_callback=on_success)
            remaining -= len(candidates)
        print(
            "[Backfill] done "
            f"scanned={stats.scanned} processed={stats.processed} "
            f"updated={stats.updated} skipped={stats.skipped} failed={stats.failed}",
            flush=True,
        )
        return 1 if stats.failed else 0

    for spec in table_specs():
        if remaining <= 0:
            break
        if record_id_filter:
            candidates = fetch_record_id_candidates(
                spec,
                tenant_token,
                record_id_filter,
                include_existing=args.rebuild_existing,
                limit=remaining,
            )
        else:
            candidates = fetch_candidates(
                spec,
                tenant_token,
                since_ms,
                before_ms,
                remaining,
                args.page_size,
                args.max_pages,
                include_existing=args.rebuild_existing,
            )
        candidates = filter_candidates_for_record_ids_and_state(
            candidates,
            spec,
            record_id_filter,
            state,
            stats,
        )
        stats.scanned += len(candidates)
        print(f"[{spec.name}] candidates={len(candidates)}", flush=True)
        process_candidates(
            candidates,
            spec,
            tenant_token,
            screen_prompt,
            provider,
            model,
            args.dry_run,
            max(1, args.schema_retries),
            max(1, args.llm_concurrency),
            keyword_index,
            keyword_lock,
            stats,
            rebuild_existing=args.rebuild_existing,
            clear_when_empty=args.clear_when_empty,
            success_callback=on_success,
        )
        remaining -= len(candidates)

    print(
        "[Backfill] done "
        f"scanned={stats.scanned} processed={stats.processed} "
        f"updated={stats.updated} skipped={stats.skipped} failed={stats.failed}",
        flush=True,
    )
    return 1 if stats.failed else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
