# -*- coding: utf-8 -*-
"""Use an LLM to review noisy KEYWORD records without writing changes."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
import rss_ingest
from feishu_client import batch_delete_bitable_records, get_tenant_access_token, list_bitable_records

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


DEFAULT_PROMPT_PATH = Path("docs/local-keyword-noise-audit-prompt.md")
VALID_DECISIONS = {"keep", "block_auto", "review"}
VALID_RISKS = {"low", "medium", "high"}
GENERIC_PHRASE_HINTS = {
    "行业", "平台", "工具", "产品", "模型", "应用", "服务", "系统", "生态", "内容", "业务",
    "趋势", "分析", "观点", "报告", "指南", "功能", "更新", "升级", "发布",
    "用户", "客户", "开发者", "团队", "研究人员",
    "增长", "下降", "突破", "扩张", "转型", "合作", "融资", "投资",
    "隐私", "版权", "合规", "伦理", "监管", "安全风险",
    "体验", "优化", "提升", "需求", "压力", "效率", "工作流", "自动化",
}
MANUAL_FIELD_CANDIDATES = ("manual/auto", "manual", "来源", "创建方式", "创建来源", config.KEYWORD_FIELD_NOTE)


def keyword_record(record: Dict[str, Any]) -> Dict[str, Any]:
    fields = record.get("fields") or {}
    return {
        "record_id": str(record.get("record_id") or "").strip(),
        "keyword": rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_CANONICAL_NAME)).strip(),
        "type": rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_TYPE)).strip().lower(),
        "note": rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_NOTE)).strip(),
        "first_seen_ms": rss_ingest.parse_ts_ms(fields.get(config.KEYWORD_FIELD_FIRST_SEEN)) or 0,
        "news_count": rss_ingest.parse_int(fields.get(config.KEYWORD_FIELD_NEWS_COUNT)) or 0,
        "filtered_count": rss_ingest.parse_int(fields.get(config.KEYWORD_FIELD_FILTERED_COUNT)) or 0,
        "heat_30d": rss_ingest.parse_int(fields.get("30d")) or 0,
    }


def is_manual_fields(fields: Dict[str, Any]) -> bool:
    for field_name in MANUAL_FIELD_CANDIDATES:
        value = rss_ingest.clean_feishu_value(fields.get(field_name)).strip().lower()
        if value == "manual" or "[manual]" in value:
            return True
    return False


def noise_score(item: Dict[str, Any]) -> int:
    name = str(item.get("keyword") or "").strip()
    lower = name.lower()
    normalized = rss_ingest.normalize_keyword_alias(name)
    score = 0
    if not name:
        return 999
    blocklist = getattr(rss_ingest, "_KEYWORD_NAME_BLOCKLIST", set()) or set()
    compact_blocklist = {rss_ingest.compact_keyword_alias(item) for item in blocklist if item}
    if normalized in blocklist:
        score += 120
    elif compact_keyword := rss_ingest.compact_keyword_alias(name):
        if compact_keyword in compact_blocklist:
            score += 100
    for hint in GENERIC_PHRASE_HINTS:
        if hint and hint in name:
            score += 18
            if name.endswith(hint):
                score += 12
    if re.fullmatch(r"\d+(\.\d+)?\s*(亿用户|吉瓦算力|亿美元.*)", lower):
        score += 55
    if int(item.get("heat_30d") or 0) == 0:
        score += 10
    if int(item.get("news_count") or 0) + int(item.get("filtered_count") or 0) == 0:
        score += 10
    return score


def select_candidates(records: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    active = [
        item for item in records
        if item.get("record_id")
        and item.get("keyword")
        and not rss_ingest.is_merged_keyword_note(item.get("note"))
        and "[manual]" not in str(item.get("note") or "").lower()
    ]
    active.sort(key=lambda item: (noise_score(item), int(item.get("first_seen_ms") or 0), str(item.get("keyword") or "")), reverse=True)
    if limit <= 0:
        return active
    return active[:limit]


def filter_recent_records(records: List[Dict[str, Any]], incremental_since_ms: int) -> List[Dict[str, Any]]:
    if incremental_since_ms <= 0:
        return records
    return [item for item in records if int(item.get("first_seen_ms") or 0) >= incremental_since_ms]


def build_batch_prompt(base_prompt: str, batch: List[Dict[str, Any]]) -> str:
    payload = [
        {
            "record_id": item["record_id"],
            "keyword": item["keyword"],
            "type": item.get("type") or "",
            "news_count": item.get("news_count") or 0,
            "filtered_count": item.get("filtered_count") or 0,
            "heat_30d": item.get("heat_30d") or 0,
            "first_seen_ms": item.get("first_seen_ms") or 0,
        }
        for item in batch
    ]
    return (
        f"{base_prompt.strip()}\n\n"
        "## 待审查关键词\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def build_actionable_only_prompt(base_prompt: str, batch: List[Dict[str, Any]]) -> str:
    return (
        build_batch_prompt(base_prompt, batch)
        + "\n\n## 本次输出要求\n\n"
        "只返回需要处理的关键词：decision 为 block_auto 或 review 的 items。\n"
        "明确 keep 的关键词不要返回。\n"
        "不要为了凑数返回不确定项。"
    )


def parse_first_json_object(raw_text: str) -> Dict[str, Any]:
    text = str(raw_text or "").strip()
    text = text.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
    start = text.find("{")
    if start < 0:
        raise json.JSONDecodeError("missing JSON object", text, 0)
    parsed, _end = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("first JSON value is not an object", text, start)
    return parsed


def build_gemini_noise_audit_payload(prompt: str) -> Dict[str, Any]:
    return {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens": 65536,
            "thinkingConfig": {"thinkingLevel": "minimal"},
        },
    }


def extract_gemini_text(data: Dict[str, Any]) -> str:
    parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    return "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict)).strip()


def analyze_with_gemini_direct(prompt: str, model: str) -> Dict[str, Any]:
    if rss_ingest.gemini_backend() != "vertex" and not str(config.GEMINI_API_KEY or "").strip():
        raise RuntimeError("missing GEMINI_API_KEY")
    url = rss_ingest.build_gemini_url(model or config.GEMINI_MODEL_NAME)
    payload = build_gemini_noise_audit_payload(prompt)
    headers = rss_ingest.build_gemini_headers()
    attempts = max(1, int(getattr(config, "GEMINI_RETRIES", 1) or 1))
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            resp = rss_ingest._http_post(url, headers=headers, payload=payload, timeout=config.GEMINI_TIMEOUT)
            if resp.status_code == 200:
                raw_text = extract_gemini_text(resp.json())
                return parse_first_json_object(raw_text)
            last_error = RuntimeError(rss_ingest.response_snippet(resp))
            if resp.status_code == 400:
                break
        except Exception as exc:
            last_error = exc
        if attempt < attempts:
            time.sleep(min(8.0, 1.2 * attempt))
    raise RuntimeError(f"Gemini direct call failed: {last_error}")


def raw_action_items(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for decision in ("block_auto", "review"):
        bucket = raw.get(decision)
        if isinstance(bucket, list):
            for item in bucket:
                if isinstance(item, dict):
                    merged = dict(item)
                    merged["decision"] = decision
                    items.append(merged)
    raw_items = raw.get("items")
    if isinstance(raw_items, list):
        items.extend(item for item in raw_items if isinstance(item, dict))
    return items


def normalize_llm_items(raw: Dict[str, Any], batch: List[Dict[str, Any]], fill_missing: bool = True) -> List[Dict[str, Any]]:
    by_id = {item["record_id"]: item for item in batch}
    out: List[Dict[str, Any]] = []
    seen = set()
    items = raw_action_items(raw) if isinstance(raw, dict) else []
    for item in items:
        record_id = str(item.get("record_id") or "").strip()
        if record_id not in by_id or record_id in seen:
            continue
        decision = str(item.get("decision") or "").strip()
        risk = str(item.get("risk") or "").strip()
        if decision not in VALID_DECISIONS:
            decision = "review"
        if risk not in VALID_RISKS:
            risk = "medium"
        source = by_id[record_id]
        out.append(
            {
                "record_id": record_id,
                "keyword": source["keyword"],
                "type": source.get("type") or "",
                "decision": decision,
                "risk": risk,
                "reason": str(item.get("reason") or "").strip()[:200],
                "news_count": source.get("news_count") or 0,
                "filtered_count": source.get("filtered_count") or 0,
                "heat_30d": source.get("heat_30d") or 0,
                "first_seen_ms": source.get("first_seen_ms") or 0,
                "noise_score": noise_score(source),
            }
        )
        seen.add(record_id)
    if fill_missing:
        for record_id, source in by_id.items():
            if record_id not in seen:
                out.append(
                    {
                        "record_id": record_id,
                        "keyword": source["keyword"],
                        "type": source.get("type") or "",
                        "decision": "review",
                        "risk": "high",
                        "reason": "LLM未返回该记录",
                        "news_count": source.get("news_count") or 0,
                        "filtered_count": source.get("filtered_count") or 0,
                        "heat_30d": source.get("heat_30d") or 0,
                        "first_seen_ms": source.get("first_seen_ms") or 0,
                        "noise_score": noise_score(source),
                    }
                )
    return out


def chunks(items: List[Dict[str, Any]], size: int) -> List[List[Dict[str, Any]]]:
    if size <= 0:
        return [items] if items else []
    return [items[index : index + size] for index in range(0, len(items), size)]


def build_block_auto_delete_plan(audit_payload: Dict[str, Any], live_records: List[Dict[str, Any]]) -> Dict[str, Any]:
    items = audit_payload.get("items") if isinstance(audit_payload, dict) else []
    if not isinstance(items, list):
        items = []
    block_ids = {
        str(item.get("record_id") or "").strip()
        for item in items
        if isinstance(item, dict) and str(item.get("decision") or "").strip() == "block_auto"
    }
    review_count = sum(
        1 for item in items if isinstance(item, dict) and str(item.get("decision") or "").strip() == "review"
    )
    live_by_id = {str(record.get("record_id") or "").strip(): record for record in live_records}
    to_delete: List[Dict[str, Any]] = []
    skipped_manual: List[Dict[str, Any]] = []
    missing: List[str] = []
    for record_id in sorted(block_ids):
        record = live_by_id.get(record_id)
        if not record:
            missing.append(record_id)
            continue
        fields = record.get("fields") or {}
        item = {
            "record_id": record_id,
            "keyword": rss_ingest.clean_feishu_value(fields.get(config.KEYWORD_FIELD_CANONICAL_NAME)).strip(),
        }
        if is_manual_fields(fields):
            skipped_manual.append(item)
        else:
            to_delete.append(item)
    return {
        "block_auto_count": len(block_ids),
        "review_count": review_count,
        "delete_count": len(to_delete),
        "to_delete": to_delete,
        "skipped_manual": skipped_manual,
        "missing": missing,
    }


def apply_block_auto_delete_report(
    audit_path: Path,
    output_path: Path,
    max_pages: int,
    page_size: int,
    batch_size: int,
) -> Dict[str, Any]:
    audit_payload = json.loads(audit_path.read_text(encoding="utf-8"))
    tenant_token = get_tenant_access_token(config.FEISHU_APP_ID, config.FEISHU_APP_SECRET, config.HTTP_TIMEOUT, config.HTTP_RETRIES)
    live_records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_KEYWORD_TABLE_ID,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=page_size,
        max_pages=max_pages,
    )
    plan = build_block_auto_delete_plan(audit_payload, live_records)
    failed: List[Dict[str, Any]] = []
    deleted = 0
    record_ids = [item["record_id"] for item in plan["to_delete"]]
    for batch in chunks([{"record_id": record_id} for record_id in record_ids], batch_size):
        ids = [item["record_id"] for item in batch]
        ok, data = batch_delete_bitable_records(
            config.FEISHU_APP_TOKEN,
            config.FEISHU_KEYWORD_TABLE_ID,
            tenant_token,
            ids,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
        )
        if ok:
            deleted += len(ids)
        else:
            failed.append({"record_ids": ids, "error": data})
    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "keyword-noise-block-auto-delete",
        **plan,
        "deleted": deleted,
        "failed": failed,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def run(
    provider: str,
    model: str,
    prompt_path: Path,
    output_path: Path,
    limit: int,
    batch_size: int,
    max_pages: int,
    page_size: int,
    incremental_since_ms: int = 0,
    actionable_only: bool = False,
) -> Dict[str, Any]:
    tenant_token = get_tenant_access_token(config.FEISHU_APP_ID, config.FEISHU_APP_SECRET, config.HTTP_TIMEOUT, config.HTTP_RETRIES)
    raw_records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_KEYWORD_TABLE_ID,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=page_size,
        max_pages=max_pages,
    )
    rss_ingest.load_local_prompt_sections()
    records = [keyword_record(record) for record in raw_records]
    recent_records = filter_recent_records(records, incremental_since_ms)
    candidates = select_candidates(recent_records, limit)
    base_prompt = prompt_path.read_text(encoding="utf-8")
    target_provider = rss_ingest.normalize_provider_name(provider)
    target_model = model or rss_ingest.provider_model_for_stage(target_provider, "screen")

    decisions: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for index, batch in enumerate(chunks(candidates, batch_size), start=1):
        prompt = build_actionable_only_prompt(base_prompt, batch) if actionable_only else build_batch_prompt(base_prompt, batch)
        article = {"title": f"KEYWORD noise audit batch {index}", "content": "按提示词审查本批关键词。", "link": "", "source": "keyword-noise-audit"}
        try:
            if target_provider == "gemini":
                raw = analyze_with_gemini_direct(prompt, target_model)
            else:
                raw = rss_ingest.analyze_with_provider_prompt(article, target_provider, prompt, target_model)
            normalized = normalize_llm_items(raw, batch, fill_missing=not actionable_only)
            if actionable_only:
                normalized = [item for item in normalized if item.get("decision") in {"block_auto", "review"}]
            decisions.extend(normalized)
        except Exception as exc:
            errors.append({"batch": index, "error": str(exc)})
            decisions.extend(
                {
                    "record_id": item["record_id"],
                    "keyword": item["keyword"],
                    "type": item.get("type") or "",
                    "decision": "review",
                    "risk": "high",
                    "reason": f"LLM调用失败: {exc}",
                    "news_count": item.get("news_count") or 0,
                    "filtered_count": item.get("filtered_count") or 0,
                    "heat_30d": item.get("heat_30d") or 0,
                    "first_seen_ms": item.get("first_seen_ms") or 0,
                    "noise_score": noise_score(item),
                }
                for item in batch
            )

    counts: Dict[str, int] = {}
    for item in decisions:
        counts[item["decision"]] = counts.get(item["decision"], 0) + 1
    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "keyword-noise-llm-audit-dry-run",
        "provider": target_provider,
        "model": target_model,
        "keyword_scanned": len(records),
        "incremental_since_ms": incremental_since_ms,
        "recent_keyword_count": len(recent_records),
        "candidate_count": len(candidates),
        "actionable_only": actionable_only,
        "decision_counts": counts,
        "errors": errors,
        "items": decisions,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run LLM audit for noisy KEYWORD records.")
    parser.add_argument("--provider", default="gemini")
    parser.add_argument("--model", default="")
    parser.add_argument("--prompt-path", default=str(DEFAULT_PROMPT_PATH))
    parser.add_argument("--output", default="")
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--batch-size", type=int, default=500, help="LLM batch size; <=0 means review all selected candidates in one request.")
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--incremental-since-ms", type=int, default=0)
    parser.add_argument("--actionable-only", action="store_true")
    parser.add_argument("--delete-block-auto-from", default="", help="Apply deletion for block_auto records from an audit JSON report.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    output_path = Path(args.output) if args.output else Path("out") / f"keyword-noise-audit-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
    if args.delete_block_auto_from:
        result = apply_block_auto_delete_report(
            audit_path=Path(args.delete_block_auto_from),
            output_path=output_path,
            max_pages=args.max_pages,
            page_size=args.page_size,
            batch_size=args.batch_size,
        )
        print(json.dumps({k: v for k, v in result.items() if k != "to_delete"}, ensure_ascii=False, indent=2))
        print(f"[keyword-noise-delete] output={output_path}")
        return 1 if result["failed"] else 0
    result = run(
        provider=args.provider,
        model=args.model,
        prompt_path=Path(args.prompt_path),
        output_path=output_path,
        limit=args.limit,
        batch_size=args.batch_size,
        max_pages=args.max_pages,
        page_size=args.page_size,
        incremental_since_ms=args.incremental_since_ms,
        actionable_only=args.actionable_only,
    )
    print(json.dumps({k: v for k, v in result.items() if k != "items"}, ensure_ascii=False, indent=2))
    print(f"[keyword-noise-audit] output={output_path}")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
