# -*- coding: utf-8 -*-
import argparse
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from feishu_client import (
    batch_update_bitable_records,
    create_bitable_field,
    get_tenant_access_token,
    list_bitable_fields,
    list_bitable_records,
)
from rss_ingest import _load_keyword_name_blocklist, analyze_with_provider_prompt, clean_feishu_value, provider_model_for_stage


SEPARATOR_RE = re.compile(r"[\s\-_./:：·•]+")
MERGE_REASON = "写法只差大小写、空格、横杠、点号或类似分隔符"
ALIAS_REASON = "命中本地常用译名/别名种子"


@dataclass(frozen=True)
class KeywordEntry:
    record_id: str
    canonical_name: str
    type: str
    aliases: List[str]
    news_count: int = 0
    filtered_count: int = 0
    note: str = ""
    parent_ids: List[str] = field(default_factory=list)
    owner_ids: List[str] = field(default_factory=list)


@dataclass
class KeywordUsage:
    news_count: int = 0
    filtered_count: int = 0
    first_seen_ms: int = 0
    last_seen_ms: int = 0
    samples: List[Tuple[int, str]] = field(default_factory=list)


def compact_keyword_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    return SEPARATOR_RE.sub("", normalized)


def _readability_score(entry: KeywordEntry) -> tuple:
    name = entry.canonical_name
    separator_bonus = 2 if re.search(r"\s", name) else 1 if "-" in name else 0
    uppercase_bonus = sum(1 for ch in name if ch.isupper())
    lower_penalty = 1 if name == name.lower() else 0
    return (separator_bonus, uppercase_bonus, -lower_penalty, len(name), name)


def choose_main(entries: List[KeywordEntry]) -> KeywordEntry:
    return sorted(entries, key=_readability_score, reverse=True)[0]


def build_merge_suggestions(entries: List[KeywordEntry]) -> List[Dict[str, Any]]:
    groups: Dict[tuple, List[KeywordEntry]] = {}
    for entry in entries:
        key = compact_keyword_key(entry.canonical_name)
        if not key:
            continue
        groups.setdefault((entry.type, key), []).append(entry)

    suggestions: List[Dict[str, Any]] = []
    for (type_, _key), group in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1])):
        unique_names = {entry.canonical_name for entry in group}
        if len(unique_names) < 2:
            continue
        main = choose_main(group)
        merge_items = [
            {"name": entry.canonical_name, "record_id": entry.record_id}
            for entry in sorted(group, key=lambda item: item.canonical_name)
            if entry.record_id != main.record_id
        ]
        suggestions.append(
            {
                "main": main.canonical_name,
                "main_record_id": main.record_id,
                "merge_into_main": merge_items,
                "type": type_,
                "reason": MERGE_REASON,
            }
        )
    return suggestions


def parse_ts_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None
    if isinstance(value, dict) and "value" in value:
        return parse_ts_ms(value.get("value"))
    if isinstance(value, list) and value:
        return parse_ts_ms(value[0])
    return None


def format_ts_ms(value: int) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(value / 1000).astimezone().isoformat(timespec="seconds")


def parse_linked_record_ids(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, dict) and isinstance(value.get("link_record_ids"), list):
        raw_items = value.get("link_record_ids") or []
    else:
        raw_items = value if isinstance(value, list) else [value]
    out: List[str] = []
    seen = set()
    for item in raw_items:
        record_id = ""
        if isinstance(item, str):
            record_id = item.strip()
        elif isinstance(item, dict):
            for key in ("record_id", "id"):
                if item.get(key):
                    record_id = clean_feishu_value(item.get(key)).strip()
                    break
        if record_id and record_id.startswith("rec") and record_id not in seen:
            seen.add(record_id)
            out.append(record_id)
    return out


def add_keyword_usage_from_records(
    usage: Dict[str, KeywordUsage],
    records: List[Dict[str, Any]],
    *,
    kind: str,
    link_field: str,
    title_field: str,
    published_field: str,
) -> None:
    for record in records:
        fields = record.get("fields") or {}
        linked_ids = parse_linked_record_ids(fields.get(link_field))
        if not linked_ids:
            continue
        title = clean_feishu_value(fields.get(title_field)).strip()
        ts_ms = parse_ts_ms(fields.get(published_field)) or 0
        for keyword_id in linked_ids:
            stat = usage.setdefault(keyword_id, KeywordUsage())
            if kind == "news":
                stat.news_count += 1
            elif kind == "filtered":
                stat.filtered_count += 1
            else:
                raise ValueError(f"unsupported usage kind: {kind}")
            if ts_ms and (not stat.first_seen_ms or ts_ms < stat.first_seen_ms):
                stat.first_seen_ms = ts_ms
            if ts_ms > stat.last_seen_ms:
                stat.last_seen_ms = ts_ms
            if title:
                stat.samples.append((ts_ms, title))


def sample_titles_for_usage(stat: KeywordUsage, limit: int) -> List[str]:
    titles: List[str] = []
    seen = set()
    for _ts, title in sorted(stat.samples, key=lambda item: item[0], reverse=True):
        if title in seen:
            continue
        seen.add(title)
        titles.append(title)
        if len(titles) >= limit:
            break
    return titles


def build_heat_sample_text(stat: KeywordUsage, limit: int) -> str:
    titles = sample_titles_for_usage(stat, limit)
    lines = [
        f"NEWS次数: {stat.news_count}",
        f"FILTERED次数: {stat.filtered_count}",
    ]
    last_seen = format_ts_ms(stat.last_seen_ms)
    if last_seen:
        lines.append(f"最后出现: {last_seen}")
    if titles:
        lines.append("样本标题:")
        lines.extend(f"- {title}" for title in titles)
    return "\n".join(lines)


def build_compact_candidate_groups(
    entries: List[KeywordEntry],
    usage: Optional[Dict[str, KeywordUsage]] = None,
    sample_limit: int = 5,
) -> List[Dict[str, Any]]:
    groups: Dict[tuple, List[KeywordEntry]] = {}
    for entry in entries:
        key = compact_keyword_key(entry.canonical_name)
        if not key:
            continue
        groups.setdefault((entry.type, key), []).append(entry)

    candidate_groups: List[Dict[str, Any]] = []
    for (type_, key), group in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1])):
        unique_names = {entry.canonical_name for entry in group}
        if len(unique_names) < 2:
            continue
        candidate_groups.append(
            build_candidate_group(
                f"compact::{type_ or 'unknown'}::{key}",
                sorted(group, key=lambda item: item.canonical_name.lower()),
                usage=usage,
                sample_limit=sample_limit,
            )
        )
    return candidate_groups


