# -*- coding: utf-8 -*-
import argparse
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rss_ingest import analyze_with_provider_prompt, clean_feishu_value, provider_model_for_stage
from feishu_client import get_tenant_access_token, list_bitable_records
import config
from merge_keywords import parse_ts_ms


DEFAULT_FIXTURE_PATH = Path("tests/fixtures/alias_discovery_cases.json")
DEFAULT_PROMPT_PATH = Path("docs/local-alias-discovery-prompt.md")
DEFAULT_ALIAS_SEED_PATH = Path("docs/local-merge-alias-groups.json")


@dataclass(frozen=True)
class KeywordEntry:
    record_id: str
    canonical_name: str
    type: str
    aliases: Tuple[str, ...] = ()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log_progress(message: str) -> None:
    print(f"[alias-discovery] {now_iso()} {message}", flush=True)


def resolve_path(path: Any) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return Path(getattr(config, "BASE_DIR", Path(__file__).resolve().parent)) / candidate


def normalize_name(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().lower()


def unique_names(names: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for name in names:
        clean = str(name or "").strip()
        key = normalize_name(clean)
        if not clean or key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def render_prompt(
    prompt_template: str,
    keyword_type: str,
    keyword_names: List[str],
    keyword_titles: Optional[Dict[str, List[str]]] = None,
) -> str:
    if "{{keyword_type}}" not in prompt_template or "{{keyword_names_json_array}}" not in prompt_template:
        raise ValueError("prompt template must contain {{keyword_type}} and {{keyword_names_json_array}}")
    if keyword_titles:
        entries = []
        for name in keyword_names:
            titles = keyword_titles.get(name, [])
            if titles:
                trimmed = [t[:60] for t in titles[:3]]
                entries.append({"name": name, "titles": trimmed})
            else:
                entries.append({"name": name})
        names_json = json.dumps(entries, ensure_ascii=False)
    else:
        names_json = json.dumps(keyword_names, ensure_ascii=False)
    return (
        prompt_template.replace("{{keyword_type}}", keyword_type)
        .replace("{{keyword_names_json_array}}", names_json)
    )


def render_incremental_prompt(
    keyword_type: str,
    new_names: List[str],
    history_entries: List[KeywordEntry],
) -> str:
    history_payload = [
        {
            "name": entry.canonical_name,
            **({"aliases": list(entry.aliases)} if entry.aliases else {}),
        }
        for entry in history_entries
    ]
    return (
        "你是关键词归一助手。你的任务不是重新整理历史词库，而是只判断“新增关键词”是否应该归到“历史快照”里的某个规范词。\n\n"
        f"type: {keyword_type}\n"
        f"新增关键词: {json.dumps(new_names, ensure_ascii=False)}\n"
        f"历史快照: {json.dumps(history_payload, ensure_ascii=False)}\n\n"
        "规则：\n"
        "1. 只能处理新增关键词；历史快照里的词不能彼此合并。\n"
        "2. 每个输出项必须包含一个新增关键词和一个历史规范词。\n"
        "3. 历史规范词必须来自历史快照的 name，不能使用 aliases 作为 canonical。\n"
        "4. 只有新增词和历史规范词可互换、指称边界不变时才输出。\n"
        "5. 如果只是同领域、同公司、同产品线、上下位、版本不同、套餐不同、功能不同，不要输出。\n"
        "6. model 类型最严格：版本号、tier、尺寸、snapshot、suffix 不同一律不要输出。\n"
        "7. 不确定就不要输出。\n\n"
        "输出严格 JSON 对象，不要 Markdown，不要解释文字：\n"
        '{"mappings":[{"new":"新增关键词","canonical":"历史规范词","reason":"为什么是同一对象的别名"}]}\n'
        '如果没有可归一项，输出：{"mappings":[]}'
    )


def parse_json_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        text = text.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
        return json.loads(text)
    return []


_NEGATIVE_REASON_MARKERS = [
    "不应合并", "不能合并", "不应输出", "不是别名", "不等于",
    "不同版本", "不同tier", "不同型号", "不同尺寸", "不满足",
    "禁止合并", "禁止", "上下位", "子类", "子公司", "子品牌",
    "母公司", "版本关系", "相关但", "不是同一", "不指向同一",
    "不符合别名", "不符合合并", "不同的人", "不能互换", "不是同一人",
    "不是同一个", "不是同一家", "不是同一种",
    "not alias", "not the same", "different", "should not",
    "broader", "narrower", "version", "variant", "subtype", "sub-product",
    "subsidiary", "parent company",
]


def _reason_is_negative(reason: str) -> bool:
    lower = reason.lower()
    return any(marker in lower for marker in _NEGATIVE_REASON_MARKERS)


def extract_llm_alias_groups(result: Any, input_names: List[str]) -> List[Dict[str, Any]]:
    if isinstance(result, dict) and "groups" in result:
        payload = result["groups"]
    elif isinstance(result, dict):
        raw_value = result.get("raw", result.get("categories", ""))
        parsed = parse_json_value(raw_value)
        if isinstance(parsed, dict) and "groups" in parsed:
            payload = parsed["groups"]
        elif isinstance(parsed, list):
            payload = parsed
        else:
            payload = parsed
    else:
        payload = parse_json_value(result)
        if isinstance(payload, dict) and "groups" in payload:
            payload = payload["groups"]

    if not isinstance(payload, list):
        raise ValueError("LLM alias response must contain a groups array")

    allowed_by_key = {normalize_name(name): name for name in input_names}
    groups: List[Dict[str, Any]] = []
    seen = set()
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("LLM alias response item must be an object")
        raw_group = item.get("group")
        if not isinstance(raw_group, list):
            raise ValueError("LLM alias response item must contain group list")
        why_might_match = str(item.get("why_might_match") or item.get("reason") or "").strip()
        why_might_not_match = str(item.get("why_might_not_match") or "").strip()
        reason = why_might_match
        if _reason_is_negative(reason) or _reason_is_negative(why_might_not_match):
            continue
        clean_group: List[str] = []
        group_keys = set()
        for name in raw_group:
            key = normalize_name(name)
            if key not in allowed_by_key:
                continue
            if key in group_keys:
                continue
            group_keys.add(key)
            clean_group.append(allowed_by_key[key])
        if len(clean_group) < 2:
            continue
        group_key = tuple(sorted(group_keys))
        if group_key in seen:
            continue
        seen.add(group_key)
        groups.append(
            {
                "group": clean_group,
                "reason": reason,
                "why_might_match": why_might_match,
                "why_might_not_match": why_might_not_match,
                "alias_type": str(item.get("alias_type") or "").strip(),
                "confidence": str(item.get("confidence") or "").strip(),
            }
        )
    return groups


def extract_incremental_alias_groups(
    result: Any,
    new_names: List[str],
    history_entries: List[KeywordEntry],
) -> List[Dict[str, Any]]:
    if isinstance(result, dict) and "mappings" in result:
        payload = result["mappings"]
    elif isinstance(result, dict):
        raw_value = result.get("raw", result.get("categories", ""))
        parsed = parse_json_value(raw_value)
        payload = parsed.get("mappings") if isinstance(parsed, dict) else parsed
    else:
        parsed = parse_json_value(result)
        payload = parsed.get("mappings") if isinstance(parsed, dict) else parsed

    if not isinstance(payload, list):
        raise ValueError("LLM incremental alias response must contain mappings array")

    new_by_key = {normalize_name(name): name for name in new_names}
    history_by_key = {normalize_name(entry.canonical_name): entry.canonical_name for entry in history_entries}
    groups: List[Dict[str, Any]] = []
    seen = set()
    invalid_item_count = 0
    for item in payload:
        if not isinstance(item, dict):
            invalid_item_count += 1
            continue
        raw_new = item.get("new")
        raw_canonical = item.get("canonical")
        new_name = new_by_key.get(normalize_name(raw_new))
        canonical_name = history_by_key.get(normalize_name(raw_canonical))
        if not new_name or not canonical_name:
            continue
        if normalize_name(new_name) == normalize_name(canonical_name):
            continue
        reason = str(item.get("reason") or "").strip()
        if _reason_is_negative(reason):
            continue
        key = tuple(sorted([normalize_name(new_name), normalize_name(canonical_name)]))
        if key in seen:
            continue
        seen.add(key)
        groups.append(
            {
                "group": [canonical_name, new_name],
                "reason": reason,
                "why_might_match": reason,
                "why_might_not_match": "",
                "alias_type": str(item.get("alias_type") or "").strip(),
                "confidence": str(item.get("confidence") or "").strip(),
            }
        )
    if invalid_item_count:
        log_progress(f"incremental parse skipped invalid_items={invalid_item_count}")
    return groups


def call_llm_for_incremental_alias_batch(
    keyword_type: str,
    new_names: List[str],
    history_entries: List[KeywordEntry],
    provider: str,
    model: str,
) -> List[Dict[str, Any]]:
    prompt_text = render_incremental_prompt(keyword_type, new_names, history_entries)
    result = analyze_with_provider_prompt(
        {"title": "alias_discovery_incremental", "content": ""},
        provider,
        prompt_text,
        model,
    )
    return extract_incremental_alias_groups(result, new_names, history_entries)


def call_llm_for_alias_batch(
    keyword_type: str,
    keyword_names: List[str],
    prompt_template: str,
    provider: str,
    model: str,
    keyword_titles: Optional[Dict[str, List[str]]] = None,
) -> List[Dict[str, Any]]:
    prompt_text = render_prompt(prompt_template, keyword_type, keyword_names, keyword_titles)
    result = analyze_with_provider_prompt(
        {"title": "alias_discovery", "content": ""},
        provider,
        prompt_text,
        model,
    )
    return extract_llm_alias_groups(result, keyword_names)


def records_to_keyword_entries(records: List[Dict[str, Any]]) -> List[KeywordEntry]:
    entries: List[KeywordEntry] = []
    for record in records:
        record_id = clean_feishu_value(record.get("record_id")).strip()
        fields = record.get("fields") or {}
        canonical_name = clean_feishu_value(fields.get(config.KEYWORD_FIELD_CANONICAL_NAME)).strip()
        type_ = clean_feishu_value(fields.get(config.KEYWORD_FIELD_TYPE)).strip().lower()
        if not record_id or not canonical_name:
            continue
        aliases_text = clean_feishu_value(fields.get(config.KEYWORD_FIELD_ALIASES))
        aliases = tuple(alias.strip() for alias in aliases_text.splitlines() if alias.strip())
        entries.append(KeywordEntry(record_id=record_id, canonical_name=canonical_name, type=type_, aliases=aliases))
    return entries


def keyword_entry_to_snapshot(entry: KeywordEntry, record: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fields = (record or {}).get("fields") or {}
    aliases_text = clean_feishu_value(fields.get(config.KEYWORD_FIELD_ALIASES))
    aliases = aliases_text.splitlines() if aliases_text else list(entry.aliases)
    return {
        "record_id": entry.record_id,
        "canonical_name": entry.canonical_name,
        "type": entry.type,
        "aliases": aliases,
        "news_count": fields.get(config.KEYWORD_FIELD_NEWS_COUNT, 0),
        "filtered_count": fields.get(config.KEYWORD_FIELD_FILTERED_COUNT, 0),
        "note": clean_feishu_value(fields.get(config.KEYWORD_FIELD_NOTE)).strip(),
    }


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
        if record_id and canonical:
            aliases = item.get("aliases") or []
            if not isinstance(aliases, list):
                aliases = []
            entries.append(
                KeywordEntry(
                    record_id=record_id,
                    canonical_name=canonical,
                    type=type_,
                    aliases=tuple(clean_feishu_value(alias).strip() for alias in aliases if clean_feishu_value(alias).strip()),
                )
            )
    return entries


def merge_keyword_entries(base: List[KeywordEntry], updates: List[KeywordEntry]) -> List[KeywordEntry]:
    by_id = {entry.record_id: entry for entry in base if entry.record_id}
    for entry in updates:
        if entry.record_id:
            by_id[entry.record_id] = entry
    return list(by_id.values())


def write_keyword_snapshot(path: Path, entries: List[KeywordEntry], raw_records: List[Dict[str, Any]], source: str) -> None:
    records_by_id = {clean_feishu_value(record.get("record_id")).strip(): record for record in raw_records}
    payload = {
        "schema_version": 1,
        "generated_at": now_iso(),
        "source": source,
        "entry_count": len(entries),
        "entries": [
            keyword_entry_to_snapshot(entry, records_by_id.get(entry.record_id))
            for entry in sorted(entries, key=lambda item: (item.type, item.canonical_name.lower(), item.record_id))
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_keyword_entries(max_pages: int, page_size: int, tenant_token: Optional[str] = None) -> Tuple[List[KeywordEntry], List[Dict[str, Any]]]:
    table_id = clean_feishu_value(getattr(config, "FEISHU_KEYWORD_TABLE_ID", "")).strip()
    if not table_id:
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
        table_id,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=page_size,
        max_pages=max_pages,
    )
    return records_to_keyword_entries(records), records


def fetch_recent_keyword_entries(
    page_size: int,
    max_pages: int,
    since_ms: int,
    tenant_token: str,
) -> Tuple[List[KeywordEntry], List[Dict[str, Any]]]:
    table_id = clean_feishu_value(getattr(config, "FEISHU_KEYWORD_TABLE_ID", "")).strip()
    if not table_id:
        raise RuntimeError("missing FEISHU_KEYWORD_TABLE_ID")
    records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        table_id,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=page_size,
        max_pages=max_pages,
        sort=[{"field_name": config.KEYWORD_FIELD_FIRST_SEEN, "desc": True}],
        allow_partial=True,
    )
    recent_records: List[Dict[str, Any]] = []
    for record in records:
        fields = record.get("fields") or {}
        first_seen = parse_ts_ms(fields.get(config.KEYWORD_FIELD_FIRST_SEEN)) or 0
        if first_seen >= since_ms:
            recent_records.append(record)
    return records_to_keyword_entries(recent_records), recent_records


def group_entries_by_type(entries: List[KeywordEntry]) -> Dict[str, List[str]]:
    groups: Dict[str, List[str]] = {}
    for entry in entries:
        type_ = entry.type or "unknown"
        groups.setdefault(type_, []).append(entry.canonical_name)
    return {type_: unique_names(names) for type_, names in groups.items()}


def parse_type_filter(value: str) -> set:
    return {
        part.strip().lower()
        for part in str(value or "").split(",")
        if part.strip()
    }


def filter_entries_by_type(entries: List[KeywordEntry], type_filter: str) -> List[KeywordEntry]:
    allowed = parse_type_filter(type_filter)
    if not allowed:
        return entries
    return [entry for entry in entries if (entry.type or "unknown").lower() in allowed]


def chunked(items: List[str], size: int) -> List[List[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def load_alias_seed_sets(path: Path) -> List[Tuple[str, set]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("alias seed file must be a JSON array")
    seeds: List[Tuple[str, set]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        type_ = str(item.get("type") or "").strip().lower()
        names = item.get("names") or []
        if not isinstance(names, list):
            continue
        normalized = {normalize_name(name) for name in names if normalize_name(name)}
        if len(normalized) >= 2:
            seeds.append((type_, normalized))
    return seeds


def is_already_known(candidate: Dict[str, Any], seeds: List[Tuple[str, set]]) -> bool:
    candidate_type = str(candidate.get("type") or "").strip().lower()
    candidate_names = {normalize_name(name) for name in candidate.get("group") or [] if normalize_name(name)}
    if len(candidate_names) < 2:
        return False
    return any(candidate_type == seed_type and candidate_names <= seed_names for seed_type, seed_names in seeds)


def dedupe_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for candidate in candidates:
        key = (
            str(candidate.get("type") or "").strip().lower(),
            tuple(sorted(normalize_name(name) for name in candidate.get("group") or [])),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


LARGE_TYPE_BATCH_SIZE = 200


def discover_candidates_for_type(
    keyword_type: str,
    keyword_names: List[str],
    prompt_template: str,
    provider: str,
    model: str,
    batch_size: int,
) -> List[Dict[str, Any]]:
    if len(keyword_names) < 2:
        return []
    effective_size = max(LARGE_TYPE_BATCH_SIZE, batch_size)
    candidates: List[Dict[str, Any]] = []
    for batch in chunked(keyword_names, effective_size):
        if len(batch) < 2:
            continue
        groups = call_llm_for_alias_batch(keyword_type, batch, prompt_template, provider, model)
        for g in groups:
            candidates.append(
                {"group": g["group"], "type": keyword_type, "reason": g.get("reason", ""), "source": "llm_batch"}
            )
    return candidates


def build_output_payload(
    keyword_count: int,
    types_scanned: List[str],
    candidates: List[Dict[str, Any]],
    already_known_count: int,
) -> Dict[str, Any]:
    return {
        "timestamp": now_iso(),
        "keyword_count": keyword_count,
        "types_scanned": types_scanned,
        "candidates": candidates,
        "already_known_count": already_known_count,
        "new_candidate_count": len(candidates),
    }


def default_output_path(out_dir: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return out_dir / f"alias_discovery_{timestamp}.json"


def write_output(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_real_dry_run(
    prompt_path: Path,
    provider: str,
    model: str,
    page_size: int,
    max_pages: int,
    batch_size: int,
    type_filter: str = "",
) -> Dict[str, Any]:
    prompt_template = prompt_path.read_text(encoding="utf-8")
    tenant_token = get_tenant_access_token(
        config.FEISHU_APP_ID,
        config.FEISHU_APP_SECRET,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )
    all_entries, raw_records = fetch_keyword_entries(max_pages=max_pages, page_size=page_size, tenant_token=tenant_token)
    entries = filter_low_frequency(all_entries, raw_records)
    entries = filter_entries_by_type(entries, type_filter)
    print(f"[alias-discovery] keywords total={len(all_entries)} alias input={len(entries)}")
    by_type = group_entries_by_type(entries)
    all_candidates: List[Dict[str, Any]] = []
    for keyword_type in sorted(by_type):
        all_candidates.extend(
            discover_candidates_for_type(
                keyword_type,
                by_type[keyword_type],
                prompt_template,
                provider,
                model,
                batch_size,
            )
        )

    all_candidates = dedupe_candidates(all_candidates)
    seeds = load_alias_seed_sets(resolve_path(DEFAULT_ALIAS_SEED_PATH))
    new_candidates = [candidate for candidate in all_candidates if not is_already_known(candidate, seeds)]
    return build_output_payload(
        keyword_count=len(entries),
        types_scanned=sorted(by_type),
        candidates=new_candidates,
        already_known_count=len(all_candidates) - len(new_candidates),
    )


def expected_group_found(expected: List[str], predicted_sets: List[set]) -> bool:
    expected_set = {normalize_name(name) for name in expected}
    return any(expected_set == predicted for predicted in predicted_sets)


def expected_non_group_found(expected: List[str], predicted_sets: List[set]) -> bool:
    expected_set = {normalize_name(name) for name in expected}
    return any(expected_set <= predicted for predicted in predicted_sets)


def run_fixture_case(
    case: Dict[str, Any],
    prompt_template: str,
    provider: str,
    model: str,
) -> Tuple[bool, List[Dict[str, Any]], str]:
    keyword_type = str(case.get("type") or "").strip().lower()
    input_keywords = unique_names([str(name) for name in case.get("input_keywords") or []])
    keyword_titles = case.get("keyword_titles") or None
    groups = call_llm_for_alias_batch(keyword_type, input_keywords, prompt_template, provider, model, keyword_titles)
    predicted_sets = [{normalize_name(name) for name in group["group"]} for group in groups]

    missing = [
        group
        for group in case.get("expected_groups") or []
        if not expected_group_found(group, predicted_sets)
    ]
    unexpected = [
        group
        for group in case.get("expected_non_groups") or []
        if expected_non_group_found(group, predicted_sets)
    ]
    passed = not missing and not unexpected
    if passed:
        error = ""
    else:
        error = f"missing={missing} unexpected={unexpected}"
    candidates = [
        {
            "group": group["group"],
            "type": keyword_type,
            "reason": group.get("reason", ""),
            "source": "llm_batch",
        }
        for group in groups
    ]
    return passed, candidates, error


def run_fixture_suite(
    fixture_path: Path,
    prompt_path: Path,
    provider: str,
    model: str,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError("fixture file must be a JSON array")
    prompt_template = prompt_path.read_text(encoding="utf-8")
    all_candidates: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    keyword_count = 0
    types = set()
    for case in cases:
        keyword_count += len(case.get("input_keywords") or [])
        types.add(str(case.get("type") or "").strip().lower())
        passed, candidates, error = run_fixture_case(case, prompt_template, provider, model)
        all_candidates.extend(candidates)
        results.append(
            {
                "id": str(case.get("id") or ""),
                "passed": passed,
                "error": error,
            }
        )
    payload = build_output_payload(
        keyword_count=keyword_count,
        types_scanned=sorted(type_ for type_ in types if type_),
        candidates=dedupe_candidates(all_candidates),
        already_known_count=0,
    )
    return payload, results


MAX_TITLES_PER_KEYWORD = 3
MAX_TITLE_CHARS = 60
BATCH_SIZE_WITH_TITLES = 15
BATCH_SIZE_WITHOUT_TITLES = 30

_VERSION_PATTERN = re.compile(r'\d+(?:\.\d+)+|(?:[vV])\d+')
_DOMAIN_PATTERN = re.compile(r'\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)+\b', re.IGNORECASE)
_TIER_WORD_PATTERN = re.compile(
    r'(会员|订阅|套餐|团队版|企业版|专业版|旗舰版|免费版|'
    r'\b(?:plus|pro|team|enterprise|business|free|premium|basic|starter|tier|plan|'
    r'app|plugin|extension|client|desktop|windows|mac|ios|android)\b)',
    re.IGNORECASE,
)
_UNCERTAIN_REASON_MARKERS = [
    "可能", "疑似", "大概率", "极大概率", "也许", "或许", "似乎",
    "或为", "或是", "或指", "可能对应", "可能指代", "俗称",
    "合租", "代充", "非正式", "不同分发形式",
    "may", "might", "possibly", "probably", "likely",
]
_GENERIC_AI_LATIN_TOKENS = {
    "ai", "agent", "agents", "bot", "bots", "chatbot", "chatbots",
    "assistant", "assistants", "tool", "tools", "platform", "platforms",
    "service", "services", "app", "apps", "workflow", "workflows",
}
_GENERIC_AI_CJK_PARTS = [
    "人工智能", "智能", "代理", "机器人", "助手", "工具", "平台", "系统",
    "服务", "应用", "工作流", "客服", "销售", "营销", "写作", "编程",
    "搜索", "问答", "对话", "自动化", "生成", "内容", "办公",
]


def _extract_version_tokens(name: str) -> set:
    return set(_VERSION_PATTERN.findall(name.lower()))


def _version_veto(name_a: str, name_b: str) -> bool:
    va = _extract_version_tokens(name_a)
    vb = _extract_version_tokens(name_b)
    if va and vb and va != vb:
        return True
    return False


def _compact_alias_key(value: str) -> str:
    text = normalize_name(value)
    return re.sub(r'[\s\-_./+™®]+', '', text)


def deterministic_format_alias_pair(name_a: str, name_b: str) -> bool:
    key_a = _compact_alias_key(name_a)
    key_b = _compact_alias_key(name_b)
    return bool(key_a and key_a == key_b and normalize_name(name_a) != normalize_name(name_b))


def _domain_veto(name_a: str, name_b: str) -> bool:
    if _compact_alias_key(name_a) == _compact_alias_key(name_b):
        return False
    return bool(_DOMAIN_PATTERN.search(name_a) or _DOMAIN_PATTERN.search(name_b))


def _tier_veto(name_a: str, name_b: str) -> bool:
    if _compact_alias_key(name_a) == _compact_alias_key(name_b):
        return False
    return bool(_TIER_WORD_PATTERN.search(name_a) or _TIER_WORD_PATTERN.search(name_b))


def _combo_veto(name: str) -> bool:
    return "+" in name or "/" in name or " vs " in name.lower()


def _uncertain_reason_veto(reason: str) -> bool:
    lower = normalize_name(reason)
    return any(marker in lower for marker in _UNCERTAIN_REASON_MARKERS)


def _generic_ai_label_veto(name_a: str, name_b: str) -> bool:
    if _compact_alias_key(name_a) == _compact_alias_key(name_b):
        return False

    def is_generic_ai_label(name: str) -> bool:
        lower = normalize_name(name)
        if "ai" not in lower and "人工智能" not in lower:
            return False
        if _extract_version_tokens(lower):
            return False
        latin_tokens = re.findall(r'[a-z0-9]+', lower)
        specific_latin = [token for token in latin_tokens if token not in _GENERIC_AI_LATIN_TOKENS]
        if specific_latin:
            return False
        cjk_text = re.sub(r'[a-z0-9\s\-_./+™®]+', '', lower)
        for part in _GENERIC_AI_CJK_PARTS:
            cjk_text = cjk_text.replace(part, "")
        return not cjk_text

    return is_generic_ai_label(name_a) and is_generic_ai_label(name_b)


def _generic_cjk_topic_veto(name_a: str, name_b: str) -> bool:
    if _compact_alias_key(name_a) == _compact_alias_key(name_b):
        return False
    combined = f"{name_a} {name_b}"
    if re.search(r'[A-Za-z0-9]', combined):
        return False
    return bool(re.search(r'[\u4e00-\u9fff]', name_a) and re.search(r'[\u4e00-\u9fff]', name_b))


def stage2_veto_pair(name_a: str, name_b: str, keyword_type: str, why_might_not: str) -> Optional[str]:
    if normalize_name(name_a) == normalize_name(name_b):
        return "identical_after_normalize"
    if _reason_is_negative(why_might_not):
        return "negative_reason"
    if _uncertain_reason_veto(why_might_not):
        return "uncertain_reason"
    if _combo_veto(name_a) or _combo_veto(name_b):
        return "combo_word"
    if _domain_veto(name_a, name_b):
        return "domain_word"
    if keyword_type in {"product", "topic", "model"} and _tier_veto(name_a, name_b):
        return "tier_word"
    if keyword_type in {"product", "topic"} and _generic_ai_label_veto(name_a, name_b):
        return "generic_ai_label"
    if keyword_type == "topic" and _generic_cjk_topic_veto(name_a, name_b):
        return "generic_cjk_topic"
    if keyword_type == "model" and _version_veto(name_a, name_b):
        return "model_version_mismatch"
    return None


def render_verify_prompt(template: str, keyword_type: str, name_a: str, name_b: str,
                         context_a: str, context_b: str, recall_reason: str) -> str:
    return (template
            .replace("{{keyword_type}}", keyword_type)
            .replace("{{name_a}}", name_a)
            .replace("{{name_b}}", name_b)
            .replace("{{context_a}}", context_a)
            .replace("{{context_b}}", context_b)
            .replace("{{recall_reason}}", recall_reason))


def call_pairwise_verify(keyword_type: str, name_a: str, name_b: str,
                         context_a: str, context_b: str, recall_reason: str,
                         verify_template: str, provider: str, model: str) -> Dict[str, Any]:
    prompt_text = render_verify_prompt(verify_template, keyword_type, name_a, name_b,
                                       context_a, context_b, recall_reason)
    result = analyze_with_provider_prompt(
        {"title": "alias_verify", "content": ""},
        provider, prompt_text, model,
    )
    if isinstance(result, dict) and "decision" in result:
        return result
    if isinstance(result, dict):
        raw = result.get("raw", result.get("categories", ""))
        parsed = parse_json_value(raw)
        if isinstance(parsed, dict):
            return parsed
    return {"decision": "reject", "rejection_reason": "parse_failure"}


def verify_consistency_check(v: Dict[str, Any]) -> str:
    decision = str(v.get("decision") or "").strip().lower()
    counter = str(v.get("strongest_counterargument") or "").strip().lower()
    rej = str(v.get("rejection_reason") or "").strip().lower()
    is_alias = v.get("is_alias")
    if decision == "accept":
        if counter and counter != "none":
            return "reject"
        if rej and rej != "none":
            return "reject"
        if is_alias is False:
            return "reject"
    return decision


def batch_verify_decision(result: Dict[str, Any]) -> str:
    decision = str(result.get("decision") or "").strip().lower()
    reason = str(result.get("reason") or result.get("positive_reason") or "")
    counter = str(result.get("strongest_counterargument") or "").strip().lower()
    rejection = str(result.get("rejection_reason") or "").strip().lower()
    is_alias = result.get("is_alias")
    if _reason_is_negative(reason) or _reason_is_negative(counter) or _reason_is_negative(rejection):
        return "reject"
    if _uncertain_reason_veto(reason):
        return "reject"
    if decision == "accept":
        if counter and counter != "none":
            return "reject"
        if rejection and rejection != "none":
            return "reject"
        if is_alias is False:
            return "reject"
    return decision if decision in {"accept", "reject", "quarantine"} else "reject"


def build_batch_verify_prompt(candidates_text: str) -> str:
    return (
        "你是别名验证器。以下是候选别名对，请逐个判断是否为同一真实对象的别名。\n"
        "只输出严格 JSON 对象，不要 Markdown，不要额外解释：\n"
        '{"results": [{"A": "...", "B": "...", "decision": "accept|reject|quarantine", '
        '"is_alias": true|false, "alias_type": "format_variant|translation|transliteration|nickname|abbreviation|rebrand|codename|same_term|none", '
        '"positive_reason": "...", "strongest_counterargument": "none 或最强反证", '
        '"rejection_reason": "version_mismatch|tier_mismatch|parent_child|company_product|competitor|related_not_alias|broader_narrower|different_entity|generic_specific|malformed_combo|insufficient_evidence|none"}]}\n\n'
        "accept 必须同时满足：A 和 B 可互换，指称边界不变；strongest_counterargument 必须是 none；rejection_reason 必须是 none；is_alias 必须是 true。\n"
        "命中以下任一情况必须 reject：上下位、母子品牌、公司产品、版本差异、tier 差异、竞品、相关但不同义、泛词和具体词、拼接词、只有共现或标题相似。\n"
        "product 类型额外规则：基础产品名不要和 app/plugin/extension/client/desktop/windows/team/plus/pro/会员/订阅/tier 等具体形态或套餐合并，除非两者确实是同一个正式名称的纯写法差异。\n"
        "通用 AI 能力词、角色词、服务形态词之间不要合并；只有真实品牌、产品、模型、协议、论文名等实体才可合并。\n"
        "model 类型额外规则：版本号、tier、尺寸、snapshot、suffix 不同一律 reject；家族名不得和具体版本合并；只有大小写、空格、横杠差异可 accept。\n"
        "technology/topic 类型补充：资讯流归一允许“核心词”和“核心词+通用中文后缀”合并，例如后缀为协议、架构、层、模型、技术、工具、平台、框架、系统、引擎、模式、机制等；这类不是普通上下位，若它们明显指向同一检索入口，可 accept。\n"
        "但带不同英文前缀或限定场景的词不要因为共享核心词就合并；只有中文后缀或纯翻译/缩写/格式差异可以放宽。\n"
        "topic 类型额外规则：只有同一术语的不同写法/翻译/缩写才 accept；普通中文议题短语、同领域、同主题、因果、应用、流程、情绪的不同角度一律 reject。\n\n"
        f"候选对：\n{candidates_text}"
    )


def run_three_stage(
    recall_prompt_path: Path,
    verify_prompt_path: Path,
    provider: str,
    model: str,
    page_size: int,
    max_pages: int,
    batch_size: int,
    type_filter: str = "",
    keyword_snapshot_path: str = "",
    incremental_since_ms: int = 0,
    snapshot_output_path: str = "",
) -> Dict[str, Any]:
    recall_template = recall_prompt_path.read_text(encoding="utf-8")

    tenant_token = get_tenant_access_token(
        config.FEISHU_APP_ID, config.FEISHU_APP_SECRET, config.HTTP_TIMEOUT, config.HTTP_RETRIES,
    )
    mode = "full"
    snapshot_path = Path(keyword_snapshot_path) if keyword_snapshot_path else None
    snapshot_entries: List[KeywordEntry] = []
    if snapshot_path and snapshot_path.exists() and incremental_since_ms > 0:
        mode = "incremental"
        snapshot_entries = load_keyword_snapshot_entries(snapshot_path)
        recent_entries, raw_records = fetch_recent_keyword_entries(
            page_size=page_size,
            max_pages=max_pages,
            since_ms=incremental_since_ms,
            tenant_token=tenant_token,
        )
        all_entries = merge_keyword_entries(snapshot_entries, recent_entries)
        scan_entries = recent_entries
    else:
        all_entries, raw_records = fetch_keyword_entries(max_pages=max_pages, page_size=page_size, tenant_token=tenant_token)
        scan_entries = all_entries

    if snapshot_output_path:
        write_keyword_snapshot(Path(snapshot_output_path), all_entries, raw_records, source=f"alias-discovery:{mode}")

    scan_entries = filter_low_frequency(scan_entries, raw_records)
    scan_entries = filter_entries_by_type(scan_entries, type_filter)
    scan_keys = {(entry.type, normalize_name(entry.canonical_name)) for entry in scan_entries}
    scan_types = {entry.type for entry in scan_entries}
    candidate_entries = [
        entry for entry in all_entries
        if entry.type in scan_types
    ]
    candidate_entries = filter_entries_by_type(candidate_entries, type_filter)
    log_progress(
        f"mode={mode} keywords total={len(all_entries)} "
        f"scan input={len(scan_entries)} candidate input={len(candidate_entries)}"
    )
    by_type = group_entries_by_type(candidate_entries)
    scan_by_type = group_entries_by_type(scan_entries)
    history_by_type: Dict[str, List[KeywordEntry]] = {}
    for entry in snapshot_entries:
        history_by_type.setdefault(entry.type or "unknown", []).append(entry)

    # Stage 1: Recall
    stage1_groups: List[Dict[str, Any]] = []
    for keyword_type in sorted(by_type):
        if mode == "incremental":
            new_names = scan_by_type.get(keyword_type, [])
            history_entries = history_by_type.get(keyword_type, [])
            if not new_names or not history_entries:
                log_progress(
                    f"stage1 incremental skip type={keyword_type} "
                    f"new={len(new_names)} history={len(history_entries)}"
                )
                continue
            batches = chunked(new_names, batch_size)
            log_progress(
                f"stage1 incremental start type={keyword_type} "
                f"new={len(new_names)} history={len(history_entries)} batches={len(batches)} batch_size={batch_size}"
            )
            for batch_index, batch in enumerate(batches, start=1):
                log_progress(
                    f"stage1 incremental batch start type={keyword_type} "
                    f"batch={batch_index}/{len(batches)} size={len(batch)}"
                )
                groups = call_llm_for_incremental_alias_batch(keyword_type, batch, history_entries, provider, model)
                log_progress(
                    f"stage1 incremental batch done type={keyword_type} "
                    f"batch={batch_index}/{len(batches)} groups={len(groups)}"
                )
                for g in groups:
                    stage1_groups.append({**g, "type": keyword_type})
        else:
            names = by_type[keyword_type]
            batches = chunked(names, batch_size)
            log_progress(
                f"stage1 full start type={keyword_type} "
                f"names={len(names)} batches={len(batches)} batch_size={batch_size}"
            )
            for batch_index, batch in enumerate(batches, start=1):
                if len(batch) < 2:
                    continue
                log_progress(
                    f"stage1 full batch start type={keyword_type} "
                    f"batch={batch_index}/{len(batches)} size={len(batch)}"
                )
                groups = call_llm_for_alias_batch(keyword_type, batch, recall_template, provider, model)
                log_progress(
                    f"stage1 full batch done type={keyword_type} "
                    f"batch={batch_index}/{len(batches)} groups={len(groups)}"
                )
                for g in groups:
                    stage1_groups.append({**g, "type": keyword_type})
    log_progress(f"stage1 done groups={len(stage1_groups)}")

    # Stage 2: Veto (expand groups to pairs, apply rules)
    stage2_pairs: List[Dict[str, Any]] = []
    deterministic_pairs: List[Dict[str, Any]] = []
    vetoed_count = 0
    for group_info in stage1_groups:
        names = group_info.get("group") or []
        keyword_type = group_info.get("type", "")
        recall_reason = str(group_info.get("why_might_match") or group_info.get("reason") or "")
        why_not = str(group_info.get("why_might_not_match") or "")
        veto_context = " ".join(
            part for part in [why_not, recall_reason]
            if part and normalize_name(part) != "none"
        )
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                veto = stage2_veto_pair(names[i], names[j], keyword_type, veto_context)
                if veto:
                    vetoed_count += 1
                elif deterministic_format_alias_pair(names[i], names[j]):
                    deterministic_pairs.append({
                        "name_a": names[i], "name_b": names[j],
                        "type": keyword_type,
                        "recall_reason": recall_reason,
                    })
                else:
                    stage2_pairs.append({
                        "name_a": names[i], "name_b": names[j],
                        "type": keyword_type,
                        "recall_reason": recall_reason,
                    })
    log_progress(
        f"stage2 done pairs={len(stage2_pairs)} deterministic={len(deterministic_pairs)} vetoed={vetoed_count}"
    )

    # Dedupe pairs
    seen_pairs: set = set()
    unique_pairs: List[Dict[str, Any]] = []
    for p in stage2_pairs:
        key = tuple(sorted([normalize_name(p["name_a"]), normalize_name(p["name_b"])]))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        unique_pairs.append(p)

    deterministic_unique_pairs: List[Dict[str, Any]] = []
    for p in deterministic_pairs:
        key = tuple(sorted([normalize_name(p["name_a"]), normalize_name(p["name_b"])]))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        deterministic_unique_pairs.append(p)

    # Stage 3: Verify only surviving candidates (NOT full keyword scan)
    accepted: List[Dict[str, Any]] = [
        {
            "pair": [p["name_a"], p["name_b"]],
            "type": p["type"],
            "recall_reason": p.get("recall_reason", ""),
            "decision": "deterministic_format_alias",
        }
        for p in deterministic_unique_pairs
    ]
    rejected: List[Dict[str, Any]] = []

    # Build compact candidate list for verification
    verify_items = [
        {"a": p["name_a"], "b": p["name_b"], "type": p["type"], "reason": p.get("recall_reason", "")}
        for p in unique_pairs
    ]

    # Batch candidates (not keywords) — up to 40 pairs per LLM call
    VERIFY_BATCH_SIZE = 40
    verify_batches = chunked(verify_items, VERIFY_BATCH_SIZE)
    log_progress(f"stage3 start verify_items={len(verify_items)} batches={len(verify_batches)}")
    for batch_index, batch in enumerate(verify_batches, start=1):
        log_progress(f"stage3 batch start batch={batch_index}/{len(verify_batches)} size={len(batch)}")
        candidates_text = json.dumps(
            [{"A": c["a"], "B": c["b"], "type": c["type"]} for c in batch],
            ensure_ascii=False,
        )
        verify_prompt = build_batch_verify_prompt(candidates_text)
        result = analyze_with_provider_prompt(
            {"title": "alias_verify", "content": ""},
            provider, verify_prompt, model,
        )
        log_progress(f"stage3 batch response batch={batch_index}/{len(verify_batches)}")
        # Parse results
        parsed = None
        if isinstance(result, dict) and "results" in result:
            parsed = result["results"]
        elif isinstance(result, dict):
            raw = result.get("raw", result.get("categories", ""))
            try:
                obj = parse_json_value(raw)
                if isinstance(obj, dict) and "results" in obj:
                    parsed = obj["results"]
                elif isinstance(obj, list):
                    parsed = obj
            except Exception:
                pass

        if not parsed or not isinstance(parsed, list):
            for c in batch:
                rejected.append({"pair": [c["a"], c["b"]], "type": c["type"], "decision": "parse_failure"})
            log_progress(f"stage3 batch parse_failure batch={batch_index}/{len(verify_batches)}")
            continue

        decision_map: Dict[str, str] = {}
        for r in parsed:
            if not isinstance(r, dict):
                continue
            key = tuple(sorted([normalize_name(r.get("A", "")), normalize_name(r.get("B", ""))]))
            dec = batch_verify_decision(r)
            decision_map[key] = dec

        for c in batch:
            key = tuple(sorted([normalize_name(c["a"]), normalize_name(c["b"])]))
            dec = decision_map.get(key, "reject")
            entry = {"pair": [c["a"], c["b"]], "type": c["type"], "recall_reason": c["reason"]}
            if dec == "accept":
                entry["decision"] = "accept"
                accepted.append(entry)
            else:
                entry["decision"] = dec or "rejected_by_verifier"
                rejected.append(entry)
        log_progress(
            f"stage3 batch done batch={batch_index}/{len(verify_batches)} "
            f"accepted={len(accepted)} rejected={len(rejected)}"
        )

    return {
        "timestamp": now_iso(),
        "mode": mode,
        "keyword_count": len(candidate_entries),
        "scan_keyword_count": len(scan_entries),
        "total_keyword_count": len(all_entries),
        "types_scanned": sorted(by_type),
        "summary": {
            "stage1_candidate_groups": len(stage1_groups),
            "stage2_vetoed_pairs": vetoed_count,
            "deterministic_accepted_pairs": len(deterministic_unique_pairs),
            "stage3_verified_pairs": len(unique_pairs),
            "stage3_accepted": len(accepted),
            "stage3_rejected": len(rejected),
        },
        "accepted": accepted,
        "rejected": rejected,
    }


MIN_KEYWORD_COUNT = 1


def filter_low_frequency(entries: List[KeywordEntry], records: List[Dict[str, Any]]) -> List[KeywordEntry]:
    return entries


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Discover keyword alias candidates.")
    parser.add_argument("--fixture-run", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--three-stage", action="store_true")
    parser.add_argument("--recall-prompt-path", default="docs/local-alias-recall-prompt.md")
    parser.add_argument("--verify-prompt-path", default="docs/local-alias-verify-prompt.md")
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--model", default="")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--prompt-path", default=str(DEFAULT_PROMPT_PATH))
    parser.add_argument("--output", help="Output JSON path.")
    parser.add_argument("--out-dir", default="out")
    parser.add_argument("--batch-size", type=int, default=9999, help="Max keywords per LLM batch. Default: no limit (entire type at once).")
    parser.add_argument("--type-filter", default="", help="Comma-separated keyword types to scan, e.g. model,product.")
    parser.add_argument("--keyword-snapshot-path", default="", help="Existing keyword snapshot for incremental alias discovery.")
    parser.add_argument("--incremental-since-ms", type=int, default=0, help="Only scan keywords first seen since this timestamp when a snapshot exists.")
    parser.add_argument("--snapshot-output", default="", help="Write the keyword snapshot used by this run.")
    parser.add_argument("--print", action="store_true")
    args = parser.parse_args(argv)

    modes = sum([args.fixture_run, args.dry_run, args.three_stage])
    if modes != 1:
        parser.error("choose exactly one of --fixture-run, --dry-run, or --three-stage")

    provider = args.provider
    model = args.model or provider_model_for_stage(provider, "screen")
    prompt_path = resolve_path(args.prompt_path)

    if args.fixture_run:
        payload, results = run_fixture_suite(resolve_path(DEFAULT_FIXTURE_PATH), prompt_path, provider, model)
        passed = sum(1 for r in results if r["passed"])
        for r in results:
            status = "PASS" if r["passed"] else "FAIL"
            suffix = "" if r["passed"] else f" {r['error']}"
            print(f"[alias-discovery] {status} {r['id']}{suffix}")
        print(f"[alias-discovery] fixture summary passed={passed}/{len(results)} failed={len(results)-passed}")
    elif args.three_stage:
        payload = run_three_stage(
            resolve_path(args.recall_prompt_path), resolve_path(args.verify_prompt_path),
            provider, model, args.page_size, args.max_pages, args.batch_size, args.type_filter,
            args.keyword_snapshot_path, args.incremental_since_ms, args.snapshot_output,
        )
        s = payload["summary"]
        print(f"[alias-discovery] three-stage: recall={s['stage1_candidate_groups']} vetoed={s['stage2_vetoed_pairs']} accepted={s['stage3_accepted']} rejected={s['stage3_rejected']} keywords={payload['keyword_count']}")
    else:
        payload = run_real_dry_run(prompt_path, provider, model, args.page_size, args.max_pages, args.batch_size, args.type_filter)
        print(f"[alias-discovery] dry-run candidates={payload['new_candidate_count']} known_filtered={payload['already_known_count']} keywords={payload['keyword_count']}")

    output_path = resolve_path(args.output) if args.output else default_output_path(resolve_path(args.out_dir))
    write_output(output_path, payload)
    print(f"[alias-discovery] output={output_path}")
    if args.print:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
