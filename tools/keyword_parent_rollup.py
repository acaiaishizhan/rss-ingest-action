# -*- coding: utf-8 -*-
"""Maintain KEYWORD parent-child links and rollup heat formulas."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Sequence, Set

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
import rss_ingest
from feishu_client import (
    batch_update_bitable_records,
    create_bitable_field,
    get_tenant_access_token,
    list_bitable_fields,
    list_bitable_records,
    update_bitable_field,
)
from tools.keyword_snapshot import load_snapshot_entries

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PARENT_FIELD = config.KEYWORD_FIELD_PARENT
CHILD_FIELD = "子关键词"
OWNER_FIELD = config.KEYWORD_FIELD_OWNERS
OWNER_CHILD_FIELD = "归属子关键词"
SELF_HEAT_FIELDS = ["自身24h", "自身前24h", "自身7d", "自身前7d", "自身30d", "自身前30d"]
TOTAL_TO_SELF = {
    "24h": "自身24h",
    "前24h": "自身前24h",
    "7d": "自身7d",
    "前7d": "自身前7d",
    "30d": "自身30d",
    "前30d": "自身前30d",
}
NEWS_ONLY_TOTAL_TO_SELF = {
    config.KEYWORD_FIELD_NEWS_24H: "自身NEWS24h",
}
FORMULA_FIELD_TYPE = 20
LINK_FIELD_TYPE = 21
RISKY_PARENT_NAMES = {
    "chat",
    "claw",
    "agent",
    "agentic",
    "arena",
    "assistant",
    "banana",
    "buddy",
    "computer",
    "conductor",
    "enter",
    "flash",
    "flow",
    "fluid",
    "glob",
    "harness",
    "imagine",
    "intel",
    "markdown",
    "monitor",
    "navigator",
    "office",
    "pilot",
    "play",
    "pocket",
    "skills",
    "spark",
    "space",
    "square",
    "thrive",
    "vertex",
    "wire",
    "word",
    "world",
    "worktree",
    "xhigh",
    "model",
    "/model",
    "token",
    "ultra",
    "美国",
    "中国",
    "英国",
    "日本",
    "德国",
    "伊朗",
    "台湾",
    "加州",
    "北京",
    "广州",
    "旧金山",
    "科罗拉多州",
}
PARENT_ALLOWLIST_NAMES = {
    "GPT",
    "Claude",
    "Opus",
    "Sonnet",
    "Gemini",
    "GLM",
    "Kimi",
    "Qwen",
    "Llama",
    "DeepSeek",
    "Sora",
    "Veo",
    "Grok",
    "Copilot",
    "Codex",
    "ChatGPT",
    "Gemma",
    "OpenAI",
    "Anthropic",
    "Google",
    "Microsoft",
    "Meta",
    "Apple",
    "Nvidia",
    "NVIDIA",
    "GitHub",
    "Amazon",
    "AWS",
    "Cloudflare",
    "Vercel",
    "Cursor",
    "阿里",
    "腾讯",
    "字节",
    "百度",
    "华为",
    "小米",
    "智谱",
    "月之暗面",
    "MiniMax",
    "欧盟",
}
LATIN_TOKEN_RE = re.compile(r"[0-9a-z]+", re.IGNORECASE)


@dataclass(frozen=True)
class KeywordEntry:
    record_id: str
    name: str
    type: str
    note: str = ""
    news_count: int = 0
    filtered_count: int = 0


@dataclass(frozen=True)
class ParentPlan:
    child_id: str
    parent_id: str
    child_name: str
    parent_name: str
    type: str


def compact(value: str) -> str:
    return rss_ingest.compact_keyword_alias(value)


def contains_cjk(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", value or ""))


def latin_tokens(value: str) -> List[str]:
    return [match.group(0).lower() for match in LATIN_TOKEN_RE.finditer(value or "")]


def is_confident_parent_match(parent_name: str, child_name: str, allow_short_latin: bool = False) -> bool:
    parent_norm = rss_ingest.normalize_keyword_alias(parent_name)
    child_norm = rss_ingest.normalize_keyword_alias(child_name)
    parent_compact = compact(parent_name)
    child_compact = compact(child_name)
    if not parent_compact or not child_compact or child_compact == parent_compact:
        return False
    if parent_compact not in child_compact:
        return False
    if contains_cjk(parent_norm):
        return len(parent_compact) >= 2
    parent_parts = latin_tokens(parent_norm)
    child_parts = latin_tokens(child_norm)
    if not parent_parts or len(parent_compact) < 5:
        if allow_short_latin and len(parent_compact) >= 3:
            return any(
                part == parent_compact
                or (part.startswith(parent_compact) and len(part) > len(parent_compact) and part[len(parent_compact)].isdigit())
                for part in child_parts
            )
        return False
    width = len(parent_parts)
    return any(child_parts[index : index + width] == parent_parts for index in range(0, len(child_parts) - width + 1))


def normalized_generic_names(generic_names: Set[str]) -> Set[str]:
    names = {rss_ingest.normalize_keyword_alias(item) for item in generic_names if item}
    names.update(RISKY_PARENT_NAMES)
    return names


def normalized_parent_allowlist() -> Set[str]:
    names = {rss_ingest.normalize_keyword_alias(item) for item in PARENT_ALLOWLIST_NAMES}
    names.update(rss_ingest.compact_keyword_alias(item) for item in PARENT_ALLOWLIST_NAMES)
    return {item for item in names if item}


def keyword_heat(entry: KeywordEntry) -> int:
    return max(0, int(entry.news_count or 0)) + max(0, int(entry.filtered_count or 0))


def is_short_latin_parent(entry: KeywordEntry) -> bool:
    return not contains_cjk(entry.name) and len(compact(entry.name)) < 5


def parent_sort_key(entry: KeywordEntry) -> tuple[int, int, int, str]:
    return (
        1 if is_short_latin_parent(entry) else 0,
        -keyword_heat(entry),
        len(compact(entry.name)),
        entry.name.lower(),
    )


def parent_type_allowed(parent: KeywordEntry, child: KeywordEntry) -> bool:
    if child.type not in {"model", "product", "org", "technology", "policy"}:
        return False
    if parent.type == child.type:
        return True
    if child.type == "model" and parent.type in {"product", "org"}:
        return True
    if child.type == "product" and parent.type in {"product", "org"}:
        return True
    if child.type == "technology" and parent.type in {"technology", "product", "org"}:
        return True
    if child.type == "policy" and parent.type in {"policy", "org"}:
        return True
    return False


def parent_child_blocked(parent: KeywordEntry, child: KeywordEntry) -> bool:
    parent_compact = compact(parent.name)
    child_compact = compact(child.name)
    if contains_cjk(parent.name) and parent.type == "org" and len(parent_compact) < 4:
        if not child_compact.startswith(parent_compact):
            return True
        if child.type == "technology":
            return True
    if parent_compact == "欧盟" and child.type == "org" and "银行" in child_compact:
        return True
    return False


def min_parent_compact_length(parent: KeywordEntry) -> int:
    if contains_cjk(parent.name) and parent.type == "org":
        return 2
    if contains_cjk(parent.name):
        return 4
    return 4


def parse_int(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        if isinstance(value.get("value"), list) and value.get("value"):
            return parse_int(value.get("value")[0])
        return parse_int(value.get("value"))
    if isinstance(value, list) and value:
        return parse_int(value[0])
    text = rss_ingest.clean_feishu_value(value).strip().replace(",", "")
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def build_parent_plans(entries: List[KeywordEntry], generic_names: Set[str]) -> Dict[str, ParentPlan]:
    generic = normalized_generic_names(generic_names)
    allowlist = normalized_parent_allowlist()
    parents_by_compact: Dict[str, List[KeywordEntry]] = {}
    for parent in entries:
        if not parent.record_id:
            continue
        parent_norm = rss_ingest.normalize_keyword_alias(parent.name)
        parent_compact = compact(parent.name)
        if not parent_compact:
            continue
        if parent_norm in generic or parent_compact in generic:
            continue
        if parent_norm not in allowlist and parent_compact not in allowlist:
            continue
        parent_is_allowlisted = parent_norm in allowlist or parent_compact in allowlist
        if len(parent_compact) < min_parent_compact_length(parent) and not parent_is_allowlisted:
            continue
        parents_by_compact.setdefault(parent_compact, []).append(parent)

    plans: Dict[str, ParentPlan] = {}
    for child in entries:
        child_compact = compact(child.name)
        if not child.record_id or not child_compact:
            continue
        child_parent_keys = {
            child_compact[start:end]
            for start in range(len(child_compact))
            for end in range(start + 2, len(child_compact) + 1)
        }
        candidates: List[KeywordEntry] = []
        for parent_compact in child_parent_keys:
            for parent in parents_by_compact.get(parent_compact, []):
                if parent.record_id == child.record_id or not parent_type_allowed(parent, child):
                    continue
                if len(child_compact) <= len(parent_compact):
                    continue
                if parent_child_blocked(parent, child):
                    continue
                parent_heat = keyword_heat(parent)
                child_heat = keyword_heat(child)
                parent_is_allowlisted = parent_compact in allowlist or rss_ingest.normalize_keyword_alias(parent.name) in allowlist
                if not parent_is_allowlisted and (parent_heat or child_heat):
                    if parent_heat < child_heat:
                        continue
                if is_confident_parent_match(parent.name, child.name, allow_short_latin=parent_is_allowlisted):
                    candidates.append(parent)
        if candidates:
            parent = sorted(candidates, key=parent_sort_key)[0]
            plans[child.record_id] = ParentPlan(
                child_id=child.record_id,
                parent_id=parent.record_id,
                child_name=child.name,
                parent_name=parent.name,
                type=child.type,
            )
    return plans


def build_parent_plans_slow(entries: List[KeywordEntry], generic_names: Set[str]) -> Dict[str, ParentPlan]:
    generic = normalized_generic_names(generic_names)
    allowlist = normalized_parent_allowlist()
    plans: Dict[str, ParentPlan] = {}
    for child in entries:
        child_compact = compact(child.name)
        if not child.record_id or not child_compact:
            continue
        candidates: List[KeywordEntry] = []
        for parent in entries:
            if parent.record_id == child.record_id or not parent_type_allowed(parent, child):
                continue
            parent_norm = rss_ingest.normalize_keyword_alias(parent.name)
            parent_compact = compact(parent.name)
            if parent_norm in generic or parent_compact in generic:
                continue
            if parent_norm not in allowlist and parent_compact not in allowlist:
                continue
            parent_is_allowlisted = parent_compact in allowlist or parent_norm in allowlist
            if (len(parent_compact) < min_parent_compact_length(parent) and not parent_is_allowlisted) or len(child_compact) <= len(parent_compact):
                continue
            if parent_child_blocked(parent, child):
                continue
            parent_heat = keyword_heat(parent)
            child_heat = keyword_heat(child)
            if not parent_is_allowlisted and (parent_heat or child_heat):
                if parent_heat < child_heat:
                    continue
            if is_confident_parent_match(parent.name, child.name, allow_short_latin=parent_is_allowlisted):
                candidates.append(parent)
        if candidates:
            parent = sorted(candidates, key=parent_sort_key)[0]
            plans[child.record_id] = ParentPlan(
                child_id=child.record_id,
                parent_id=parent.record_id,
                child_name=child.name,
                parent_name=parent.name,
                type=child.type,
            )
    return plans


def formula_property(expression: str) -> Dict[str, Any]:
    return {
        "formatter": "0",
        "formula_expression": expression,
        "type": {"data_type": 2, "ui_property": {"formatter": "0"}, "ui_type": "Number"},
    }


def formula_expression(field: Dict[str, Any]) -> str:
    return str(((field.get("property") or {}).get("formula_expression")) or "")


def same_record_field_expression(field_id: str) -> str:
    return f"bitable::$table[{config.FEISHU_KEYWORD_TABLE_ID}].$field[{field_id}]"


def linked_column_sum_expression(link_field_id: str, column_field_id: str) -> str:
    return f"SUM(bitable::$table[{config.FEISHU_KEYWORD_TABLE_ID}].$field[{link_field_id}].$column[{column_field_id}])"


def total_heat_expression(self_field_id: str, child_link_field_id: str) -> str:
    return f"{same_record_field_expression(self_field_id)}+{linked_column_sum_expression(child_link_field_id, self_field_id)}"


def self_heat_formula(field_name: str) -> str:
    return f"SUM(相关新闻.{field_name.replace('自身', '')}) + SUM(相关过滤记录.{field_name.replace('自身', '')})"


def self_news_heat_formula(field_name: str) -> str:
    return f"SUM(相关新闻.{field_name.replace('自身NEWS', '')})"


def total_heat_formula(total_field: str, self_field: str) -> str:
    return f"{self_field} + SUM({CHILD_FIELD}.{self_field})"


def get_field_id(field: Dict[str, Any]) -> str:
    return str(field.get("field_id") or field.get("id") or "")


def self_heat_source_formula(self_name: str, total_name: str, existing: Dict[str, Dict[str, Any]]) -> str:
    source_expression = formula_expression(existing.get(self_name) or existing.get(total_name, {}))
    if source_expression:
        return source_expression
    if self_name in NEWS_ONLY_TOTAL_TO_SELF.values():
        return self_news_heat_formula(self_name)
    return self_heat_formula(self_name)


def load_generic_names() -> Set[str]:
    rss_ingest.load_local_prompt_sections()
    return set(getattr(rss_ingest, "_KEYWORD_NAME_BLOCKLIST", set()) or set())


def fetch_keyword_entries(tenant_token: str, max_pages: int) -> List[KeywordEntry]:
    records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_KEYWORD_TABLE_ID,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=500,
        max_pages=max_pages,
    )
    entries: List[KeywordEntry] = []
    for record in records:
        fields = record.get("fields") or {}
        note = rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_NOTE)).strip()
        if rss_ingest.is_merged_keyword_note(note):
            continue
        entries.append(
            KeywordEntry(
                str(record.get("record_id") or ""),
                rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_CANONICAL_NAME)).strip(),
                rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_TYPE)).strip().lower(),
                note,
                parse_int(fields.get(config.KEYWORD_FIELD_NEWS_COUNT)),
                parse_int(fields.get(config.KEYWORD_FIELD_FILTERED_COUNT)),
            )
        )
    return [entry for entry in entries if entry.record_id and entry.name]


def field_by_name(tenant_token: str) -> Dict[str, Dict[str, Any]]:
    fields = list_bitable_fields(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_KEYWORD_TABLE_ID,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )
    return {str(field.get("field_name") or field.get("name") or ""): field for field in fields}


def ensure_rollup_fields(tenant_token: str, apply: bool) -> Dict[str, Any]:
    existing = field_by_name(tenant_token)
    report = {"present": [], "created": [], "updated": [], "errors": []}

    def ensure_link_field(field_name: str, back_field_name: str, multiple: bool) -> None:
        nonlocal existing
        if field_name in existing:
            report["present"].append(field_name)
            return
        if not apply:
            report["created"].append(field_name)
            return
        ok, payload = create_bitable_field(
            config.FEISHU_APP_TOKEN,
            config.FEISHU_KEYWORD_TABLE_ID,
            tenant_token,
            field_name,
            LINK_FIELD_TYPE,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
            field_property={
                "table_id": config.FEISHU_KEYWORD_TABLE_ID,
                "back_field_name": back_field_name,
                "multiple": multiple,
            },
        )
        (report["created"] if ok else report["errors"]).append(field_name if ok else payload)
        existing = field_by_name(tenant_token)

    ensure_link_field(PARENT_FIELD, CHILD_FIELD, False)
    ensure_link_field(OWNER_FIELD, OWNER_CHILD_FIELD, True)

    total_to_self = {**TOTAL_TO_SELF, **NEWS_ONLY_TOTAL_TO_SELF}

    for self_name, total_name in ((self_name, total_name) for total_name, self_name in total_to_self.items()):
        field = existing.get(self_name)
        source_expression = self_heat_source_formula(self_name, total_name, existing)
        if not apply:
            report["present" if field else "created"].append(self_name)
            continue
        if not source_expression:
            report["errors"].append({"field": self_name, "error": f"missing source formula expression from {total_name}"})
            continue
        prop = formula_property(source_expression)
        if field:
            field_id = str(field.get("field_id") or field.get("id") or "")
            ok, payload = update_bitable_field(
                config.FEISHU_APP_TOKEN,
                config.FEISHU_KEYWORD_TABLE_ID,
                field_id,
                tenant_token,
                config.HTTP_TIMEOUT,
                config.HTTP_RETRIES,
                field_name=self_name,
                field_type=FORMULA_FIELD_TYPE,
                field_property=prop,
            )
            (report["updated"] if ok else report["errors"]).append(self_name if ok else payload)
        else:
            ok, payload = create_bitable_field(
                config.FEISHU_APP_TOKEN,
                config.FEISHU_KEYWORD_TABLE_ID,
                tenant_token,
                self_name,
                FORMULA_FIELD_TYPE,
                config.HTTP_TIMEOUT,
                config.HTTP_RETRIES,
                field_property=prop,
            )
            (report["created"] if ok else report["errors"]).append(self_name if ok else payload)
    if apply:
        existing = field_by_name(tenant_token)

    for total_name, self_name in total_to_self.items():
        field = existing.get(total_name)
        self_field = existing.get(self_name)
        if not apply:
            report["present" if field else "created"].append(total_name)
            continue
        child_field = existing.get(CHILD_FIELD)
        self_expression = formula_expression(self_field or {})
        self_field_id = get_field_id(self_field or {})
        child_field_id = get_field_id(child_field or {})
        if self_field_id and child_field_id:
            expression = total_heat_expression(self_field_id, child_field_id)
        elif self_expression:
            expression = self_expression
        else:
            report["errors"].append({"field": total_name, "error": "missing total/self formula for heat field"})
            continue
        prop = formula_property(expression)
        if field:
            ok, payload = update_bitable_field(
                config.FEISHU_APP_TOKEN,
                config.FEISHU_KEYWORD_TABLE_ID,
                get_field_id(field),
                tenant_token,
                config.HTTP_TIMEOUT,
                config.HTTP_RETRIES,
                field_name=total_name,
                field_type=FORMULA_FIELD_TYPE,
                field_property=prop,
            )
        else:
            ok, payload = create_bitable_field(
                config.FEISHU_APP_TOKEN,
                config.FEISHU_KEYWORD_TABLE_ID,
                tenant_token,
                total_name,
                FORMULA_FIELD_TYPE,
                config.HTTP_TIMEOUT,
                config.HTTP_RETRIES,
                field_property=prop,
            )
        if ok:
            report["updated" if field else "created"].append(total_name)
        else:
            report["errors"].append(payload)
    return report


def build_report(entries: List[KeywordEntry], plans: Dict[str, ParentPlan]) -> Dict[str, Any]:
    parent_counts: Dict[str, int] = {}
    for plan in plans.values():
        parent_counts[plan.parent_name] = parent_counts.get(plan.parent_name, 0) + 1
    return {
        "keyword_total": len(entries),
        "parent_link_count": len(plans),
        "top_parents": sorted(parent_counts.items(), key=lambda item: item[1], reverse=True)[:50],
        "plans": [plan.__dict__ for plan in plans.values()],
        "risk": {
            "contains_openai_codex": [entry.__dict__ for entry in entries if "openai" in compact(entry.name) and "codex" in compact(entry.name)],
        },
    }


def parse_linked_ids(raw: Any) -> List[str]:
    if isinstance(raw, dict) and isinstance(raw.get("link_record_ids"), list):
        return [str(item or "").strip() for item in raw.get("link_record_ids") or [] if str(item or "").strip()]
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, dict):
                value = item.get("record_id") or item.get("id")
            else:
                value = item
            clean = str(value or "").strip()
            if clean:
                out.append(clean)
        return out
    return []


def fetch_current_parent_links(tenant_token: str, max_pages: int = 300) -> Dict[str, List[str]]:
    records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_KEYWORD_TABLE_ID,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=500,
        max_pages=max_pages,
    )
    out: Dict[str, List[str]] = {}
    for record in records:
        record_id = str(record.get("record_id") or "").strip()
        if not record_id:
            continue
        parent_ids = parse_linked_ids((record.get("fields") or {}).get(PARENT_FIELD))
        if parent_ids:
            out[record_id] = parent_ids
    return out


def keyword_entries_from_snapshot(path: Path) -> List[KeywordEntry]:
    return [
        KeywordEntry(
            record_id=entry.record_id,
            name=entry.canonical_name,
            type=entry.type,
            note=entry.note,
            news_count=entry.news_count,
            filtered_count=entry.filtered_count,
        )
        for entry in load_snapshot_entries(path)
    ]


def apply_parent_plans(tenant_token: str, plans: List[Dict[str, Any]]) -> Dict[str, Any]:
    desired = {
        str(item.get("child_id") or ""): [str(item.get("parent_id") or "")]
        for item in plans
        if item.get("child_id") and item.get("parent_id")
    }
    current = fetch_current_parent_links(tenant_token)
    payload = []
    for child_id, parent_ids in desired.items():
        if current.get(child_id) != parent_ids:
            payload.append({"record_id": child_id, "fields": {PARENT_FIELD: parent_ids}})
    for child_id in sorted(set(current) - set(desired)):
        payload.append({"record_id": child_id, "fields": {PARENT_FIELD: []}})
    report = {"updated": 0, "failed": 0, "failed_records": []}
    for start in range(0, len(payload), 50):
        chunk = payload[start : start + 50]
        ok, _ = batch_update_bitable_records(
            config.FEISHU_APP_TOKEN,
            config.FEISHU_KEYWORD_TABLE_ID,
            tenant_token,
            chunk,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
        )
        if ok:
            report["updated"] += len(chunk)
        else:
            for item in chunk:
                single_ok, _ = batch_update_bitable_records(
                    config.FEISHU_APP_TOKEN,
                    config.FEISHU_KEYWORD_TABLE_ID,
                    tenant_token,
                    [item],
                    config.HTTP_TIMEOUT,
                    config.HTTP_RETRIES,
                )
                if single_ok:
                    report["updated"] += 1
                else:
                    report["failed"] += 1
                    report["failed_records"].append(item.get("record_id"))
    return report


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build/apply KEYWORD parent rollups.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--input", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--max-pages", type=int, default=200)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    tenant_token = get_tenant_access_token(config.FEISHU_APP_ID, config.FEISHU_APP_SECRET, config.HTTP_TIMEOUT, config.HTTP_RETRIES)
    if args.apply and args.input:
        field_result = ensure_rollup_fields(tenant_token, apply=True)
        report = apply_parent_plans(tenant_token, json.loads(Path(args.input).read_text(encoding="utf-8")).get("plans", []))
        report["field_result"] = field_result
    else:
        entries = keyword_entries_from_snapshot(Path(args.input)) if args.input else fetch_keyword_entries(tenant_token, args.max_pages)
        plans = build_parent_plans(entries, load_generic_names())
        report = build_report(entries, plans)
        report["field_result"] = ensure_rollup_fields(tenant_token, apply=bool(args.apply))
        if args.apply:
            report["apply_result"] = apply_parent_plans(tenant_token, report["plans"])
    if args.output:
        write_json(Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