def normalize_alias_seed_name(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().lower()


def load_alias_seed_groups(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("alias seed file must be a list")
    groups = []
    for item in data:
        if not isinstance(item, dict):
            continue
        names = item.get("names") or []
        if not isinstance(names, list):
            continue
        clean_names = [str(name).strip() for name in names if str(name).strip()]
        if len(clean_names) < 2:
            continue
        groups.append(
            {
                "group_id": str(item.get("group_id") or "alias::" + compact_keyword_key(clean_names[0])),
                "type": str(item.get("type") or "").strip().lower(),
                "names": clean_names,
            }
        )
    return groups


def build_alias_candidate_groups(
    entries: List[KeywordEntry],
    alias_seed_groups: List[Dict[str, Any]],
    usage: Optional[Dict[str, KeywordUsage]] = None,
    sample_limit: int = 5,
) -> List[Dict[str, Any]]:
    entries_by_name: Dict[Tuple[str, str], List[KeywordEntry]] = {}
    for entry in entries:
        key = (entry.type, normalize_alias_seed_name(entry.canonical_name))
        entries_by_name.setdefault(key, []).append(entry)

    out: List[Dict[str, Any]] = []
    seen_group_ids = set()
    for seed in alias_seed_groups:
        type_ = str(seed.get("type") or "").strip().lower()
        matched: List[KeywordEntry] = []
        seen_record_ids = set()
        for name in seed.get("names") or []:
            for entry in entries_by_name.get((type_, normalize_alias_seed_name(name)), []):
                if entry.record_id in seen_record_ids:
                    continue
                seen_record_ids.add(entry.record_id)
                matched.append(entry)
        if len({entry.canonical_name for entry in matched}) < 2:
            continue
        group_id = str(seed.get("group_id") or "alias::" + compact_keyword_key(matched[0].canonical_name))
        if group_id in seen_group_ids:
            continue
        seen_group_ids.add(group_id)
        group = build_candidate_group(
            group_id,
            sorted(matched, key=lambda item: item.canonical_name.lower()),
            usage=usage,
            sample_limit=sample_limit,
        )
        group["candidate_reason"] = ALIAS_REASON
        out.append(group)
    return out


def build_candidate_groups(
    entries: List[KeywordEntry],
    usage: Optional[Dict[str, KeywordUsage]] = None,
    sample_limit: int = 5,
    alias_seed_groups: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    groups = build_compact_candidate_groups(entries, usage=usage, sample_limit=sample_limit)
    alias_groups = build_alias_candidate_groups(
        entries,
        alias_seed_groups or [],
        usage=usage,
        sample_limit=sample_limit,
    )
    seen_ids = {group["group_id"] for group in groups}
    for group in alias_groups:
        if group["group_id"] in seen_ids:
            continue
        seen_ids.add(group["group_id"])
        groups.append(group)
    return sorted(groups, key=lambda item: item["group_id"])


def build_candidate_group(
    group_id: str,
    entries: List[KeywordEntry],
    usage: Optional[Dict[str, KeywordUsage]] = None,
    sample_limit: int = 5,
) -> Dict[str, Any]:
    return {
        "group_id": group_id,
        "candidates": [
            {
                "keyword_id": entry.record_id,
                "name": entry.canonical_name,
                "type": entry.type,
                "aliases": list(entry.aliases),
                "news_count": (usage or {}).get(entry.record_id, KeywordUsage()).news_count,
                "filtered_count": (usage or {}).get(entry.record_id, KeywordUsage()).filtered_count,
                "last_seen_at": format_ts_ms((usage or {}).get(entry.record_id, KeywordUsage()).last_seen_ms),
                "sample_titles": sample_titles_for_usage(
                    (usage or {}).get(entry.record_id, KeywordUsage()),
                    sample_limit,
                ),
            }
            for entry in entries
        ],
    }


def _result_error(message: str) -> Tuple[bool, str]:
    return False, message


def validate_llm_result(group: Dict[str, Any], result: Dict[str, Any]) -> Tuple[bool, str]:
    required = {
        "group_id",
        "decision",
        "confidence",
        "risk",
        "canonical_id",
        "items",
        "merge_ids",
        "skip_ids",
        "force_skip_reason",
    }
    extra = set(result) - required
    missing = required - set(result)
    if missing:
        return _result_error(f"missing fields: {sorted(missing)}")
    if extra:
        return _result_error(f"extra fields: {sorted(extra)}")
    if result.get("group_id") != group.get("group_id"):
        return _result_error("group_id mismatch")
    if result.get("decision") not in {"merge", "skip"}:
        return _result_error("invalid decision")
    if result.get("risk") not in {"low", "medium", "high"}:
        return _result_error("invalid risk")
    confidence = result.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        return _result_error("invalid confidence")
    if not isinstance(result.get("items"), list):
        return _result_error("items must be list")
    if not isinstance(result.get("merge_ids"), list):
        return _result_error("merge_ids must be list")
    if not isinstance(result.get("skip_ids"), list):
        return _result_error("skip_ids must be list")
    if not isinstance(result.get("force_skip_reason"), str):
        return _result_error("force_skip_reason must be string")

    candidates = group.get("candidates") or []
    candidate_ids = {str(item.get("keyword_id")) for item in candidates if item.get("keyword_id")}
    candidate_types = {str(item.get("type") or "").lower() for item in candidates}
    if not candidate_ids:
        return _result_error("candidate ids empty")

    all_result_ids = set(result["merge_ids"]) | set(result["skip_ids"])
    canonical_id = str(result.get("canonical_id") or "")
    if canonical_id:
        all_result_ids.add(canonical_id)
    if not all_result_ids <= candidate_ids:
        return _result_error("result contains unknown keyword_id")

    item_ids = []
    canonical_count = 0
    for item in result["items"]:
        if not isinstance(item, dict):
            return _result_error("item must be object")
        item_required = {"keyword_id", "name", "action", "reason"}
        if set(item) != item_required:
            return _result_error("item fields mismatch")
        keyword_id = str(item.get("keyword_id") or "")
        if keyword_id not in candidate_ids:
            return _result_error("item contains unknown keyword_id")
        action = item.get("action")
        if action not in {"canonical", "merge_to_canonical", "skip"}:
            return _result_error("invalid item action")
        if action == "canonical":
            canonical_count += 1
        item_ids.append(keyword_id)
    if len(item_ids) != len(candidate_ids) or set(item_ids) != candidate_ids:
        return _result_error("items must appear exactly once")

    if result["decision"] == "skip":
        if canonical_id:
            return _result_error("skip canonical_id must be empty")
        if result["merge_ids"]:
            return _result_error("skip merge_ids must be empty")
        if any(item.get("action") != "skip" for item in result["items"]):
            return _result_error("skip items must all use skip action")
        return True, ""

    if result["risk"] != "low":
        return _result_error("merge risk must be low")
    if float(confidence) < 0.95:
        return _result_error("merge confidence below threshold")
    if len(candidate_types) != 1:
        return _result_error("type mismatch")
    if canonical_id not in candidate_ids:
        return _result_error("canonical_id must be candidate")
    if canonical_count != 1:
        return _result_error("merge must have exactly one canonical")
    if not result["merge_ids"]:
        return _result_error("merge_ids must be non-empty")
    if result["skip_ids"]:
        return _result_error("skip_ids must be empty for merge")
    if any(item.get("action") == "skip" for item in result["items"]):
        return _result_error("merge result cannot contain skip action")
    if {canonical_id} | set(result["merge_ids"]) != candidate_ids:
        return _result_error("merge must cover every candidate")
    return True, ""


def llm_results_consistent(first: Dict[str, Any], second: Dict[str, Any]) -> bool:
    if first.get("decision") == "skip" and second.get("decision") == "skip":
        return not first.get("merge_ids") and not second.get("merge_ids")
    first_ids = set(first.get("merge_ids") or [])
    second_ids = set(second.get("merge_ids") or [])
    if first.get("canonical_id"):
        first_ids.add(first.get("canonical_id"))
    if second.get("canonical_id"):
        second_ids.add(second.get("canonical_id"))
    return (
        first.get("decision") == second.get("decision")
        and first.get("risk") == second.get("risk")
        and first_ids == second_ids
    )


def _candidate_canonical_score(candidate: Dict[str, Any]) -> tuple:
    entry = KeywordEntry(
        record_id=str(candidate.get("keyword_id") or ""),
        canonical_name=str(candidate.get("name") or ""),
        type=str(candidate.get("type") or ""),
        aliases=[],
    )
    total_count = int(candidate.get("news_count") or 0) + int(candidate.get("filtered_count") or 0)
    return (*_readability_score(entry), total_count, int(candidate.get("news_count") or 0), str(candidate.get("last_seen_at") or ""))


def deterministic_merge_plan(group: Dict[str, Any]) -> Dict[str, Any]:
    candidates = group.get("candidates") or []
    if not candidates:
        return {"canonical_id": "", "merge_ids": []}
    canonical = sorted(candidates, key=_candidate_canonical_score, reverse=True)[0]
    canonical_id = str(canonical.get("keyword_id") or "")
    return {
        "canonical_id": canonical_id,
        "merge_ids": [
            str(candidate.get("keyword_id") or "")
            for candidate in candidates
            if str(candidate.get("keyword_id") or "") != canonical_id
        ],
    }


def build_llm_article(group: Dict[str, Any]) -> Dict[str, str]:
    group_id = str(group.get("group_id") or "unknown")
    return {
        "title": f"keyword merge group {group_id}",
        "content": json.dumps(group, ensure_ascii=False, indent=2),
    }


def auto_merge_ready(
    group: Dict[str, Any],
    first_result: Dict[str, Any],
    second_result: Dict[str, Any],
) -> Tuple[bool, str]:
    first_ok, first_error = validate_llm_result(group, first_result)
    if not first_ok:
        return False, f"first invalid: {first_error}"
    second_ok, second_error = validate_llm_result(group, second_result)
    if not second_ok:
        return False, f"second invalid: {second_error}"
    if not llm_results_consistent(first_result, second_result):
        return False, "inconsistent llm results"
    if first_result.get("decision") != "merge":
        return False, "decision is not merge"
    return True, ""


def load_merge_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def shuffled_group(group: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(group)
    out["candidates"] = list(reversed(group.get("candidates") or []))
    return out


def call_llm_for_group(
    group: Dict[str, Any],
    prompt_text: str,
    provider: str,
    model: str,
) -> Dict[str, Any]:
    return analyze_with_provider_prompt(
        build_llm_article(group),
        provider,
        prompt_text,
        model,
    )


def fixture_result_matches(case: Dict[str, Any], result: Dict[str, Any]) -> Tuple[bool, str]:
    expected_decision = case.get("expected_decision")
    if result.get("decision") != expected_decision:
        return False, f"expected {expected_decision}, got {result.get('decision')}"
    if expected_decision == "merge":
        expected_canonical = case.get("expected_canonical_id")
        expected_merge_ids = set(case.get("expected_merge_ids") or [])
        if case.get("check_canonical") and expected_canonical and result.get("canonical_id") != expected_canonical:
            return False, f"expected canonical {expected_canonical}, got {result.get('canonical_id')}"
        if case.get("check_merge_ids") and expected_merge_ids and set(result.get("merge_ids") or []) != expected_merge_ids:
            return False, f"expected merge_ids {sorted(expected_merge_ids)}, got {result.get('merge_ids')}"
    return True, ""


def run_fixture_case(
    case: Dict[str, Any],
    prompt_text: str,
    provider: str,
    model: str,
) -> Dict[str, Any]:
    group = case["group"]
    first = call_llm_for_group(group, prompt_text, provider, model)
    second_group = shuffled_group(group)
    second = call_llm_for_group(second_group, prompt_text, provider, model)

    first_valid, first_error = validate_llm_result(group, first)
    second_valid, second_error = validate_llm_result(second_group, second)
    consistent = first_valid and second_valid and llm_results_consistent(first, second)
    expected_ok = False
    expected_error = ""
    if first_valid:
        expected_ok, expected_error = fixture_result_matches(case, first)

    ready = False
    ready_error = ""
    if first.get("decision") == "merge":
        ready, ready_error = auto_merge_ready(group, first, second)

    return {
        "case_id": case.get("case_id"),
        "expected_decision": case.get("expected_decision"),
        "first": first,
        "second": second,
        "first_valid": first_valid,
        "first_error": first_error,
        "second_valid": second_valid,
        "second_error": second_error,
        "consistent": consistent,
        "expected_ok": expected_ok,
        "expected_error": expected_error,
        "auto_merge_ready": ready,
        "auto_merge_error": ready_error,
        "passed": first_valid and second_valid and consistent and expected_ok,
    }


def run_fixture_suite(
    fixture_path: Path,
    prompt_path: Path,
    provider: str,
    model: str,
) -> Dict[str, Any]:
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))
    prompt_text = load_merge_prompt(prompt_path)
    results = [
        run_fixture_case(case, prompt_text, provider, model)
        for case in cases
    ]
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "llm-fixture-run",
        "provider": provider,
        "model": model,
        "fixture_path": str(fixture_path),
        "prompt_path": str(prompt_path),
        "total": len(results),
        "passed": sum(1 for item in results if item["passed"]),
        "failed": sum(1 for item in results if not item["passed"]),
        "results": results,
    }


def parse_aliases(value: Any) -> List[str]:
    text = clean_feishu_value(value)
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_keyword_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        if "value" in value:
            return parse_keyword_count(value.get("value"))
        if "text" in value:
            return parse_keyword_count(value.get("text"))
    if isinstance(value, list):
        if not value:
            return 0
        return parse_keyword_count(value[0])
    text = clean_feishu_value(value).strip().replace(",", "")
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def unique_aliases(values: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        clean = clean_feishu_value(value).strip()
        key = normalize_alias_seed_name(clean)
        if not clean or key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def keyword_entries_from_records(records: List[Dict[str, Any]]) -> List[KeywordEntry]:
    entries: List[KeywordEntry] = []
    for record in records:
        record_id = clean_feishu_value(record.get("record_id")).strip()
        fields = record.get("fields") or {}
        canonical_name = clean_feishu_value(fields.get(config.KEYWORD_FIELD_CANONICAL_NAME)).strip()
        type_ = clean_feishu_value(fields.get(config.KEYWORD_FIELD_TYPE)).strip().lower()
        if not record_id or not canonical_name:
            continue
        entries.append(
            KeywordEntry(
                record_id=record_id,
                canonical_name=canonical_name,
                type=type_,
                aliases=parse_aliases(fields.get(config.KEYWORD_FIELD_ALIASES)),
                news_count=parse_keyword_count(fields.get(config.KEYWORD_FIELD_NEWS_COUNT)),
                filtered_count=parse_keyword_count(fields.get(config.KEYWORD_FIELD_FILTERED_COUNT)),
                note=clean_feishu_value(fields.get(config.KEYWORD_FIELD_NOTE)).strip(),
                parent_ids=parse_linked_record_ids(fields.get(config.KEYWORD_FIELD_PARENT)),
                owner_ids=parse_linked_record_ids(fields.get(config.KEYWORD_FIELD_OWNERS)),
            )
        )
    return entries


def fetch_keyword_entries(max_pages: int, page_size: int, tenant_token: Optional[str] = None) -> List[KeywordEntry]:
    if not config.FEISHU_KEYWORD_TABLE_ID:
        raise RuntimeError("missing FEISHU_KEYWORD_TABLE_ID")
    if tenant_token is None:
        tenant_token = get_tenant_access_token(
            config.FEISHU_APP_ID,
            config.FEISHU_APP_SECRET,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
        )
    records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_KEYWORD_TABLE_ID,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=page_size,
        max_pages=max_pages,
    )
    return keyword_entries_from_records(records)


def load_keyword_snapshot_entries(path: Path) -> List[KeywordEntry]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("keyword snapshot must contain entries list")
    entries: List[KeywordEntry] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        record_id = clean_feishu_value(item.get("record_id")).strip()
        canonical = clean_feishu_value(item.get("canonical_name")).strip()
        type_ = clean_feishu_value(item.get("type")).strip().lower()
        aliases = item.get("aliases") or []
        if not isinstance(aliases, list):
            aliases = []
        if not record_id or not canonical:
            continue
        entries.append(
            KeywordEntry(
                record_id=record_id,
                canonical_name=canonical,
                type=type_,
                aliases=[clean_feishu_value(alias).strip() for alias in aliases if clean_feishu_value(alias).strip()],
                news_count=parse_keyword_count(item.get("news_count")),
                filtered_count=parse_keyword_count(item.get("filtered_count")),
                note=clean_feishu_value(item.get("note")).strip(),
                parent_ids=parse_linked_record_ids(item.get("parent_ids")),
                owner_ids=parse_linked_record_ids(item.get("owner_ids")),
            )
        )
    return entries


def load_alias_discovery_accepted(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    accepted = data.get("accepted") if isinstance(data, dict) else None
    if not isinstance(accepted, list):
        raise ValueError("alias discovery file must contain accepted list")
    return [item for item in accepted if isinstance(item, dict)]


def load_alias_update_preview(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("mode") != "alias-update-preview":
        raise ValueError("alias update preview file must have mode=alias-update-preview")
    updates = data.get("updates")
    if not isinstance(updates, list):
        raise ValueError("alias update preview file must contain updates list")
    return data


def choose_alias_update_main(entries: List[KeywordEntry]) -> KeywordEntry:
    if not entries:
        raise ValueError("entries must not be empty")
    return sorted(
        entries,
        key=lambda entry: (
            -(entry.news_count + entry.filtered_count),
            len(entry.canonical_name),
            entry.canonical_name.lower(),
        ),
    )[0]


def build_alias_update_preview(
    entries: List[KeywordEntry],
    accepted: List[Dict[str, Any]],
    source_path: str = "",
) -> Dict[str, Any]:
    entries_by_key: Dict[Tuple[str, str], List[KeywordEntry]] = {}
    entries_by_id: Dict[str, KeywordEntry] = {}
    for entry in entries:
        entries_by_key.setdefault((entry.type, normalize_alias_seed_name(entry.canonical_name)), []).append(entry)
        entries_by_id[entry.record_id] = entry

    parent: Dict[str, str] = {}
    component_sources: Dict[str, List[Dict[str, Any]]] = {}
    skipped: List[Dict[str, Any]] = []

    def find(record_id: str) -> str:
        parent.setdefault(record_id, record_id)
        if parent[record_id] != record_id:
            parent[record_id] = find(parent[record_id])
        return parent[record_id]

    def union(left: str, right: str) -> str:
        root_left = find(left)
        root_right = find(right)
        if root_left == root_right:
            return root_left
        root = min(root_left, root_right)
        other = root_right if root == root_left else root_left
        parent[other] = root
        component_sources.setdefault(root, [])
        component_sources[root].extend(component_sources.pop(other, []))
        return root

    for item in accepted:
        pair = item.get("pair")
        type_ = clean_feishu_value(item.get("type")).strip().lower()
        if not isinstance(pair, list) or len(pair) < 2:
            skipped.append({"item": item, "reason": "invalid_pair"})
            continue

        matched: List[KeywordEntry] = []
        missing: List[str] = []
        seen_record_ids = set()
        for raw_name in pair:
            name = clean_feishu_value(raw_name).strip()
            found = entries_by_key.get((type_, normalize_alias_seed_name(name)), [])
            if not found:
                missing.append(name)
                continue
            for entry in found:
                if entry.record_id in seen_record_ids:
                    continue
                seen_record_ids.add(entry.record_id)
                matched.append(entry)

        if missing or len(matched) < 2:
            skipped.append({"pair": pair, "type": type_, "missing": missing, "reason": "keyword_record_not_found"})
            continue

        root = find(matched[0].record_id)
        for entry in matched[1:]:
            root = union(root, entry.record_id)
        component_sources.setdefault(root, []).append(
            {
                "pair": pair,
                "reason": clean_feishu_value(item.get("recall_reason")).strip(),
            }
        )

    components: Dict[str, List[KeywordEntry]] = {}
    for record_id in list(parent):
        components.setdefault(find(record_id), []).append(entries_by_id[record_id])

    updates: List[Dict[str, Any]] = []
    for root, component_entries in components.items():
        if len(component_entries) < 2:
            continue
        main = choose_alias_update_main(component_entries)
        existing_aliases = unique_aliases(list(main.aliases))
        add_candidates = [
            entry.canonical_name
            for entry in sorted(component_entries, key=lambda item: item.canonical_name.lower())
            if entry.record_id != main.record_id
        ]
        canonical_key = normalize_alias_seed_name(main.canonical_name)
        existing_keys = {normalize_alias_seed_name(alias) for alias in existing_aliases}
        add_aliases = [
            alias for alias in unique_aliases(add_candidates)
            if normalize_alias_seed_name(alias) != canonical_key
            and normalize_alias_seed_name(alias) not in existing_keys
        ]
        if not add_aliases:
            skipped.append(
                {
                    "canonical_record_id": main.record_id,
                    "canonical_name": main.canonical_name,
                    "reason": "aliases_already_present",
                }
            )
            continue
        new_aliases = unique_aliases(existing_aliases + add_aliases)
        updates.append(
            {
                "canonical_record_id": main.record_id,
                "canonical_name": main.canonical_name,
                "type": main.type,
                "existing_aliases": existing_aliases,
                "add_aliases": add_aliases,
                "source_pairs": component_sources.get(find(root), []),
                "new_aliases": new_aliases,
                "new_aliases_text": "\n".join(new_aliases),
            }
        )

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "alias-update-preview",
        "source_path": source_path,
        "keyword_count": len(entries),
        "accepted_count": len(accepted),
        "update_count": len(updates),
        "skipped_count": len(skipped),
        "updates": sorted(updates, key=lambda item: (item["type"], item["canonical_name"].lower())),
        "skipped": skipped,
    }


def build_alias_update_apply_records(preview: Dict[str, Any], update_limit: int = 0) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    updates = preview.get("updates") or []
    selected_updates = updates[:update_limit] if update_limit > 0 else updates
    for item in selected_updates:
        record_id = clean_feishu_value(item.get("canonical_record_id")).strip()
        aliases = item.get("new_aliases")
        if not isinstance(aliases, list):
            aliases = clean_feishu_value(item.get("new_aliases_text")).splitlines()
        alias_text = "\n".join(unique_aliases([clean_feishu_value(alias).strip() for alias in aliases]))
        if not record_id:
            continue
        records.append(
            {
                "record_id": record_id,
                "fields": {config.KEYWORD_FIELD_ALIASES: alias_text},
            }
        )
    return records


def apply_alias_update_preview(
    preview: Dict[str, Any],
    tenant_token: str,
    update_limit: int = 0,
    dry_run: bool = False,
) -> Dict[str, Any]:
    records = build_alias_update_apply_records(preview, update_limit=update_limit)
    if dry_run:
        return {
            "mode": "alias-update-apply-dry-run",
            "target_count": len(records),
            "updated": 0,
            "failed": [],
            "records": records,
        }

    updated = 0
    failed: List[Dict[str, Any]] = []
    for i in range(0, len(records), 500):
        batch = records[i:i + 500]
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
            failed.append({"records": [item["record_id"] for item in batch], "error": payload})
    return {
        "mode": "alias-update-apply",
        "target_count": len(records),
        "updated": updated,
        "failed": failed,
    }


def fetch_keyword_usage(
    tenant_token: str,
    page_size: int,
    max_pages: int,
) -> Dict[str, KeywordUsage]:
    usage: Dict[str, KeywordUsage] = {}
    if config.FEISHU_NEWS_TABLE_ID:
        news_records = list_bitable_records(
            config.FEISHU_APP_TOKEN,
            config.FEISHU_NEWS_TABLE_ID,
            tenant_token,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
            page_size=page_size,
            max_pages=max_pages,
            sort=[{"field_name": config.NEWS_FIELD_PUBLISHED_MS, "desc": True}],
        )
        add_keyword_usage_from_records(
            usage,
            news_records,
            kind="news",
            link_field=config.NEWS_FIELD_KEYWORD_RECORDS,
            title_field=config.NEWS_FIELD_TITLE,
            published_field=config.NEWS_FIELD_PUBLISHED_MS,
        )
    if config.FEISHU_FILTERED_TABLE_ID:
        filtered_records = list_bitable_records(
            config.FEISHU_APP_TOKEN,
            config.FEISHU_FILTERED_TABLE_ID,
            tenant_token,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
            page_size=page_size,
            max_pages=max_pages,
            sort=[{"field_name": config.FILTERED_FIELD_PUBLISHED_MS, "desc": True}],
        )
        add_keyword_usage_from_records(
            usage,
            filtered_records,
            kind="filtered",
            link_field=config.FILTERED_FIELD_KEYWORD_RECORDS,
            title_field=config.FILTERED_FIELD_TITLE,
            published_field=config.FILTERED_FIELD_PUBLISHED_MS,
        )
    return usage


def required_keyword_core_fields() -> List[Dict[str, Any]]:
    return [
        {
            "name": config.KEYWORD_FIELD_NEWS_COUNT,
            "type": 2,
            "property": {"formatter": "0"},
        },
        {
            "name": config.KEYWORD_FIELD_FILTERED_COUNT,
            "type": 2,
            "property": {"formatter": "0"},
        },
        {
            "name": config.KEYWORD_FIELD_LAST_SEEN,
            "type": 5,
            "property": {"auto_fill": False, "date_formatter": "yyyy-MM-dd HH:mm"},
        },
        {
            "name": config.KEYWORD_FIELD_HEAT_SAMPLE,
            "type": 1,
            "property": None,
        },
    ]


def ensure_keyword_core_fields(tenant_token: str) -> Dict[str, Any]:
    existing = {
        str(field.get("field_name") or ""): field
        for field in list_bitable_fields(
            config.FEISHU_APP_TOKEN,
            config.FEISHU_KEYWORD_TABLE_ID,
            tenant_token,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
        )
    }
    created: List[str] = []
    present: List[str] = []
    errors: List[Dict[str, Any]] = []
    for field in required_keyword_core_fields():
        name = field["name"]
        if name in existing:
            present.append(name)
            continue
        ok, payload = create_bitable_field(
            config.FEISHU_APP_TOKEN,
            config.FEISHU_KEYWORD_TABLE_ID,
            tenant_token,
            name,
            field["type"],
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
            field_property=field["property"],
        )
        if ok:
            created.append(name)
        else:
            errors.append({"field": name, "error": payload})
    return {"created": created, "present": present, "errors": errors}


def build_keyword_core_update_fields(stat: KeywordUsage, sample_limit: int) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        config.KEYWORD_FIELD_NEWS_COUNT: stat.news_count,
        config.KEYWORD_FIELD_FILTERED_COUNT: stat.filtered_count,
        config.KEYWORD_FIELD_HEAT_SAMPLE: build_heat_sample_text(stat, sample_limit),
    }
    if stat.first_seen_ms:
        fields[config.KEYWORD_FIELD_FIRST_SEEN] = stat.first_seen_ms
    if stat.last_seen_ms:
        fields[config.KEYWORD_FIELD_LAST_SEEN] = stat.last_seen_ms
    return fields


def sync_keyword_core_fields(
    entries: List[KeywordEntry],
    usage: Dict[str, KeywordUsage],
    tenant_token: str,
    sample_limit: int,
    update_limit: int,
) -> Dict[str, Any]:
    selected_entries = list(entries)
    selected_entries = selected_entries[:update_limit] if update_limit > 0 else selected_entries
    updated = 0
    failed: List[Dict[str, Any]] = []
    batch: List[Dict[str, Any]] = []
    for entry in selected_entries:
        batch.append(
            {
                "record_id": entry.record_id,
                "fields": build_keyword_core_update_fields(
                    usage.get(entry.record_id, KeywordUsage()),
                    sample_limit,
                ),
            }
        )
        if len(batch) < 500:
            continue
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
            failed.append({"records": [item["record_id"] for item in batch], "error": payload})
        batch = []
    if batch:
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
            failed.append({"records": [item["record_id"] for item in batch], "error": payload})
    return {
        "target_count": len(selected_entries),
        "updated": updated,
        "failed": failed,
    }


def run_keyword_core_field_sync(
    page_size: int,
    max_pages: int,
    usage_max_pages: int,
    sample_limit: int,
    update_limit: int,
) -> Dict[str, Any]:
    tenant_token = get_tenant_access_token(
        config.FEISHU_APP_ID,
        config.FEISHU_APP_SECRET,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )
    field_result = ensure_keyword_core_fields(tenant_token)
    if field_result["errors"]:
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "keyword-core-field-sync",
            "field_result": field_result,
            "sync_result": None,
        }
    entries = fetch_keyword_entries(max_pages=max_pages, page_size=page_size, tenant_token=tenant_token)
    usage = fetch_keyword_usage(
        tenant_token,
        page_size=page_size,
        max_pages=usage_max_pages,
    )
    sync_result = sync_keyword_core_fields(
        entries,
        usage,
        tenant_token,
        sample_limit=sample_limit,
        update_limit=update_limit,
    )
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "keyword-core-field-sync",
        "keyword_count": len(entries),
        "usage_keyword_count": len(usage),
        "field_result": field_result,
        "sync_result": sync_result,
    }


def run_llm_candidate_group(
    group: Dict[str, Any],
    prompt_text: str,
    provider: str,
    model: str,
) -> Dict[str, Any]:
    first = call_llm_for_group(group, prompt_text, provider, model)
    second_group = shuffled_group(group)
    second = call_llm_for_group(second_group, prompt_text, provider, model)

    first_valid, first_error = validate_llm_result(group, first)
    second_valid, second_error = validate_llm_result(second_group, second)
    consistent = first_valid and second_valid and llm_results_consistent(first, second)
    ready = False
    ready_error = ""
    if first_valid and second_valid:
        ready, ready_error = auto_merge_ready(group, first, second)
    merge_plan = deterministic_merge_plan(group) if ready else {"canonical_id": "", "merge_ids": []}

    return {
        "group": group,
        "first": first,
        "second": second,
        "first_valid": first_valid,
        "first_error": first_error,
        "second_valid": second_valid,
        "second_error": second_error,
        "consistent": consistent,
        "auto_merge_ready": ready,
        "auto_merge_error": ready_error,
        "resolved_canonical_id": merge_plan["canonical_id"],
        "resolved_merge_ids": merge_plan["merge_ids"],
    }


def run_llm_dry_run(
    entries: List[KeywordEntry],
    usage: Dict[str, KeywordUsage],
    prompt_path: Path,
    provider: str,
    model: str,
    group_limit: int,
    sample_limit: int,
    alias_seed_path: Path,
) -> Dict[str, Any]:
    prompt_text = load_merge_prompt(prompt_path)
    alias_seed_groups = load_alias_seed_groups(alias_seed_path)
    groups = build_candidate_groups(
        entries,
        usage=usage,
        sample_limit=sample_limit,
        alias_seed_groups=alias_seed_groups,
    )
    selected_groups = groups[:group_limit] if group_limit > 0 else groups
    results = [
        run_llm_candidate_group(group, prompt_text, provider, model)
        for group in selected_groups
    ]
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "llm-dry-run",
        "provider": provider,
        "model": model,
        "prompt_path": str(prompt_path),
        "alias_seed_path": str(alias_seed_path),
        "alias_seed_group_count": len(alias_seed_groups),
        "keyword_count": len(entries),
        "candidate_group_count": len(groups),
        "processed_group_count": len(results),
        "auto_merge_ready_count": sum(1 for item in results if item["auto_merge_ready"]),
        "invalid_count": sum(1 for item in results if not item["first_valid"] or not item["second_valid"]),
        "inconsistent_count": sum(
            1
            for item in results
            if item["first_valid"] and item["second_valid"] and not item["consistent"]
        ),
        "results": results,
    }


def default_output_path(out_dir: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return out_dir / f"keyword-merge-suggestions-{timestamp}.json"


def write_suggestions(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_payload(entries: List[KeywordEntry], suggestions: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "dry-run",
        "keyword_count": len(entries),
        "suggestion_count": len(suggestions),
        "suggestions": suggestions,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate dry-run keyword merge suggestions.")
    parser.add_argument("--output", help="Output JSON path. Defaults to out/keyword-merge-suggestions-YYYYMMDDHHMMSS.json")
    parser.add_argument("--out-dir", default="out", help="Directory used when --output is not set.")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--print", action="store_true", help="Print JSON to stdout too.")
    parser.add_argument("--llm-fixture-run", action="store_true", help="Run local fixture groups through the configured LLM.")
    parser.add_argument("--llm-dry-run", action="store_true", help="Run real keyword candidate groups through the configured LLM without applying changes.")
    parser.add_argument("--sync-core-fields", action="store_true", help="Create and fill KEYWORD heat fields in Feishu.")
    parser.add_argument("--alias-update-preview", action="store_true", help="Build a dry-run preview for appending accepted aliases to KEYWORD aliases field.")
    parser.add_argument("--alias-discovery-path", default="", help="Path to alias_discovery.py output JSON used by --alias-update-preview.")
    parser.add_argument("--keyword-snapshot-path", default="", help="Use a local KEYWORD snapshot instead of reading the full KEYWORD table for alias preview.")
    parser.add_argument("--alias-update-apply", default="", help="Apply an alias-update-preview JSON file to KEYWORD aliases field.")
    parser.add_argument("--alias-update-apply-dry-run", action="store_true", help="Build write payload for --alias-update-apply without writing Feishu.")
    parser.add_argument("--fixture-path", default="tests/fixtures/keyword_merge_cases.json")
    parser.add_argument("--prompt-path", default="docs/local-merge-prompt.md")
    parser.add_argument("--alias-seed-path", default="docs/local-merge-alias-groups.json")
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--model", default="")
    parser.add_argument("--llm-group-limit", type=int, default=20, help="Max real candidate groups to send to LLM. Use 0 for all.")
    parser.add_argument("--usage-max-pages", type=int, default=20, help="Max NEWS/FILTERED pages used for keyword counts and samples.")
    parser.add_argument("--sample-title-limit", type=int, default=5)
    parser.add_argument("--update-limit", type=int, default=0, help="Max KEYWORD records to update for --sync-core-fields. Use 0 for all.")
    parser.add_argument("--tag-generic", action="store_true", help="Tag KEYWORD records matching keyword-name-blocklist as generic in the notes field.")
    parser.add_argument("--tag-generic-dry-run", action="store_true", help="Show which keywords would be tagged generic, without writing.")
    parser.add_argument("--blocklist-path", default="docs/local-keyword-name-blocklist.txt")
    args = parser.parse_args(argv)

    if args.tag_generic or args.tag_generic_dry_run:
        blocklist = _load_keyword_name_blocklist()
        if not blocklist:
            print(f"[tag-generic] blocklist is empty: {args.blocklist_path}")
            return 1
        tenant_token = None
        if args.tag_generic:
            tenant_token = get_tenant_access_token(config.FEISHU_APP_ID, config.FEISHU_APP_SECRET, config.HTTP_TIMEOUT, config.HTTP_RETRIES)
        if args.keyword_snapshot_path and Path(args.keyword_snapshot_path).exists():
            entries = load_keyword_snapshot_entries(Path(args.keyword_snapshot_path))
        else:
            entries = fetch_keyword_entries(max_pages=args.max_pages, page_size=args.page_size, tenant_token=tenant_token)
        matched = []
        for entry in entries:
            normalized = unicodedata.normalize("NFKC", entry.canonical_name).strip().lower()
            if normalized in blocklist:
                matched.append(entry)
        if not matched:
            print(f"[tag-generic] no keywords matched blocklist ({len(blocklist)} terms, {len(entries)} keywords)")
            return 0
        print(f"[tag-generic] matched {len(matched)}/{len(entries)} keywords against {len(blocklist)} blocklist terms")
        for entry in matched:
            print(f"  {entry.canonical_name} ({entry.type}) record_id={entry.record_id}")
        if args.tag_generic_dry_run:
            print("[tag-generic] dry-run complete, no changes written")
            return 0
        assert tenant_token is not None
        tag_prefix = "[generic:blocklist]"
        records_to_update = []
        for entry in matched:
            existing_note = ""
            for rec in list_bitable_records(
                config.FEISHU_APP_TOKEN,
                clean_feishu_value(getattr(config, "FEISHU_KEYWORD_TABLE_ID", "")),
                tenant_token,
                config.HTTP_TIMEOUT,
                config.HTTP_RETRIES,
                filter_obj={"conjunction": "and", "conditions": [{"field_name": config.KEYWORD_FIELD_CANONICAL_NAME, "operator": "is", "value": [entry.canonical_name]}]},
            ):
                existing_note = str((rec.get("fields") or {}).get(config.KEYWORD_FIELD_NOTE) or "")
                break
            if tag_prefix in existing_note:
                continue
            new_note = f"{existing_note} {tag_prefix}".strip() if existing_note else tag_prefix
            records_to_update.append({"record_id": entry.record_id, "fields": {config.KEYWORD_FIELD_NOTE: new_note}})
        if not records_to_update:
            print("[tag-generic] all matched keywords already tagged")
            return 0
        table_id = clean_feishu_value(getattr(config, "FEISHU_KEYWORD_TABLE_ID", ""))
        for i in range(0, len(records_to_update), 500):
            batch = records_to_update[i:i + 500]
            ok, data = batch_update_bitable_records(config.FEISHU_APP_TOKEN, table_id, tenant_token, batch, config.HTTP_TIMEOUT, config.HTTP_RETRIES)
            if not ok:
                print(f"[tag-generic] batch update failed: {data}")
                return 1
        print(f"[tag-generic] tagged {len(records_to_update)} keywords as generic")
        return 0

    if args.alias_update_preview:
        if not args.alias_discovery_path:
            parser.error("--alias-update-preview requires --alias-discovery-path")
        tenant_token = get_tenant_access_token(
            config.FEISHU_APP_ID,
            config.FEISHU_APP_SECRET,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
        )
        entries = fetch_keyword_entries(max_pages=args.max_pages, page_size=args.page_size, tenant_token=tenant_token)
        source_path = Path(args.alias_discovery_path)
        accepted = load_alias_discovery_accepted(source_path)
        payload = build_alias_update_preview(entries, accepted, source_path=str(source_path))
        output_path = Path(args.output) if args.output else default_output_path(Path(args.out_dir))
        write_suggestions(output_path, payload)
        print(
            f"[merge-keywords] alias-update-preview updates={payload['update_count']} "
            f"skipped={payload['skipped_count']} accepted={payload['accepted_count']} "
            f"keywords={payload['keyword_count']} output={output_path}"
        )
        if args.print:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.alias_update_apply:
        tenant_token = get_tenant_access_token(
            config.FEISHU_APP_ID,
            config.FEISHU_APP_SECRET,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
        )
        preview_path = Path(args.alias_update_apply)
        preview = load_alias_update_preview(preview_path)
        payload = apply_alias_update_preview(
            preview,
            tenant_token,
            update_limit=args.update_limit,
            dry_run=args.alias_update_apply_dry_run,
        )
        payload["source_path"] = str(preview_path)
        output_path = Path(args.output) if args.output else Path(args.out_dir) / (
            f"alias-update-apply-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
        )
        write_suggestions(output_path, payload)
        print(
            f"[merge-keywords] {payload['mode']} target={payload['target_count']} "
            f"updated={payload['updated']} failed={len(payload['failed'])} output={output_path}"
        )
        if args.print:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not payload["failed"] else 2

    if args.llm_fixture_run:
        provider = args.provider
        model = args.model or provider_model_for_stage(provider, "screen")
        payload = run_fixture_suite(
            Path(args.fixture_path),
            Path(args.prompt_path),
            provider,
            model,
        )
        output_path = Path(args.output) if args.output else default_output_path(Path(args.out_dir))
        write_suggestions(output_path, payload)
        print(
            f"[merge-keywords] llm-fixture-run passed={payload['passed']}/{payload['total']} "
            f"provider={provider} model={model} output={output_path}"
        )
        if args.print:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["failed"] == 0 else 2

    if args.sync_core_fields:
        payload = run_keyword_core_field_sync(
            page_size=args.page_size,
            max_pages=args.max_pages,
            usage_max_pages=args.usage_max_pages,
            sample_limit=args.sample_title_limit,
            update_limit=args.update_limit,
        )
        output_path = Path(args.output) if args.output else default_output_path(Path(args.out_dir))
        write_suggestions(output_path, payload)
        field_result = payload["field_result"]
        sync_result = payload["sync_result"] or {"updated": 0, "target_count": 0, "failed": []}
        print(
            f"[merge-keywords] sync-core-fields created={field_result['created']} "
            f"present={field_result['present']} updated={sync_result['updated']}/"
            f"{sync_result['target_count']} failed={len(sync_result['failed'])} output={output_path}"
        )
        if args.print:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not field_result["errors"] and not sync_result["failed"] else 2

    if args.llm_dry_run:
        provider = args.provider
        model = args.model or provider_model_for_stage(provider, "screen")
        tenant_token = get_tenant_access_token(
            config.FEISHU_APP_ID,
            config.FEISHU_APP_SECRET,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
        )
        entries = fetch_keyword_entries(max_pages=args.max_pages, page_size=args.page_size, tenant_token=tenant_token)
        usage = fetch_keyword_usage(
            tenant_token,
            page_size=args.page_size,
            max_pages=args.usage_max_pages,
        )
        payload = run_llm_dry_run(
            entries,
            usage,
            Path(args.prompt_path),
            provider,
            model,
            args.llm_group_limit,
            args.sample_title_limit,
            Path(args.alias_seed_path),
        )
        output_path = Path(args.output) if args.output else default_output_path(Path(args.out_dir))
        write_suggestions(output_path, payload)
        print(
            f"[merge-keywords] llm-dry-run ready={payload['auto_merge_ready_count']}/"
            f"{payload['processed_group_count']} candidates={payload['candidate_group_count']} "
            f"keywords={payload['keyword_count']} provider={provider} model={model} output={output_path}"
        )
        if args.print:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    entries = fetch_keyword_entries(max_pages=args.max_pages, page_size=args.page_size)
    suggestions = build_merge_suggestions(entries)
    payload = build_payload(entries, suggestions)

    output_path = Path(args.output) if args.output else default_output_path(Path(args.out_dir))
    write_suggestions(output_path, payload)

    print(f"[merge-keywords] dry-run suggestions={len(suggestions)} keywords={len(entries)} output={output_path}")
    if args.print:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
