# -*- coding: utf-8 -*-
import datetime as dt
import hashlib
import html
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

import requests

import aihot_filter
import config
from http_safety import fetch_public_content
from html_watch import (
    fetch_html_watch,
    is_html_watch_source,
    serialize_watch_state,
    should_fetch_html_watch,
)
from feishu_client import (
    create_bitable_record,
    create_bitable_record_with_id,
    get_tenant_access_token,
    list_bitable_records,
    update_bitable_record_fields,
    upload_bitable_media,
)
from article_extractor import extract_article_text
from rss_parser import build_item_key, entry_published_ts, fetch_feed
from source_runtime import SourceRuntimeConfigError, prepare_sources_for_runtime

FAILED_CATEGORIES = {"调用失败", "调用异常", "解析失败", "JSON解析失败", "异常"}
_KEYWORD_NAME_BLOCKLIST: set = set()
_KEYWORD_NAME_BLOCKED_COUNT = 0
KEYWORD_BLOCKLIST_MARKER = "关键词过滤"
PROMPT_SCREEN_MARKER = "提示词1：筛选、评分、标签"
PROMPT_SUMMARIZE_MARKER = "提示词2：标题、摘要"
KEYWORD_BLOCKLIST_MARKER_ALIASES = (
    KEYWORD_BLOCKLIST_MARKER,
    "KEYWORD_BLOCKLIST",
)
PROMPT_SCREEN_MARKER_ALIASES = (
    PROMPT_SCREEN_MARKER,
    "提示词1:筛选、评分、标签",
    "提示词：筛选、评分、标签",
    "提示词:筛选、评分、标签",
    "PROMPT_SCREEN",
)
PROMPT_SUMMARIZE_MARKER_ALIASES = (
    PROMPT_SUMMARIZE_MARKER,
    "提示词2:标题、摘要",
    "提示词：标题、摘要",
    "提示词:标题、摘要",
    "PROMPT_SUMMARIZE",
)
TWO_STAGE_PROVIDERS = {"gemini", "iflow", "openai", "ark", "deepseek", "zhipu", "ollama"}
DEPRECATED_PROVIDERS: set[str] = set()
_RUN_LOG_LOCK = threading.Lock()


class _TeeStream:
    def __init__(self, original, log_file) -> None:
        self.original = original
        self.log_file = log_file

    @property
    def encoding(self):
        return getattr(self.original, "encoding", None) or "utf-8"

    def write(self, value) -> int:
        text = str(value)
        with _RUN_LOG_LOCK:
            try:
                self.original.write(text)
            except UnicodeEncodeError:
                safe = text.encode(self.encoding, errors="replace").decode(self.encoding, errors="replace")
                self.original.write(safe)
            self.log_file.write(text)
        return len(text)

    def flush(self) -> None:
        with _RUN_LOG_LOCK:
            self.original.flush()
            self.log_file.flush()

    def isatty(self) -> bool:
        return bool(getattr(self.original, "isatty", lambda: False)())

    def fileno(self):
        return self.original.fileno()

    def __getattr__(self, name):
        return getattr(self.original, name)

def log(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe = msg.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe, flush=True)


def audit_llm_http(provider: str, model: str, url: str, status: str, elapsed_ms: int) -> None:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        audit_path = Path(__file__).resolve().parent / "out" / "llm_http_audit.log"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        line = (
            f"{ts}\tprovider={provider}\tmodel={model}\t"
            f"host={parsed.netloc}\tpath={parsed.path}\t"
            f"status={status}\telapsed_ms={elapsed_ms}\n"
        )
        with audit_path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def audit_text_dedup_failure(provider: str, title: str, reason: str) -> None:
    try:
        audit_path = Path(__file__).resolve().parent / "out" / "text_dedup_failures.log"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        line = (
            f"{ts}\tprovider={provider}\t"
            f"title={truncate_text(title, 120)}\t"
            f"reason={truncate_text(reason, 200)}\n"
        )
        with audit_path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def audit_llm_parse_failure(
    provider: str,
    model: str,
    article: Dict[str, Any],
    parse_error: str,
    diagnostics: str,
    raw_text: str,
) -> None:
    try:
        audit_path = Path(__file__).resolve().parent / "out" / "llm_parse_failures.log"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        line = (
            f"{ts}\tprovider={provider}\tmodel={model}\t"
            f"source={truncate_text(article.get('source') or '', 80)}\t"
            f"title={truncate_text(article.get('title') or '', 120)}\t"
            f"link={truncate_text(article.get('link') or '', 160)}\t"
            f"error={truncate_text(parse_error, 120)}\t"
            f"{diagnostics}\t"
            f"raw={truncate_text(raw_text, 200)}\n"
        )
        with audit_path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def default_ingest_lock_path() -> Path:
    return Path(__file__).resolve().parent / "out" / "rss_ingest.lock"


def _read_lock_pid(path: Path) -> Optional[int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pid = int(data.get("pid") or 0)
        return pid if pid > 0 else None
    except Exception:
        return None


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform.startswith("win"):
        try:
            import ctypes

            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, int(pid))
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _lock_file_is_stale(path: Path, invalid_stale_seconds: int = 60) -> bool:
    pid = _read_lock_pid(path)
    if pid is not None:
        return not _pid_exists(pid)
    try:
        return time.time() - path.stat().st_mtime > invalid_stale_seconds
    except OSError:
        return False


@dataclass
class SingleInstanceLock:
    path: Path
    acquired: bool = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "started_at": dt.datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if _lock_file_is_stale(self.path):
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        continue
                    except OSError:
                        return False
                    continue
                return False
            try:
                os.write(fd, payload)
            except Exception:
                try:
                    os.close(fd)
                finally:
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                raise
            os.close(fd)
            self.acquired = True
            return True

    def release(self) -> None:
        if not self.acquired:
            return
        self.acquired = False
        try:
            if _read_lock_pid(self.path) == os.getpid():
                self.path.unlink()
        except OSError:
            pass


def run_with_single_instance_lock(
    run_func: Optional[Callable[[], Any]] = None,
    lock_path: Optional[Path] = None,
) -> int:
    target = run_func or main
    lock = SingleInstanceLock(Path(lock_path) if lock_path else default_ingest_lock_path())
    if not lock.acquire():
        log(f"[RSS] another rss_ingest.py is already running; skip this run lock={lock.path}")
        return 0
    try:
        result = target()
        return result if isinstance(result, int) else 0
    finally:
        lock.release()


def collect_queue_items(items: Iterable[dict], existing_keys: set) -> list:
    out = []
    for item in items:
        key = item.get("item_key")
        if not key or key in existing_keys:
            continue
        out.append(item)
    return out


def render_progress(done: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "0/0 [" + "".ljust(width, ".") + "]"
    filled = int(width * done / total)
    return f"{done}/{total} [" + "#" * filled + "." * (width - filled) + "]"


ROOT_CAUSE_RECORDED = False
ROOT_CAUSE_LOCK = threading.Lock()
NOTIFY_TENANT_TOKEN: Optional[str] = None
FEISHU_TEXT_CELL_SAFE_LIMIT = 80000


def set_notify_tenant_token(token: str) -> None:
    global NOTIFY_TENANT_TOKEN
    NOTIFY_TENANT_TOKEN = token


def truncate_text(text: str, limit: int = 1000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 3] + "..."


def limit_prompt_text(text: Any, limit: int) -> str:
    raw = str(text or "")
    max_len = max(1, int(limit or 0))
    if len(raw) <= max_len:
        return raw
    omitted = len(raw) - max_len
    return f"{raw[:max_len]}\n...[truncated {omitted} chars]"


def limit_feishu_cell_text(text: Any, limit: int = FEISHU_TEXT_CELL_SAFE_LIMIT) -> str:
    raw = str(text or "")
    max_len = max(1, int(limit or 0))
    if len(raw) <= max_len:
        return raw
    marker = f"\n...[truncated {len(raw) - max_len} chars for Feishu cell limit]"
    return raw[: max(0, max_len - len(marker))].rstrip() + marker


def find_marker_match(text: str, aliases: tuple[str, ...]) -> Optional[re.Match[str]]:
    candidates: List[re.Match[str]] = []
    for marker in aliases:
        match = re.search(rf"(?m)^\s*{re.escape(marker)}\s*$", text)
        if match:
            candidates.append(match)
    if not candidates:
        return None
    return min(candidates, key=lambda item: item.start())


def parse_prompt_sections(raw_content: str) -> Dict[str, Any]:
    text = str(raw_content or "").replace("\r\n", "\n")
    screen_match = find_marker_match(text, PROMPT_SCREEN_MARKER_ALIASES)
    summarize_match = find_marker_match(text, PROMPT_SUMMARIZE_MARKER_ALIASES)
    if not screen_match or not summarize_match:
        raise ValueError(
            "prompt document must contain marker lines: "
            f"{PROMPT_SCREEN_MARKER} and {PROMPT_SUMMARIZE_MARKER}"
        )
    if summarize_match.start() <= screen_match.end():
        raise ValueError(f"{PROMPT_SUMMARIZE_MARKER} must appear after {PROMPT_SCREEN_MARKER}")

    blocklist_keywords: List[str] = []
    blocklist_match = find_marker_match(text, KEYWORD_BLOCKLIST_MARKER_ALIASES)
    if blocklist_match and blocklist_match.start() < screen_match.start():
        raw_block = text[blocklist_match.end():screen_match.start()]
        for raw_line in raw_block.splitlines():
            keyword = raw_line.strip()
            if keyword.startswith(("-", "*")):
                keyword = keyword[1:].strip()
            if not keyword or keyword.startswith("#"):
                continue
            if keyword not in blocklist_keywords:
                blocklist_keywords.append(keyword)

    screen_prompt = text[screen_match.end():summarize_match.start()].strip()
    summarize_prompt = text[summarize_match.end():].strip()
    if not screen_prompt:
        raise ValueError(f"{PROMPT_SCREEN_MARKER} section is empty")
    if not summarize_prompt:
        raise ValueError(f"{PROMPT_SUMMARIZE_MARKER} section is empty")

    return {
        "keyword_blocklist": blocklist_keywords,
        "screen_prompt": screen_prompt,
        "summarize_prompt": summarize_prompt,
    }


def resolve_local_doc_path(path: Any) -> Path:
    doc_path = Path(path)
    if not doc_path.is_absolute():
        doc_path = Path(getattr(config, "BASE_DIR", Path(__file__).resolve().parent)) / doc_path
    return doc_path.resolve()


def _load_keyword_name_blocklist() -> set:
    path = resolve_local_doc_path(config.LOCAL_KEYWORD_NAME_BLOCKLIST_PATH)
    if not path.exists():
        return set()
    raw = path.read_text(encoding="utf-8")
    names: set = set()
    for line in raw.replace("\r\n", "\n").splitlines():
        word = line.strip()
        if word.startswith(("-", "*")):
            word = word[1:].strip()
        if not word or word.startswith("#"):
            continue
        names.add(unicodedata.normalize("NFKC", word).strip().lower())
    return names


def parse_keyword_blocklist(raw_content: str) -> List[str]:
    keywords: List[str] = []
    text = str(raw_content or "").replace("\r\n", "\n")
    for raw_line in text.splitlines():
        keyword = raw_line.strip()
        if keyword.startswith(("-", "*")):
            keyword = keyword[1:].strip()
        if not keyword or keyword.startswith("#"):
            continue
        if keyword not in keywords:
            keywords.append(keyword)
    return keywords


def load_prompt_text_file(path: Any) -> str:
    prompt_path = resolve_local_doc_path(path)
    content = prompt_path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"prompt file is empty: {prompt_path}")
    return content


def load_local_prompt_sections(path: Any = None) -> Dict[str, Any]:
    if path is not None:
        prompt_path = resolve_local_doc_path(path)
        raw_content = prompt_path.read_text(encoding="utf-8")
        parsed = parse_prompt_sections(raw_content)
        parsed["path"] = str(prompt_path)
        return parsed

    keyword_path = resolve_local_doc_path(config.LOCAL_KEYWORD_BLOCKLIST_PATH)
    screen_path = resolve_local_doc_path(config.LOCAL_SCREEN_PROMPT_PATH)
    summarize_path = resolve_local_doc_path(config.LOCAL_SUMMARIZE_PROMPT_PATH)
    keywords_addendum_raw = str(getattr(config, "LOCAL_SCREEN_KEYWORDS_ADDENDUM_PATH", "") or "").strip()
    keywords_addendum_path = resolve_local_doc_path(keywords_addendum_raw) if keywords_addendum_raw else None

    keyword_blocklist = parse_keyword_blocklist(keyword_path.read_text(encoding="utf-8"))
    keyword_name_blocklist = _load_keyword_name_blocklist()
    screen_prompt = load_prompt_text_file(screen_path)
    summarize_prompt = load_prompt_text_file(summarize_path)
    if keywords_addendum_path is not None:
        keywords_addendum = load_prompt_text_file(keywords_addendum_path)
        screen_prompt = f"{screen_prompt}\n\n{keywords_addendum}"

    return {
        "keyword_blocklist": keyword_blocklist,
        "keyword_name_blocklist": keyword_name_blocklist,
        "screen_prompt": screen_prompt,
        "summarize_prompt": summarize_prompt,
        "keyword_path": str(keyword_path),
        "screen_path": str(screen_path),
        "summarize_path": str(summarize_path),
        "screen_keywords_addendum_path": str(keywords_addendum_path) if keywords_addendum_path else "",
        "path": (
            f"keywords={keyword_path}; screen={screen_path}; "
            f"summarize={summarize_path}; "
            f"screen_keywords_addendum={keywords_addendum_path or ''}"
        ),
    }


def resolve_prompt_config(prompt_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if isinstance(prompt_config, dict):
        return prompt_config
    return load_local_prompt_sections()


def normalize_string_list(value: Any) -> List[str]:
    items = value if isinstance(value, list) else [value]
    normalized: List[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def parse_score(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def find_blocked_keyword(article: Dict[str, Any], keywords: List[str]) -> str:
    if not keywords:
        return ""
    title = str(article.get("title") or "")
    content = clean_html_to_text(str(article.get("content") or ""))
    haystack = f"{title}\n{content}".lower()
    for keyword in keywords:
        normalized = str(keyword or "").strip()
        if normalized and normalized.lower() in haystack:
            return normalized
    return ""


def attach_llm_meta(analysis: Dict[str, Any], **meta: Any) -> Dict[str, Any]:
    out = dict(analysis)
    existing = out.get("_llm_meta")
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update(meta)
    out["_llm_meta"] = merged
    return out


def get_llm_meta(analysis: Dict[str, Any]) -> Dict[str, Any]:
    meta = analysis.get("_llm_meta")
    return dict(meta) if isinstance(meta, dict) else {}


def try_mark_root_cause_recorded() -> bool:
    global ROOT_CAUSE_RECORDED
    with ROOT_CAUSE_LOCK:
        if ROOT_CAUSE_RECORDED:
            return False
        ROOT_CAUSE_RECORDED = True
        return True


def _validate_keywords(raw: Any, *, allow_empty: bool = False) -> List[Dict[str, str]]:
    if raw is None:
        raise ValueError("missing keywords")
    if not isinstance(raw, list):
        raise ValueError("keywords must be list")
    if len(raw) > 3 or (not allow_empty and len(raw) < 1):
        raise ValueError("keywords count out of range")

    out: List[Dict[str, str]] = []
    skipped_too_long = False
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("keyword item must be dict")
        name = str(item.get("name") or "").strip()
        type_ = str(item.get("type") or "").strip().lower()
        if not name:
            raise ValueError("keyword name empty")
        if type_ not in config.KEYWORD_TYPE_OPTIONS:
            raise ValueError(f"keyword type invalid: {type_ or 'empty'}")
        if len(name) > 20:
            skipped_too_long = True
            continue
        out.append({"name": name, "type": type_})
    if not out:
        if allow_empty:
            return []
        if skipped_too_long:
            raise ValueError("keyword name too long")
        raise ValueError("missing keywords")
    return out


def _validate_categories(raw: Any) -> List[str]:
    categories = normalize_string_list(raw or [])
    if not categories:
        raise ValueError("missing categories")

    allowed = set(config.NEWS_CATEGORY_OPTIONS)
    invalid = [category for category in categories if category not in allowed]
    if invalid:
        raise ValueError(f"invalid categories: {', '.join(invalid)}")
    return categories[:3]


def _is_retryable_screen_validation_error(exc: ValueError) -> bool:
    message = str(exc)
    return (
        message.startswith("invalid action:")
        or message.startswith("missing reason")
        or message.startswith("missing keywords")
        or message.startswith("keywords must be list")
        or message.startswith("keywords count out of range")
        or message.startswith("keyword item must be dict")
        or message.startswith("keyword name empty")
        or message.startswith("keyword name too long")
        or message.startswith("keyword type invalid:")
        or message.startswith("invalid categories:")
        or message.startswith("invalid score")
        or message.startswith("missing title_zh")
        or message.startswith("missing summary")
        or message.startswith("missing brief_summary")
    )


def _screen_retry_prompt(screen_prompt: str, error: ValueError) -> str:
    return (
        f"{screen_prompt}\n\n"
        "【格式重试】\n"
        f"上一轮输出未通过系统校验：{error}\n"
        "请重新输出，只返回一个合法 JSON 对象，不要 Markdown，不要解释文字。\n"
        "必须严格包含当前 action 对应的字段；ingest/pass 都必须包含 reason、title_zh、summary、keywords；"
        "ingest 的 keywords 必须是 1-3 个对象，pass 允许 0-3 个对象；每个对象包含 name 和 type。"
    )


def screen_fact_summary(analysis: Dict[str, Any]) -> str:
    return (
        str(analysis.get("summary") or "").strip()
        or str(analysis.get("brief_summary") or "").strip()
    )


def validate_screen_result(analysis: Dict[str, Any]) -> Dict[str, Any]:
    action = str(analysis.get("action") or "").strip().lower()
    if action not in ("ingest", "pass"):
        raise ValueError(f"invalid action: {action or 'empty'}")

    reason = str(analysis.get("reason") or "").strip()
    if not reason:
        raise ValueError("missing reason")

    keywords = _validate_keywords(analysis.get("keywords"), allow_empty=action == "pass")

    if action == "pass":
        result: Dict[str, Any] = {"action": "pass", "reason": reason, "keywords": keywords}
        title_zh = str(analysis.get("title_zh") or "").strip()
        if not title_zh:
            raise ValueError("missing title_zh")
        result["title_zh"] = title_zh
        summary = str(analysis.get("summary") or "").strip()
        if not summary:
            raise ValueError("missing summary")
        result["summary"] = summary
        return result

    title_zh = str(analysis.get("title_zh") or "").strip()
    if not title_zh:
        raise ValueError("missing title_zh")

    summary = screen_fact_summary(analysis)
    if not summary:
        raise ValueError("missing summary")

    result: Dict[str, Any] = {
        "action": "ingest",
        "reason": reason,
        "title_zh": title_zh,
        "keywords": keywords,
        "summary": summary,
        "brief_summary": summary,
    }
    raw_categories = analysis.get("categories")
    if raw_categories not in (None, "", []):
        result["categories"] = _validate_categories(raw_categories)

    raw_score = analysis.get("score")
    if raw_score not in (None, ""):
        score = parse_score(raw_score)
        if score is None or score < 0 or score > 10:
            raise ValueError("invalid score")
        result["score"] = score

    raw_qa = normalize_qa(analysis.get("qa") or [])
    if raw_qa:
        result["qa"] = raw_qa

    return result


def validate_summary_result(analysis: Dict[str, Any]) -> Dict[str, Any]:
    qa = normalize_qa(analysis.get("qa") or [])
    if not qa:
        raise ValueError("missing qa")
    if len(qa) < 3:
        raise ValueError("qa must contain at least 3 items")

    return {
        "qa": qa[:5],
    }

def build_plain_notice(error_type: str) -> str:
    if error_type == "auth":
        return "鉴权失败：API Key 无效/过期/权限不足，请更新后重试。"
    if error_type == "rate_limit":
        return "触发限流或配额不足：请降低频率或检查配额。"
    if error_type == "server_error":
        return "上游服务异常：请稍后重试。"
    if error_type == "timeout":
        return "网络超时：请检查网络/代理设置。"
    if error_type == "parse_error":
        return "输出格式异常：请检查提示词或更换模型。"
    if error_type == "config":
        return "关键配置缺失：请检查 Secrets/环境变量是否完整。"
    return "未知错误：请查看详情并排查配置。"



def notify_root_cause(event: str, detail: str, error_type: str = "unknown") -> None:
    if not try_mark_root_cause_recorded():
        return

    if not config.FEISHU_NOTIFY_TABLE_ID:
        log("[Notify] skipped: missing FEISHU_NOTIFY_TABLE_ID")
        return
    if not NOTIFY_TENANT_TOKEN:
        log("[Notify] skipped: missing tenant token")
        return

    plain_text = build_plain_notice(error_type)
    fields = {
        config.NOTIFY_FIELD_EVENT: event,
        config.NOTIFY_FIELD_DETAIL: truncate_text(detail.strip() or event),
        config.NOTIFY_FIELD_PLAIN: plain_text,
        config.NOTIFY_FIELD_TRIGGER_TIME: int(time.time() * 1000),
        config.NOTIFY_FIELD_NOTIFIED: False,
    }
    ok = create_bitable_record(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_NOTIFY_TABLE_ID,
        NOTIFY_TENANT_TOKEN,
        fields,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )
    if not ok:
        log("[Notify] create record failed")


def notify_auth_failure(service: str, detail: str) -> None:
    notify_root_cause(f"{service} 鉴权失败", detail, "auth")


def notify_rate_limit(service: str, detail: str) -> None:
    notify_root_cause(f"{service} 请求失败", detail, "rate_limit")


def notify_server_error(service: str, detail: str) -> None:
    notify_root_cause(f"{service} 请求失败", detail, "server_error")


def notify_timeout(service: str, detail: str) -> None:
    notify_root_cause(f"{service} 请求失败", detail, "timeout")


def notify_parse_error(service: str, detail: str) -> None:
    notify_root_cause(f"{service} 输出解析失败", detail, "parse_error")


def notify_config_missing(detail: str) -> None:
    notify_root_cause("关键配置缺失", detail, "config")


def response_snippet(resp: requests.Response) -> str:
    try:
        text = resp.text or ""
    except Exception:
        return f"HTTP {resp.status_code}"
    return f"HTTP {resp.status_code}: {truncate_text(text.strip(), 300)}"


def _http_post(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout: int,
    *,
    audit_provider: str = "",
    audit_model: str = "",
) -> requests.Response:
    started = time.time()
    try:
        if config.USE_SYSTEM_PROXY:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        else:
            with requests.Session() as sess:
                sess.trust_env = False
                resp = sess.post(url, headers=headers, json=payload, timeout=timeout)
        audit_llm_http(audit_provider, audit_model, url, str(resp.status_code), int((time.time() - started) * 1000))
        return resp
    except Exception as exc:
        audit_llm_http(
            audit_provider,
            audit_model,
            url,
            f"exception:{type(exc).__name__}",
            int((time.time() - started) * 1000),
        )
        raise


def clean_feishu_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        if "text" in value and isinstance(value["text"], str):
            return value["text"]
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                t = item.get("text")
                parts.append(t if isinstance(t, str) else str(item))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(value)


def is_checked(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "yes", "y", "1", "checked", "on"):
            return True
        if s in ("false", "no", "n", "0", ""):
            return False
        return True
    if isinstance(value, list):
        return len(value) > 0
    if isinstance(value, dict):
        return True
    return bool(value)


def parse_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        s = str(value).strip()
        return int(s) if s else None
    except Exception:
        return None


def parse_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip()
        return float(s) if s else None
    except Exception:
        return None


def parse_ts_ms(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    fmts = ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]
    for fmt in fmts:
        try:
            dt_obj = dt.datetime.strptime(s, fmt)
            return int(dt_obj.timestamp() * 1000)
        except Exception:
            continue
    return 0


def clean_html_to_text(html: str) -> str:
    if not html:
        return ""
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p\s*>", "\n", html)
    html = re.sub(r"(?i)</div\s*>", "\n", html)
    html = re.sub(r"(?i)</li\s*>", "\n", html)
    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


_IMG_SRC_RE = re.compile(r"""<img\b[^>]*(?:src|data-src)=["']([^"']+)["']""", re.IGNORECASE)
_RAW_IMAGE_URL_RE = re.compile(
    r"https?://[^\s\"'<>)]+(?:\.(?:png|jpe?g|webp|gif)(?:\?[^\s\"'<>)]*)?|[^\s\"'<>)]+)",
    re.IGNORECASE,
)
_IMAGE_URL_HINT_RE = re.compile(
    r"(?:\.(?:png|jpe?g|webp|gif)(?:\?|$)|pbs\.twimg\.com|breakout-|article-images\.zsxq|mmbiz|qpic)",
    re.IGNORECASE,
)


def dedupe_strings(values: Iterable[Any]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def is_aipoju_article(article: Dict[str, Any]) -> bool:
    text = " ".join(
        str(article.get(key) or "")
        for key in ("title", "source", "link")
    ).lower()
    return "aipoju.com/topic-details/" in text or "ai破局" in text or "ai赚钱频道" in text


def _x_status_ref(url: str) -> Tuple[str, str]:
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    if host not in {"x.com", "twitter.com", "mobile.twitter.com", "www.x.com", "www.twitter.com"}:
        return "", ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 3 and parts[-2] in {"status", "statuses"}:
        status_id = re.sub(r"\D+", "", parts[-1])
        handle = parts[-3] if len(parts) >= 3 else "i"
        if handle in {"i", "web"} and len(parts) >= 4:
            handle = "i"
        return handle or "i", status_id
    if len(parts) >= 4 and parts[0] == "i" and parts[1] == "web" and parts[2] == "status":
        return "i", re.sub(r"\D+", "", parts[3])
    return "", ""


def is_x_article(article: Dict[str, Any]) -> bool:
    handle, status_id = _x_status_ref(str(article.get("link") or ""))
    return bool(handle and status_id)


def is_reddit_article(article: Dict[str, Any]) -> bool:
    parsed = urlparse(str(article.get("link") or ""))
    host = (parsed.hostname or "").lower()
    if host not in {"reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com"}:
        return False
    parts = [part for part in parsed.path.split("/") if part]
    return len(parts) >= 4 and parts[0].lower() == "r" and parts[2].lower() == "comments"


def _entry_html_parts(entry: Optional[Dict[str, Any]]) -> List[str]:
    if not entry:
        return []
    parts: List[str] = []
    for key in ("summary", "description"):
        value = entry.get(key)
        if value:
            parts.append(str(value))
    content = entry.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                value = item.get("value")
            else:
                value = item
            if value:
                parts.append(str(value))
    for key in ("content:encoded", "content_encoded", "encoded"):
        value = entry.get(key)
        if value:
            parts.append(str(value))
    return parts


def _extract_image_urls_from_html(raw_html: str, base_url: str = "") -> List[str]:
    urls: List[str] = []
    for match in _IMG_SRC_RE.finditer(raw_html or ""):
        src = html.unescape(match.group(1)).strip()
        if src:
            urls.append(urljoin(base_url, src) if base_url else src)
    for match in _RAW_IMAGE_URL_RE.finditer(html.unescape(raw_html or "")):
        url = match.group(0).rstrip(".,;，。；)")
        if _IMAGE_URL_HINT_RE.search(url):
            urls.append(url)
    return dedupe_strings(urls)


def entry_image_urls(entry: Optional[Dict[str, Any]], base_url: str = "") -> List[str]:
    if not entry:
        return []
    urls: List[str] = []
    for key in ("media_content", "media_thumbnail"):
        for item in entry.get(key) or []:
            if isinstance(item, dict):
                value = item.get("url") or item.get("href")
                if value:
                    urls.append(str(value))
    for item in entry.get("enclosures") or []:
        if isinstance(item, dict):
            value = item.get("href") or item.get("url")
            kind = str(item.get("type") or "")
            if value and ("image" in kind.lower() or _IMAGE_URL_HINT_RE.search(str(value))):
                urls.append(str(value))
    for part in _entry_html_parts(entry):
        urls.extend(_extract_image_urls_from_html(part, base_url=base_url))
    return dedupe_strings(urls)


def fetch_x_media_urls(url: str) -> List[str]:
    handle, status_id = _x_status_ref(url)
    if not status_id:
        return []
    api_url = f"https://api.fxtwitter.com/{handle or 'i'}/status/{status_id}"
    resp = requests.get(
        api_url,
        timeout=max(1, int(getattr(config, "IMAGE_ATTACHMENT_TIMEOUT", 12) or 12)),
        headers={"User-Agent": "rss-ingest-image-attachments/1.0"},
    )
    if resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except Exception:
        return []
    tweet = data.get("tweet") if isinstance(data, dict) and data.get("code") == 200 else None
    if not isinstance(tweet, dict):
        return []
    media = tweet.get("media") or {}
    urls: List[str] = []
    for key in ("photos", "all"):
        for item in media.get(key) or []:
            if isinstance(item, dict) and item.get("url"):
                urls.append(str(item["url"]))
    for item in media.get("videos") or []:
        if not isinstance(item, dict):
            continue
        thumb = item.get("thumbnail_url") or item.get("url")
        if thumb:
            urls.append(str(thumb))
    return dedupe_strings(urls)


def collect_article_image_urls(
    article: Dict[str, Any],
    entry: Optional[Dict[str, Any]] = None,
    max_urls: Optional[int] = None,
) -> List[str]:
    limit = max_urls if max_urls is not None else int(getattr(config, "IMAGE_ATTACHMENT_MAX_PER_RECORD", 3) or 3)
    urls: List[str] = []
    existing = article.get("image_urls")
    if isinstance(existing, list):
        urls.extend(str(item) for item in existing)

    if is_aipoju_article(article):
        urls.extend(entry_image_urls(entry, base_url=str(article.get("link") or "")))
    elif is_x_article(article):
        urls.extend(fetch_x_media_urls(str(article.get("link") or "")))
    elif is_reddit_article(article):
        urls.extend(entry_image_urls(entry, base_url=str(article.get("link") or "")))
    else:
        return []

    out = dedupe_strings(urls)
    return out[: max(0, limit)]


def _guess_image_name_and_mime(url: str, content_type: str) -> Tuple[str, str]:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if not mime or "/" not in mime:
        mime = mimetypes.guess_type(urlparse(url).path)[0] or "image/jpeg"
    parsed_name = Path(unquote(urlparse(url).path)).name
    parsed_name = re.sub(r"[^A-Za-z0-9._-]+", "_", parsed_name).strip("._")
    if not parsed_name or "." not in parsed_name:
        ext = mimetypes.guess_extension(mime) or ".jpg"
        digest = hashlib.sha1(url.encode("utf-8", errors="ignore")).hexdigest()[:12]
        parsed_name = f"image-{digest}{ext}"
    return parsed_name[:120], mime


def download_image_for_attachment(url: str) -> Tuple[str, bytes, str]:
    timeout = max(1, int(getattr(config, "IMAGE_ATTACHMENT_TIMEOUT", 12) or 12))
    max_bytes = max(1, int(getattr(config, "IMAGE_ATTACHMENT_MAX_BYTES", 5 * 1024 * 1024) or 1))
    headers = {"User-Agent": "rss-ingest-image-attachments/1.0"}
    resp = fetch_public_content(
        url,
        headers=headers,
        timeout=timeout,
        max_bytes=max_bytes,
        use_system_proxy=bool(config.USE_SYSTEM_PROXY),
        proxy_fake_ip_host_allowlist=getattr(config, "IMAGE_ATTACHMENT_PROXY_FAKE_IP_HOSTS", ()),
    )
    if resp.status_code < 200 or resp.status_code >= 300:
        raise RuntimeError(f"image HTTP {resp.status_code}: {(resp.text or '')[:200]}")
    content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type and not content_type.startswith("image/") and not _IMAGE_URL_HINT_RE.search(url):
        raise RuntimeError(f"unsupported image content type: {content_type}")
    content = resp.content or b""
    file_name, mime = _guess_image_name_and_mime(url, content_type)
    return file_name, content, mime


def upload_article_images_for_attachment(article: Dict[str, Any], tenant_token: str) -> List[str]:
    if not getattr(config, "ENABLE_IMAGE_ATTACHMENTS", True):
        return []
    if not config.FEISHU_APP_TOKEN:
        return []
    urls = collect_article_image_urls(article)
    if not urls:
        return []
    tokens: List[str] = []
    for url in urls:
        try:
            file_name, content, mime_type = download_image_for_attachment(url)
            token = upload_bitable_media(
                config.FEISHU_APP_TOKEN,
                tenant_token,
                file_name,
                content,
                mime_type,
                config.HTTP_TIMEOUT,
                config.HTTP_RETRIES,
            )
            if token:
                tokens.append(token)
        except Exception as exc:
            log(f"[Image] attachment skipped url={url[:120]} error={exc}")
    return dedupe_strings(tokens)


def format_image_attachment_tokens(file_tokens: Optional[List[Any]]) -> List[Dict[str, str]]:
    return [{"file_token": token} for token in dedupe_strings(file_tokens or [])]


def normalize_single_select(value: Any, allowed: set, default: str = "") -> str:
    s = clean_feishu_value(value).strip()
    return s if s in allowed else default


def derive_fetch_status(exc: Exception) -> str:
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg:
        return config.FETCH_STATUS_TIMEOUT
    if "parse" in msg:
        return config.FETCH_STATUS_PARSE_ERROR
    if "http" in msg:
        return config.FETCH_STATUS_HTTP_ERROR
    return config.FETCH_STATUS_HTTP_ERROR


def derive_overall_status(consecutive_fail: int, enabled: bool) -> str:
    if not enabled:
        return config.STATUS_IDLE
    if consecutive_fail >= 5:
        return config.STATUS_DEAD
    if consecutive_fail >= 2:
        return config.STATUS_UNSTABLE
    return config.STATUS_OK


def gemini_headers() -> Dict[str, str]:
    return {"Content-Type": "application/json", "x-goog-api-key": config.GEMINI_API_KEY}


def gemini_api_url(model_name: str) -> str:
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"


def gemini_backend() -> str:
    backend = str(getattr(config, "GEMINI_BACKEND", "") or "").strip().lower()
    return backend if backend in {"developer", "vertex"} else "developer"


def vertex_gemini_model(model_name: str) -> str:
    return str(model_name or getattr(config, "GOOGLE_VERTEX_MODEL", "") or config.GEMINI_MODEL_NAME).strip()


def vertex_gemini_api_url(model_name: str) -> str:
    project = str(getattr(config, "GOOGLE_CLOUD_PROJECT", "") or "").strip()
    location = str(getattr(config, "GOOGLE_CLOUD_LOCATION", "") or "global").strip()
    model = vertex_gemini_model(model_name)
    if not project:
        raise RuntimeError("missing GOOGLE_CLOUD_PROJECT")
    if not location:
        raise RuntimeError("missing GOOGLE_CLOUD_LOCATION")
    endpoint = "https://aiplatform.googleapis.com" if location == "global" else f"https://{location}-aiplatform.googleapis.com"
    return (
        f"{endpoint}/v1/projects/{project}/locations/{location}"
        f"/publishers/google/models/{model}:generateContent"
    )


def google_adc_access_token() -> str:
    try:
        import google.auth
        from google.auth.transport.requests import Request
    except ImportError as exc:
        raise RuntimeError("missing google-auth; install requirements.txt") from exc

    credentials, _project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials.refresh(Request())
    token = str(getattr(credentials, "token", "") or "").strip()
    if not token:
        raise RuntimeError("Google ADC did not return an access token")
    return token


def vertex_gemini_headers() -> Dict[str, str]:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {google_adc_access_token()}"}


def gemini_provider_requires_auth() -> bool:
    return gemini_backend() != "vertex"


def gemini_api_keys_source() -> List[str]:
    if gemini_backend() == "vertex":
        return [""]
    return [config.GEMINI_API_KEY] if config.GEMINI_API_KEY else []


def gemini_auth_missing_message() -> str:
    if gemini_backend() == "vertex":
        return "missing Google ADC / GOOGLE_CLOUD_PROJECT"
    return "missing GEMINI_API_KEY"


def build_gemini_headers(_api_key: str = "") -> Dict[str, str]:
    if gemini_backend() == "vertex":
        return vertex_gemini_headers()
    return gemini_headers()


def build_gemini_url(model_name: str) -> str:
    if gemini_backend() == "vertex":
        return vertex_gemini_api_url(model_name)
    return gemini_api_url(model_name)


def iflow_headers() -> Dict[str, str]:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {config.IFLOW_API_KEY}"}


def openai_headers(api_key: str) -> Dict[str, str]:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}


def deepseek_headers() -> Dict[str, str]:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"}


def zhipu_headers() -> Dict[str, str]:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {config.ZHIPU_API_KEY}"}


def build_prompt(article: Dict[str, Any], system_prompt: str) -> str:
    china_tz = dt.timezone(dt.timedelta(hours=8))
    now = dt.datetime.now(china_tz)
    prompt_title = limit_prompt_text(article.get("title"), config.PROMPT_TITLE_MAX_CHARS)
    prompt_content = limit_prompt_text(article.get("content"), config.PROMPT_CONTENT_MAX_CHARS)
    prompt_text = str(system_prompt or "").strip()
    if not prompt_text:
        raise ValueError("missing system prompt")
    return f"""{prompt_text}

你所处的时间为：{now.year}年{now.month:02d}月

title：{prompt_title}
content：{prompt_content}
"""


def extract_json_object(text: str) -> str:
    if not text:
        return ""
    t = text.strip()
    t = t.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
    first = t.find("{")
    last = t.rfind("}")
    if first != -1 and last != -1 and last > first:
        return t[first:last + 1]
    return t


def _repair_json(s: str) -> str:
    import re as _re
    s = s.rstrip()
    s = _re.sub(r',\s*([}\]])', r'\1', s)
    opens = s.count('{') + s.count('[')
    closes = s.count('}') + s.count(']')
    if opens > closes:
        brace_diff = s.count('{') - s.count('}')
        bracket_diff = s.count('[') - s.count(']')
        s += ']' * max(bracket_diff, 0) + '}' * max(brace_diff, 0)
    return s


def try_parse_llm_json(raw_text: str) -> tuple[Optional[Dict[str, Any]], str]:
    json_str = extract_json_object(raw_text)
    if not json_str:
        return None, "empty json"
    try:
        return json.loads(json_str), ""
    except json.JSONDecodeError:
        pass
    repaired = _repair_json(json_str)
    try:
        return json.loads(repaired), ""
    except json.JSONDecodeError as exc:
        return None, str(exc)


def parse_llm_json(raw_text: str, service: str) -> Optional[Dict[str, Any]]:
    result, error = try_parse_llm_json(raw_text)
    if result is None:
        notify_parse_error(service, error)
        return None
    return result


def get_analysis_action(analysis: Dict[str, Any]) -> str:
    action = str(analysis.get("action") or "").strip().lower()
    if not action:
        return "ingest"
    return action


def has_failed_categories(analysis: Dict[str, Any]) -> bool:
    categories = analysis.get("categories") or []
    return isinstance(categories, list) and any(category in FAILED_CATEGORIES for category in categories)


def mark_analysis_provider(analysis: Dict[str, Any], provider: str) -> Dict[str, Any]:
    analysis["_provider_used"] = provider
    return analysis


def analysis_provider_used(analysis: Dict[str, Any]) -> str:
    return str(analysis.get("_provider_used") or "").strip().lower()


def normalize_provider_name(provider: Optional[str]) -> str:
    normalized = str(provider or "").strip().lower()
    if normalized in DEPRECATED_PROVIDERS:
        return "deepseek"
    if normalized in TWO_STAGE_PROVIDERS:
        return normalized
    return "gemini"


def provider_service_name(provider: str) -> str:
    names = {
        "gemini": "Gemini",
        "iflow": "iFlow",
        "openai": "OpenAI",
        "ark": "Volcengine Ark",
        "deepseek": "DeepSeek",
        "zhipu": "Zhipu",
        "ollama": "Ollama",
    }
    return names.get(provider, provider or "LLM")


def provider_model_for_stage(provider: str, stage: str) -> str:
    normalized_provider = normalize_provider_name(provider)
    normalized_stage = str(stage or "").strip().lower()
    if normalized_provider == "gemini":
        return config.GEMINI_MODEL_NAME
    if normalized_provider == "iflow":
        return config.IFLOW_MODEL
    if normalized_provider == "openai":
        return config.OPENAI_MODEL
    if normalized_provider == "ark":
        return config.ARK_MODEL
    if normalized_provider == "deepseek":
        return config.DEEPSEEK_MODEL
    if normalized_provider == "ollama":
        if normalized_stage == "screen":
            return config.OLLAMA_SCREEN_MODEL or config.OLLAMA_MODEL
        return config.OLLAMA_MODEL
    if normalized_provider == "zhipu":
        return config.ZHIPU_MODEL
    return config.GEMINI_MODEL_NAME


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    display_name: str
    timeout_attr: str
    retries_attr: str
    build_url: Callable[[str], str]
    build_payload: Callable[[str, str], Dict[str, Any]]
    build_headers: Callable[[str], Dict[str, str]]
    extract_text: Callable[[Dict[str, Any]], str]
    api_keys_source: Callable[[], List[str]]
    default_model: Callable[[], str]
    api_key_env: str = ""
    requires_auth: Callable[[], bool] | bool = True
    fail_fast_on_400: bool = False
    log_bad_status: bool = False
    parse_retries_attr: str = ""
    response_cleanup: Optional[Callable[[str], str]] = None


def _failed_call(summary: str = "") -> Dict[str, Any]:
    return build_failed_analysis(summary, category="调用失败")


def _failed_exception(summary: str) -> Dict[str, Any]:
    return build_failed_analysis(summary, category="调用异常")


def _extract_gemini_text(data: Dict[str, Any]) -> str:
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


def _extract_openai_compat_text(data: Dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return (message.get("content") or "").strip()


def _openai_compat_response_diagnostics(data: Dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return "choices=0"
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") or {}
    content = message.get("content") or ""
    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    tool_calls = message.get("tool_calls") or []
    usage = truncate_text(json.dumps(data.get("usage") or {}, ensure_ascii=False), 160)
    return (
        f"finish_reason={choice.get('finish_reason') or ''} "
        f"content_len={len(content)} reasoning_len={len(reasoning)} "
        f"tool_calls={len(tool_calls) if isinstance(tool_calls, list) else 1} "
        f"usage={usage}"
    )


def _openai_compat_finish_reason(data: Dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    return str(choices[0].get("finish_reason") or "").strip().lower()


def _extract_openai_responses_text(data: Dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str) and data["output_text"]:
        return data["output_text"]
    parts: List[str] = []
    outputs = data.get("output") or []
    if isinstance(outputs, list):
        for item in outputs:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("text"), str):
                parts.append(item["text"])
            content = item.get("content") or []
            if isinstance(content, list):
                for piece in content:
                    if isinstance(piece, dict):
                        text = piece.get("text")
                        if isinstance(text, str):
                            parts.append(text)
    return "".join(parts)



def _bearer_headers(api_key: str) -> Dict[str, str]:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}


def build_gemini_payload(prompt: str, model: str = "") -> Dict[str, Any]:
    generation_config: Dict[str, Any] = {
        "responseMimeType": "application/json",
        "maxOutputTokens": int(getattr(config, "GEMINI_MAX_OUTPUT_TOKENS", 65536) or 65536),
    }
    thinking_level = str(getattr(config, "GEMINI_THINKING_LEVEL", "") or "").strip()
    if thinking_level:
        generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level}
    return {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }


def build_ark_payload(prompt: str, model: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    if (
        getattr(config, "ARK_DISABLE_THINKING", True)
        and str(model or "").strip().lower().startswith("deepseek-v4")
    ):
        payload["thinking"] = {"type": "disabled"}
    return payload


PROVIDERS: Dict[str, ProviderSpec] = {
    "gemini": ProviderSpec(
        name="gemini",
        display_name="Gemini",
        api_key_env="GEMINI_API_KEY",
        timeout_attr="GEMINI_TIMEOUT",
        retries_attr="GEMINI_RETRIES",
        fail_fast_on_400=True,
        build_url=build_gemini_url,
        build_payload=build_gemini_payload,
        build_headers=build_gemini_headers,
        extract_text=_extract_gemini_text,
        api_keys_source=gemini_api_keys_source,
        default_model=lambda: getattr(config, "GOOGLE_VERTEX_MODEL", "") if gemini_backend() == "vertex" else config.GEMINI_MODEL_NAME,
        requires_auth=gemini_provider_requires_auth,
    ),
    "iflow": ProviderSpec(
        name="iflow",
        display_name="iFlow",
        api_key_env="IFLOW_API_KEY",
        timeout_attr="IFLOW_TIMEOUT",
        retries_attr="IFLOW_RETRIES",
        fail_fast_on_400=True,
        log_bad_status=True,
        build_url=lambda _model: f"{config.IFLOW_BASE_URL}/chat/completions",
        build_payload=lambda prompt, model: {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        },
        build_headers=lambda _key: iflow_headers(),
        extract_text=_extract_openai_compat_text,
        api_keys_source=lambda: [config.IFLOW_API_KEY] if config.IFLOW_API_KEY else [],
        default_model=lambda: config.IFLOW_MODEL,
    ),
    "openai": ProviderSpec(
        name="openai",
        display_name="OpenAI",
        api_key_env="OPENAI_API_KEY",
        timeout_attr="OPENAI_TIMEOUT",
        retries_attr="OPENAI_RETRIES",
        build_url=lambda _model: f"{config.OPENAI_BASE_URL}/responses",
        build_payload=lambda prompt, model: {"model": model, "input": prompt},
        build_headers=_bearer_headers,
        extract_text=_extract_openai_responses_text,
        api_keys_source=lambda: [config.OPENAI_API_KEY] if config.OPENAI_API_KEY else [],
        default_model=lambda: config.OPENAI_MODEL,
    ),
    "ark": ProviderSpec(
        name="ark",
        display_name="Volcengine Ark",
        api_key_env="ARK_API_KEY",
        timeout_attr="ARK_TIMEOUT",
        retries_attr="ARK_RETRIES",
        build_url=lambda _model: f"{config.ARK_BASE_URL}/chat/completions",
        build_payload=build_ark_payload,
        build_headers=_bearer_headers,
        extract_text=_extract_openai_compat_text,
        api_keys_source=lambda: [config.ARK_API_KEY] if config.ARK_API_KEY else [],
        default_model=lambda: config.ARK_MODEL,
        parse_retries_attr="ARK_PARSE_RETRIES",
    ),
    "deepseek": ProviderSpec(
        name="deepseek",
        display_name="DeepSeek",
        api_key_env="DEEPSEEK_API_KEY",
        timeout_attr="DEEPSEEK_TIMEOUT",
        retries_attr="DEEPSEEK_RETRIES",
        build_url=lambda _model: f"{config.DEEPSEEK_BASE_URL}/chat/completions",
        build_payload=lambda prompt, model: {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "response_format": {"type": "json_object"},
        },
        build_headers=lambda _key: deepseek_headers(),
        extract_text=_extract_openai_compat_text,
        api_keys_source=lambda: [config.DEEPSEEK_API_KEY] if config.DEEPSEEK_API_KEY else [],
        default_model=lambda: config.DEEPSEEK_MODEL,
    ),
    "ollama": ProviderSpec(
        name="ollama",
        display_name="Ollama",
        api_key_env="OLLAMA_API_KEY",
        timeout_attr="OLLAMA_TIMEOUT",
        retries_attr="OLLAMA_RETRIES",
        build_url=lambda _model: f"{config.OLLAMA_BASE_URL}/chat/completions",
        build_payload=lambda prompt, model: {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "response_format": {"type": "json_object"},
        },
        build_headers=_bearer_headers,
        extract_text=_extract_openai_compat_text,
        api_keys_source=lambda: [config.OLLAMA_API_KEY] if config.OLLAMA_API_KEY else [],
        default_model=lambda: config.OLLAMA_MODEL,
        requires_auth=False,
    ),
    "zhipu": ProviderSpec(
        name="zhipu",
        display_name="Zhipu",
        api_key_env="ZHIPU_API_KEY",
        timeout_attr="ZHIPU_TIMEOUT",
        retries_attr="ZHIPU_RETRIES",
        build_url=lambda _model: f"{config.ZHIPU_BASE_URL}/chat/completions",
        build_payload=lambda prompt, model: {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        },
        build_headers=lambda _key: zhipu_headers(),
        extract_text=_extract_openai_compat_text,
        api_keys_source=lambda: [config.ZHIPU_API_KEY] if config.ZHIPU_API_KEY else [],
        default_model=lambda: config.ZHIPU_MODEL,
    ),
}


def _run_provider_chat(
    spec: ProviderSpec,
    article: Dict[str, Any],
    system_prompt: str,
    model_name: Optional[str] = None,
    *,
    fixed_api_key: Optional[str] = None,
    retry_limit: Optional[int] = None,
    suppress_notify: bool = False,
) -> Dict[str, Any]:
    if fixed_api_key is not None:
        api_keys = [fixed_api_key]
    else:
        api_keys = spec.api_keys_source()

    requires_auth = spec.requires_auth() if callable(spec.requires_auth) else spec.requires_auth
    if requires_auth and not api_keys:
        missing_message = gemini_auth_missing_message() if spec.name == "gemini" else f"missing {spec.api_key_env}"
        notify_auth_failure(spec.display_name, missing_message)
        return _failed_call(missing_message)

    if not api_keys:
        api_keys = [""]

    target_model = model_name or spec.default_model()
    prompt = build_prompt(article, system_prompt)
    payload = spec.build_payload(prompt, target_model)
    url = spec.build_url(target_model)
    timeout = getattr(config, spec.timeout_attr)

    if retry_limit is not None:
        http_retry_limit = max(1, retry_limit)
    else:
        http_retry_limit = max(1, getattr(config, spec.retries_attr))
    parse_retry_limit = max(1, getattr(config, spec.parse_retries_attr) if spec.parse_retries_attr else 1)

    last_err: Optional[Exception] = None
    last_status_type: Optional[str] = None
    last_status_detail = ""
    http_retry_count = 0
    parse_fail_count = 0

    while True:
        api_key = api_keys[0] if len(api_keys) == 1 else api_keys[http_retry_count % len(api_keys)]
        try:
            resp = _http_post(
                url,
                headers=spec.build_headers(api_key),
                payload=payload,
                timeout=timeout,
                audit_provider=spec.name,
                audit_model=target_model,
            )

            if resp.status_code in (401, 403):
                if not suppress_notify:
                    notify_auth_failure(spec.display_name, response_snippet(resp))
                return _failed_call()

            if spec.fail_fast_on_400 and resp.status_code == 400:
                return _failed_call()

            if resp.status_code in (429, 500, 502, 503, 504):
                last_status_type = "rate_limit" if resp.status_code == 429 else "server_error"
                last_status_detail = response_snippet(resp)
                http_retry_count += 1
                if http_retry_count >= http_retry_limit:
                    break

                time.sleep(1.2 * http_retry_count)
                continue

            if resp.status_code != 200:
                if spec.log_bad_status:
                    log(f"[{spec.display_name}] bad status: {response_snippet(resp)}")
                return _failed_call()

            response_data = resp.json()
            raw_text = spec.extract_text(response_data)
            if spec.response_cleanup is not None:
                raw_text = spec.response_cleanup(raw_text)

            result, parse_error = try_parse_llm_json(raw_text)
            if result is None:
                parse_fail_count += 1
                diagnostics = _openai_compat_response_diagnostics(response_data)
                audit_llm_parse_failure(
                    spec.name,
                    target_model,
                    article,
                    parse_error,
                    diagnostics,
                    raw_text,
                )
                log(
                    f"[{spec.display_name}] parse failed, raw={truncate_text(raw_text, 300)} "
                    f"{diagnostics}"
                )
                if _openai_compat_finish_reason(response_data) == "content_filter":
                    return _failed_call("content_filter")
                if parse_fail_count < parse_retry_limit:
    
                    time.sleep(0.8 * parse_fail_count)
                    continue
                if not suppress_notify:
                    notify_parse_error(spec.display_name, f"{parse_error}; {diagnostics}")
                return _failed_call()
            return result
        except Exception as exc:
            last_err = exc
            if "timeout" in str(exc).lower():
                last_status_type = "timeout"
            http_retry_count += 1
            if http_retry_count >= http_retry_limit:
                break
            time.sleep(1.0 + http_retry_count)

    if not suppress_notify:
        if last_status_type == "rate_limit":
            notify_rate_limit(spec.display_name, last_status_detail or "HTTP 429")
        elif last_status_type == "server_error":
            notify_server_error(spec.display_name, last_status_detail or "HTTP 5xx")
        elif last_status_type == "timeout":
            notify_timeout(spec.display_name, str(last_err) if last_err else "timeout")
    return _failed_exception(str(last_err) if last_err else "")


def analyze_with_gemini_prompt(
    article: Dict[str, Any],
    system_prompt: str,
    model_name: Optional[str] = None,
) -> Dict[str, Any]:
    return _run_provider_chat(PROVIDERS["gemini"], article, system_prompt, model_name)


def analyze_with_iflow_prompt(
    article: Dict[str, Any],
    system_prompt: str,
    model_name: Optional[str] = None,
) -> Dict[str, Any]:
    return _run_provider_chat(PROVIDERS["iflow"], article, system_prompt, model_name)


def analyze_with_openai_prompt(
    article: Dict[str, Any],
    system_prompt: str,
    model_name: Optional[str] = None,
) -> Dict[str, Any]:
    return _run_provider_chat(PROVIDERS["openai"], article, system_prompt, model_name)


def analyze_with_ark_prompt(
    article: Dict[str, Any],
    system_prompt: str,
    model_name: Optional[str] = None,
) -> Dict[str, Any]:
    return _run_provider_chat(PROVIDERS["ark"], article, system_prompt, model_name)


def analyze_with_deepseek_prompt(
    article: Dict[str, Any],
    system_prompt: str,
    model_name: Optional[str] = None,
) -> Dict[str, Any]:
    return _run_provider_chat(PROVIDERS["deepseek"], article, system_prompt, model_name)


def analyze_with_ollama_prompt(
    article: Dict[str, Any],
    system_prompt: str,
    model_name: Optional[str] = None,
    suppress_notify: bool = False,
) -> Dict[str, Any]:
    return _run_provider_chat(PROVIDERS["ollama"], article, system_prompt, model_name, suppress_notify=suppress_notify)


def analyze_with_zhipu_prompt(
    article: Dict[str, Any],
    system_prompt: str,
    model_name: Optional[str] = None,
) -> Dict[str, Any]:
    return _run_provider_chat(PROVIDERS["zhipu"], article, system_prompt, model_name)


def analyze_with_provider_prompt(
    article: Dict[str, Any],
    provider: str,
    system_prompt: str,
    model_name: str,
    suppress_notify: bool = False,
) -> Dict[str, Any]:
    normalized_provider = normalize_provider_name(provider)
    if normalized_provider == "gemini":
        return analyze_with_gemini_prompt(article, system_prompt, model_name=model_name)
    if normalized_provider == "iflow":
        return analyze_with_iflow_prompt(article, system_prompt, model_name=model_name)
    if normalized_provider == "openai":
        return analyze_with_openai_prompt(article, system_prompt, model_name=model_name)
    if normalized_provider == "ark":
        return analyze_with_ark_prompt(article, system_prompt, model_name=model_name)
    if normalized_provider == "deepseek":
        return analyze_with_deepseek_prompt(article, system_prompt, model_name=model_name)
    if normalized_provider == "ollama":
        return analyze_with_ollama_prompt(article, system_prompt, model_name=model_name, suppress_notify=suppress_notify)
    if normalized_provider == "zhipu":
        return analyze_with_zhipu_prompt(article, system_prompt, model_name=model_name)
    raise ValueError(f"unsupported provider: {provider}")


def build_failed_analysis(summary: str, category: str = "解析失败") -> Dict[str, Any]:
    return {"categories": [category], "score": 0.0, "summary": summary, "title_zh": "", "qa": []}


def ensure_analysis_provider(analysis: Dict[str, Any], provider: str) -> Dict[str, Any]:
    if analysis_provider_used(analysis):
        return analysis
    llm_request_count = int(get_llm_meta(analysis).get("llm_request_count") or 0)
    if get_analysis_action(analysis) == "pass" and llm_request_count <= 0:
        return analysis
    return mark_analysis_provider(analysis, provider)


def analyze_article(
    article: Dict[str, Any],
    prompt_config: Any,
    provider: Optional[str] = None,
    include_summary: bool = True,
    suppress_notify: bool = False,
) -> Dict[str, Any]:
    if not isinstance(prompt_config, dict):
        return attach_llm_meta(build_failed_analysis("invalid prompt config"), llm_request_count=0)

    target_provider = normalize_provider_name(provider or config.LLM_PROVIDER)
    screen_provider_override = clean_feishu_value(getattr(config, "SCREEN_PROVIDER", "")).strip()
    screen_provider = normalize_provider_name(screen_provider_override) if screen_provider_override else target_provider
    screen_prompt = str(prompt_config.get("screen_prompt") or "").strip()
    summarize_prompt = str(prompt_config.get("summarize_prompt") or "").strip()
    if not screen_prompt or not summarize_prompt:
        return attach_llm_meta(build_failed_analysis("missing prompt sections"), llm_request_count=0)

    keyword_blocklist = normalize_string_list(prompt_config.get("keyword_blocklist") or [])
    blocked_keyword = find_blocked_keyword(article, keyword_blocklist)
    if blocked_keyword:
        return attach_llm_meta(
            {
                "action": "pass",
                "reason": f"命中关键词过滤：{blocked_keyword}",
            },
            keyword_filtered=True,
            keyword_hit=blocked_keyword,
            llm_request_count=0,
        )

    screen_validate_retries = max(1, config.SCREEN_VALIDATE_RETRIES)
    screen_request_count = 0
    validated_screen: Optional[Dict[str, Any]] = None
    last_screen_error: Optional[ValueError] = None
    screen_model = provider_model_for_stage(screen_provider, "screen")
    if screen_provider_override and screen_provider == "deepseek":
        screen_model = config.DEEPSEEK_SCREEN_MODEL or screen_model
    for attempt in range(screen_validate_retries):
        screen_request_count += 1
        attempt_prompt = screen_prompt
        if attempt > 0 and last_screen_error is not None:
            attempt_prompt = _screen_retry_prompt(screen_prompt, last_screen_error)
        screen_result = analyze_with_provider_prompt(
            article,
            screen_provider,
            attempt_prompt,
            screen_model,
            suppress_notify=suppress_notify,
        )
        if has_failed_categories(screen_result):
            return mark_analysis_provider(
                attach_llm_meta(screen_result, llm_request_count=screen_request_count),
                screen_provider,
            )
        try:
            validated_screen = validate_screen_result(screen_result)
            break
        except ValueError as exc:
            last_screen_error = exc
            if _is_retryable_screen_validation_error(exc) and attempt + 1 < screen_validate_retries:
                log(
                    f"[LLM] screen validation failed, retrying "
                    f"({attempt + 1}/{screen_validate_retries}): {exc}"
                )
                continue
            return mark_analysis_provider(
                attach_llm_meta(build_failed_analysis(f"screen: {exc}"), llm_request_count=screen_request_count),
                screen_provider,
            )
    if validated_screen is None:
        return mark_analysis_provider(
            attach_llm_meta(build_failed_analysis(f"screen: {last_screen_error}"), llm_request_count=screen_request_count),
            screen_provider,
        )
    if validated_screen["action"] == "pass":
        return mark_analysis_provider(attach_llm_meta(validated_screen, llm_request_count=screen_request_count), screen_provider)
    if not include_summary:
        return mark_analysis_provider(attach_llm_meta(validated_screen, llm_request_count=screen_request_count), screen_provider)

    summary_result = analyze_with_provider_prompt(
        article,
        target_provider,
        summarize_prompt,
        provider_model_for_stage(target_provider, "summary"),
        suppress_notify=suppress_notify,
    )
    if has_failed_categories(summary_result):
        failed = mark_analysis_provider(
            attach_llm_meta(summary_result, llm_request_count=screen_request_count + 1),
            screen_provider,
        )
        failed["_summary_provider_used"] = target_provider
        return failed
    try:
        validated_summary = validate_summary_result(summary_result)
    except ValueError as exc:
        failed = mark_analysis_provider(
            attach_llm_meta(build_failed_analysis(f"summary: {exc}"), llm_request_count=screen_request_count + 1),
            screen_provider,
        )
        failed["_summary_provider_used"] = target_provider
        return failed

    merged = dict(validated_screen)
    merged.update(validated_summary)
    merged = mark_analysis_provider(attach_llm_meta(merged, llm_request_count=screen_request_count + 1), screen_provider)
    merged["_summary_provider_used"] = target_provider
    return merged


def _analyze_article_compat(*args: Any, include_summary: bool = True, **kwargs: Any) -> Dict[str, Any]:
    try:
        return analyze_article(*args, include_summary=include_summary, **kwargs)
    except TypeError as exc:
        if "include_summary" not in str(exc):
            raise
        return analyze_article(*args, **kwargs)


def _analyze_with_llm_compat(*args: Any, include_summary: bool = True, **kwargs: Any) -> Dict[str, Any]:
    try:
        return analyze_with_llm(*args, include_summary=include_summary, **kwargs)
    except TypeError as exc:
        if "include_summary" not in str(exc):
            raise
        return analyze_with_llm(*args, **kwargs)


def summarize_with_llm(
    article: Dict[str, Any],
    analysis: Dict[str, Any],
    prompt_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    resolved_prompt_config = resolve_prompt_config(prompt_config)
    summarize_prompt = str(resolved_prompt_config.get("summarize_prompt") or "").strip()
    if not summarize_prompt:
        return attach_llm_meta(build_failed_analysis("missing summarize prompt"), llm_request_count=0)

    provider = normalize_provider_name(analysis_provider_used(analysis) or config.LLM_PROVIDER)
    base_count = int(get_llm_meta(analysis).get("llm_request_count") or 0)

    summary_result = analyze_with_provider_prompt(
        article,
        provider,
        summarize_prompt,
        provider_model_for_stage(provider, "summary"),
    )
    if has_failed_categories(summary_result):
        return mark_analysis_provider(attach_llm_meta(summary_result, llm_request_count=base_count + 1), provider)
    try:
        validated_summary = validate_summary_result(summary_result)
    except ValueError as exc:
        return mark_analysis_provider(
            attach_llm_meta(build_failed_analysis(f"summary: {exc}"), llm_request_count=base_count + 1),
            provider,
        )

    merged = dict(analysis)
    merged.update(validated_summary)
    return mark_analysis_provider(attach_llm_meta(merged, llm_request_count=base_count + 1), provider)


def analyze_with_llm(
    article: Dict[str, Any],
    prompt_config: Optional[Dict[str, Any]] = None,
    include_summary: bool = True,
) -> Dict[str, Any]:
    requested_provider = str(config.LLM_PROVIDER or "").strip().lower()
    provider = normalize_provider_name(requested_provider)
    resolved_prompt_config = resolve_prompt_config(prompt_config)

    if requested_provider and requested_provider not in TWO_STAGE_PROVIDERS and requested_provider != "gemini":
        log(f"[LLM] unknown provider={requested_provider}, fallback to gemini")

    if provider == "ollama":
        result = _analyze_article_compat(
            article,
            resolved_prompt_config,
            provider="ollama",
            include_summary=include_summary,
            suppress_notify=True,
        )
        result = ensure_analysis_provider(result, "ollama")
        if has_failed_categories(result):
            ollama_fallback_model = str(getattr(config, "OLLAMA_FALLBACK_MODEL", "") or "").strip()
            if ollama_fallback_model and ollama_fallback_model != config.OLLAMA_MODEL:
                log(
                    f"[LLM] Ollama primary failed, "
                    f"trying fallback model={ollama_fallback_model}"
                )
                original_model = config.OLLAMA_MODEL
                original_screen = config.OLLAMA_SCREEN_MODEL
                try:
                    config.OLLAMA_MODEL = ollama_fallback_model
                    config.OLLAMA_SCREEN_MODEL = ""
                    fallback_result = _analyze_article_compat(
                        article,
                        resolved_prompt_config,
                        provider="ollama",
                        include_summary=include_summary,
                        suppress_notify=True,
                    )
                    fallback_result = ensure_analysis_provider(fallback_result, "ollama")
                    if not has_failed_categories(fallback_result):
                        return fallback_result
                finally:
                    config.OLLAMA_MODEL = original_model
                    config.OLLAMA_SCREEN_MODEL = original_screen
            fallback_provider = normalize_provider_name(config.OLLAMA_FALLBACK_PROVIDER)
            if fallback_provider == "ollama":
                return result
            log(
                f"[LLM] Ollama failed, "
                f"fallback to {provider_service_name(fallback_provider)} "
                f"model={provider_model_for_stage(fallback_provider, 'screen')}"
            )
            fallback = _analyze_article_compat(
                article,
                resolved_prompt_config,
                provider=fallback_provider,
                include_summary=include_summary,
            )
            return ensure_analysis_provider(fallback, fallback_provider)
        return result

    result = _analyze_article_compat(
        article,
        resolved_prompt_config,
        provider=provider,
        include_summary=include_summary,
    )
    return ensure_analysis_provider(result, provider)


def normalize_qa(qa: Any) -> List[Dict[str, str]]:
    if not isinstance(qa, list):
        return []
    normalized: List[Dict[str, str]] = []
    for item in qa:
        if not isinstance(item, dict):
            continue
        question = " ".join(str(item.get("question") or "").split()).strip()
        answer = " ".join(str(item.get("answer") or "").split()).strip()
        if question and answer:
            normalized.append({"question": question, "answer": answer})
    return normalized


def build_summary(qa: Optional[List[Dict[str, str]]] = None) -> str:
    normalized_qa = normalize_qa(qa or [])
    if not normalized_qa:
        return ""
    return "\n\n".join(f"{item['question']}\n{item['answer']}" for item in normalized_qa)


ENABLE_TEXT_DEDUP = config.os.getenv("ENABLE_TEXT_DEDUP", "true").lower() in {"1", "true", "yes", "y"}
TEXT_DEDUP_WINDOW_DAYS = int(config.os.getenv("TEXT_DEDUP_WINDOW_DAYS", "7") or "7")
TEXT_DEDUP_MAX_CANDIDATES = int(config.os.getenv("TEXT_DEDUP_MAX_CANDIDATES", "80") or "80")
TEXT_DEDUP_PREFETCH_MAX_PAGES = int(config.os.getenv("TEXT_DEDUP_PREFETCH_MAX_PAGES", "2") or "2")
LOCAL_DEDUP_PROMPT_PATH = config.os.getenv(
    "LOCAL_DEDUP_PROMPT_PATH",
    str(config.BASE_DIR / "docs" / "local-dedup-prompt.md"),
)


def _plain_cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or value.get("value") or "").strip()
    if isinstance(value, list):
        parts = []
        for seg in value:
            if isinstance(seg, dict):
                parts.append(str(seg.get("text") or seg.get("name") or seg.get("value") or ""))
            else:
                parts.append(str(seg))
        return "".join(parts).strip()
    return str(value).strip()


@dataclass
class DedupCandidate:
    item_key: str
    title: str
    summary: str
    keywords: str
    keyword_record_ids: List[str] = field(default_factory=list)


@dataclass
class DedupAliasSnapshot:
    version: str
    alias_to_group: Dict[str, str]
    group_to_names: Dict[str, List[str]]

    def groups_for_keyword(self, keyword: str) -> set[str]:
        key = _compact_dedup_keyword(keyword)
        if not key:
            return set()
        group = self.alias_to_group.get(key)
        if not group:
            return set()
        return {group}

    def groups_for_keywords(self, keywords: List[str]) -> set[str]:
        groups: set[str] = set()
        for keyword in keywords:
            groups.update(self.groups_for_keyword(keyword))
        return groups

    def empty(self) -> bool:
        return not self.alias_to_group


class DedupCandidateStore:
    def __init__(self, alias_snapshot: Optional[DedupAliasSnapshot] = None) -> None:
        self._candidates: List[DedupCandidate] = []
        self.alias_snapshot = alias_snapshot

    def add(
        self,
        item_key: str,
        title: str,
        summary: str,
        keywords: str,
        keyword_record_ids: Optional[List[str]] = None,
    ) -> None:
        self._candidates.append(
            DedupCandidate(
                item_key=item_key,
                title=title,
                summary=summary,
                keywords=keywords,
                keyword_record_ids=keyword_link_values(keyword_record_ids),
            )
        )

    def remove(self, item_key: str) -> None:
        self._candidates = [c for c in self._candidates if c.item_key != item_key]

    def size(self) -> int:
        return len(self._candidates)

    def select_candidates(
        self,
        exclude_item_key: str = "",
        keywords: Optional[List[str]] = None,
        keyword_record_ids: Optional[List[str]] = None,
        max_candidates: int = 0,
    ) -> List[DedupCandidate]:
        selected: List[DedupCandidate] = []
        candidates = [c for c in self._candidates if c.item_key != exclude_item_key]

        if keywords is not None:
            for c in candidates:
                if not keyword_lists_overlap(
                    keywords,
                    split_keyword_text(c.keywords),
                    alias_snapshot=self.alias_snapshot,
                ):
                    continue
                selected.append(c)
                if max_candidates > 0 and len(selected) >= max_candidates:
                    break

        query_record_ids = set(keyword_link_values(keyword_record_ids))
        if not selected and keywords is None and query_record_ids:
            for c in candidates:
                if not query_record_ids.intersection(c.keyword_record_ids):
                    continue
                selected.append(c)
                if max_candidates > 0 and len(selected) >= max_candidates:
                    break

        if not selected and keywords is None:
            for c in candidates:
                selected.append(c)
                if max_candidates > 0 and len(selected) >= max_candidates:
                    break

        return selected

    def build_candidates_text(
        self,
        exclude_item_key: str = "",
        keywords: Optional[List[str]] = None,
        keyword_record_ids: Optional[List[str]] = None,
        max_candidates: int = 0,
        include_keywords: bool = False,
    ) -> str:
        selected = self.select_candidates(
            exclude_item_key=exclude_item_key,
            keywords=keywords,
            keyword_record_ids=keyword_record_ids,
            max_candidates=max_candidates,
        )
        return self.format_candidates_text(selected, include_keywords=include_keywords)

    def build_candidates_context(
        self,
        exclude_item_key: str = "",
        keywords: Optional[List[str]] = None,
        keyword_record_ids: Optional[List[str]] = None,
        max_candidates: int = 0,
        include_keywords: bool = False,
    ) -> Tuple[str, Dict[str, DedupCandidate]]:
        selected = self.select_candidates(
            exclude_item_key=exclude_item_key,
            keywords=keywords,
            keyword_record_ids=keyword_record_ids,
            max_candidates=max_candidates,
        )
        return (
            self.format_candidates_text(selected, include_keywords=include_keywords),
            {f"C{i}": c for i, c in enumerate(selected, 1)},
        )

    @staticmethod
    def format_candidates_text(selected: List[DedupCandidate], include_keywords: bool = False) -> str:
        lines = []
        for i, c in enumerate(selected, 1):
            parts = [f"C{i}: {c.title}"]
            if c.summary:
                parts.append(f"  摘要: {c.summary}")
            if include_keywords and c.keywords:
                parts.append(f"  关键词: {c.keywords}")
            lines.append("\n".join(parts))
        return "\n\n".join(lines)


def _keyword_record_ids_from_cell(value: Any) -> List[str]:
    raw_ids: List[str] = []
    if isinstance(value, str):
        raw_ids.append(value)
    elif isinstance(value, dict):
        linked_ids = value.get("link_record_ids")
        if isinstance(linked_ids, list):
            raw_ids.extend(linked_ids)
        record_id = value.get("record_id") or value.get("id")
        if isinstance(record_id, str):
            raw_ids.append(record_id)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                raw_ids.append(item)
            elif isinstance(item, dict):
                linked_ids = item.get("link_record_ids")
                if isinstance(linked_ids, list):
                    raw_ids.extend(linked_ids)
                record_id = item.get("record_id") or item.get("id")
                if isinstance(record_id, str):
                    raw_ids.append(record_id)
    return keyword_link_values(raw_ids)


def _iter_dedup_alias_groups(raw: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        for key in ("groups", "aliases", "rules"):
            value = raw.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _dedup_alias_group_names(group: Dict[str, Any]) -> List[str]:
    raw_names = group.get("names") or group.get("aliases") or []
    names: List[str] = []
    if isinstance(group.get("canonical"), str):
        names.append(group["canonical"])
    if isinstance(raw_names, list):
        for name in raw_names:
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return list(dict.fromkeys(names))


def load_dedup_alias_snapshot(path: str = "") -> DedupAliasSnapshot:
    alias_path = path or getattr(config, "LOCAL_DEDUP_ALIAS_GROUPS_PATH", "")
    if not alias_path:
        return DedupAliasSnapshot(version="", alias_to_group={}, group_to_names={})
    try:
        with Path(alias_path).open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return DedupAliasSnapshot(version="", alias_to_group={}, group_to_names={})
    except Exception as exc:
        log(f"[TextDedup] failed to load alias snapshot {alias_path}: {exc}")
        return DedupAliasSnapshot(version="", alias_to_group={}, group_to_names={})

    version = ""
    if isinstance(raw, dict):
        version = str(raw.get("version") or raw.get("snapshot_id") or "").strip()
    if not version:
        version = Path(alias_path).name

    alias_to_group: Dict[str, str] = {}
    group_to_names: Dict[str, List[str]] = {}
    ambiguous_keys: set[str] = set()
    for i, group in enumerate(_iter_dedup_alias_groups(raw), 1):
        group_id = str(
            group.get("group_id")
            or group.get("canonical_id")
            or group.get("id")
            or f"alias::{i}"
        ).strip()
        names = _dedup_alias_group_names(group)
        if not group_id or len(names) < 2:
            continue
        group_to_names[group_id] = names
        for name in names:
            key = _compact_dedup_keyword(name)
            if not key:
                continue
            existing = alias_to_group.get(key)
            if existing and existing != group_id:
                ambiguous_keys.add(key)
                continue
            alias_to_group[key] = group_id

    for key in ambiguous_keys:
        alias_to_group.pop(key, None)
    return DedupAliasSnapshot(version=version, alias_to_group=alias_to_group, group_to_names=group_to_names)


def load_dedup_store(tenant_token: str) -> DedupCandidateStore:
    alias_snapshot = load_dedup_alias_snapshot()
    store = DedupCandidateStore(alias_snapshot=alias_snapshot)
    if not config.FEISHU_NEWS_TABLE_ID or not ENABLE_TEXT_DEDUP:
        return store

    cutoff_ms = int((time.time() - TEXT_DEDUP_WINDOW_DAYS * 86400) * 1000)
    sort = [{"field_name": config.NEWS_FIELD_PUBLISHED_MS, "desc": True}]
    try:
        records = list_bitable_records(
            config.FEISHU_APP_TOKEN,
            config.FEISHU_NEWS_TABLE_ID,
            tenant_token,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
            page_size=500,
            max_pages=max(1, TEXT_DEDUP_PREFETCH_MAX_PAGES),
            sort=sort,
            allow_partial=True,
        )
    except Exception as exc:
        log(f"[TextDedup] failed to load recent NEWS: {exc}")
        return store

    for record in records:
        fields = record.get("fields") or {}
        pub_raw = fields.get(config.NEWS_FIELD_PUBLISHED_MS)
        pub_ms = parse_ts_ms(pub_raw)
        if pub_ms and pub_ms < cutoff_ms:
            break

        title = _plain_cell_text(fields.get(config.NEWS_FIELD_TITLE))
        brief = _plain_cell_text(fields.get(config.NEWS_FIELD_BRIEF_SUMMARY))
        item_key = _plain_cell_text(fields.get(config.NEWS_FIELD_ITEM_KEY))
        kw_list = split_keyword_text(fields.get(config.NEWS_FIELD_KEYWORDS))
        keyword_record_ids = _keyword_record_ids_from_cell(fields.get(config.NEWS_FIELD_KEYWORD_RECORDS))

        if title.strip() and item_key:
            store.add(item_key, title, brief, ", ".join(kw_list), keyword_record_ids=keyword_record_ids)

    alias_msg = ""
    if alias_snapshot and not alias_snapshot.empty():
        alias_msg = f", alias_snapshot={alias_snapshot.version}, alias_keys={len(alias_snapshot.alias_to_group)}"
    log(
        f"[TextDedup] loaded {store.size()} recent NEWS records for LLM dedup "
        f"({TEXT_DEDUP_WINDOW_DAYS}d window{alias_msg})"
    )
    return store


def _load_dedup_prompt() -> str:
    return load_prompt_text_file(LOCAL_DEDUP_PROMPT_PATH)


def strip_dedup_keyword_lines(text: str) -> str:
    lines = []
    for line in str(text or "").splitlines():
        if re.match(r"^\s*关键词\s*[:：]", line):
            continue
        lines.append(line)
    return "\n".join(lines)


def split_keyword_text(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        if any(isinstance(item, dict) and item.get("type") in {"text", "url"} for item in value):
            text = "".join(
                str(item.get("text") or item.get("name") or item.get("value") or "") if isinstance(item, dict) else str(item or "")
                for item in value
            ).strip()
            return split_keyword_text(text)
        out = []
        for item in value:
            if isinstance(item, dict):
                text = str(item.get("name") or item.get("text") or item.get("value") or "").strip()
            else:
                text = str(item or "").strip()
            if not text:
                continue
            if re.search(r"\s+[/／]\s+", text):
                out.extend(split_keyword_text(text))
            else:
                out.append(text)
        return out
    return [part.strip() for part in re.split(r"\s+[/／]\s+|[,，;；]", str(value)) if part.strip()]


def _normalize_dedup_keyword(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip().lower())


def _compact_dedup_keyword(value: str) -> str:
    return re.sub(r"[\s\-_·.@/\u2122]+", "", _normalize_dedup_keyword(value))


def _has_cjk(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", value))


def dedup_keywords_match(left: str, right: str) -> bool:
    a = _compact_dedup_keyword(left)
    b = _compact_dedup_keyword(right)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter = min(len(a), len(b))
    if shorter >= 3 or (shorter >= 2 and (_has_cjk(a) or _has_cjk(b))):
        return a in b or b in a
    return False


def keyword_lists_overlap(
    left: List[str],
    right: List[str],
    alias_snapshot: Optional[DedupAliasSnapshot] = None,
) -> bool:
    if any(dedup_keywords_match(a, b) for a in left for b in right):
        return True
    if not alias_snapshot or alias_snapshot.empty():
        return False
    return bool(alias_snapshot.groups_for_keywords(left) & alias_snapshot.groups_for_keywords(right))


def dedup_keyword_overlap_count(
    left: List[str],
    right: List[str],
    alias_snapshot: Optional[DedupAliasSnapshot] = None,
) -> int:
    count = 0
    for left_keyword in left:
        if any(dedup_keywords_match(left_keyword, right_keyword) for right_keyword in right):
            count += 1
            continue
        if alias_snapshot and not alias_snapshot.empty():
            left_groups = alias_snapshot.groups_for_keywords([left_keyword])
            if left_groups and left_groups & alias_snapshot.groups_for_keywords(right):
                count += 1
    return count


def dedup_match_has_sparse_keyword_overlap(
    new_keywords: List[str],
    candidate_keywords: List[str],
    alias_snapshot: Optional[DedupAliasSnapshot] = None,
) -> bool:
    if len(new_keywords) < 2 or len(candidate_keywords) < 2:
        return False
    return dedup_keyword_overlap_count(new_keywords, candidate_keywords, alias_snapshot=alias_snapshot) < 2


def _dedup_reason_contradicts_duplicate(reason: str) -> bool:
    text = str(reason or "")
    caveat_phrases = [
        "虽",
        "但",
        "不同",
        "未直接",
        "未明确",
        "可视为",
        "同属",
        "不同侧面",
        "仅能确认",
        "大致",
        "应该",
        "可能是",
        "推测",
        "似乎",
        "类似",
        "相关",
        "关联",
    ]
    if any(phrase in text for phrase in caveat_phrases):
        return True
    direct_negative = [
        "不重复",
        "无关",
        "没有匹配",
        "无匹配",
        "无任何一条",
        "未找到",
        "不是同一",
        "并非同一",
        "不属于同一",
    ]
    if any(phrase in text for phrase in direct_negative):
        return True
    if "不同事件" in text and not re.search(r"(不是|并非|非|不属于).{0,4}不同事件", text):
        return True
    return False


def llm_dedup_check(
    title_zh: str,
    brief_summary: str,
    keywords_str: str,
    candidates_text: str,
    provider: str = "",
) -> Optional[Dict[str, Any]]:
    if not candidates_text.strip():
        return None

    dedup_prompt = _load_dedup_prompt()
    clean_candidates_text = strip_dedup_keyword_lines(candidates_text)
    user_content = (
        f"# 新文章\n"
        f"标题: {title_zh}\n"
        f"摘要: {brief_summary}\n\n"
        f"# 已入库文章\n{clean_candidates_text}"
    )

    fake_article = {"title": title_zh, "content": user_content, "link": "", "source": "dedup"}
    target_provider = normalize_provider_name(provider or config.TEXT_DEDUP_PROVIDER)
    model_name = provider_model_for_stage(target_provider, "screen")

    try:
        result_dict = analyze_with_provider_prompt(
            fake_article,
            target_provider,
            dedup_prompt,
            model_name,
            suppress_notify=True,
        )
    except Exception as exc:
        log(f"[TextDedup] LLM call failed: {exc}")
        audit_text_dedup_failure(target_provider, title_zh, str(exc))
        return None

    if has_failed_categories(result_dict):
        reason = str(result_dict.get("summary") or result_dict.get("reason") or result_dict)
        log(f"[TextDedup] LLM returned failed analysis: {reason[:160]}")
        audit_text_dedup_failure(target_provider, title_zh, reason)
        return None

    is_dup = result_dict.get("is_duplicate")
    if is_dup is True:
        matched_id = str(result_dict.get("matched_id") or "").strip()
        reason = str(result_dict.get("reason") or "").strip()
        if not re.fullmatch(r"C\d+", matched_id):
            log(f"[TextDedup] ignore invalid matched_id={matched_id!r} title={title_zh[:60]}")
            return None
        if _dedup_reason_contradicts_duplicate(reason):
            log(f"[TextDedup] ignore contradictory duplicate matched={matched_id} reason={reason[:120]}")
            return None
        matched_title = str(result_dict.get("matched_title") or "").strip()
        shared_facts = result_dict.get("shared_facts")
        if not isinstance(shared_facts, list) or len([str(item).strip() for item in shared_facts if str(item).strip()]) < 2:
            log(f"[TextDedup] ignore duplicate with insufficient shared_facts matched={matched_id} title={title_zh[:60]}")
            return None
        return {
            "matched_id": matched_id,
            "matched_title": matched_title,
            "shared_facts": shared_facts,
            "reason": reason,
        }

    return None


def parse_failed_items(raw: Any) -> List[Dict[str, Any]]:
    if not raw:
        return []
    data: Any = raw
    if isinstance(raw, str) or (
        isinstance(raw, list)
        and not any(isinstance(item, dict) and item.get("item_key") for item in raw)
    ):
        s = clean_feishu_value(raw).strip()
        if not s:
            return []
        try:
            data = json.loads(s)
        except Exception:
            return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    items: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        item_key = str(item.get("item_key") or "").strip()
        if not item_key:
            continue
        items.append(
            {
                "item_key": item_key,
                "title": str(item.get("title") or ""),
                "link": str(item.get("link") or ""),
                "published_ms": int(item.get("published_ms") or 0),
                "fail_count": int(item.get("fail_count") or 0),
                "last_error": str(item.get("last_error") or ""),
                "last_seen_ms": int(item.get("last_seen_ms") or 0),
                "miss_count": int(item.get("miss_count") or 0),
            }
        )
    return items


def serialize_failed_items(items: List[Dict[str, Any]]) -> str:
    return json.dumps(items, ensure_ascii=False)


def upsert_failed_item(
    items: List[Dict[str, Any]],
    item_key: str,
    entry_ts_ms: int,
    title: str,
    link: str,
    reason: str,
    now_ms: int,
) -> List[Dict[str, Any]]:
    for item in items:
        if item.get("item_key") == item_key:
            item["fail_count"] = int(item.get("fail_count") or 0) + 1
            item["last_error"] = reason or item.get("last_error") or ""
            item["last_seen_ms"] = now_ms
            item["miss_count"] = 0
            if title and not item.get("title"):
                item["title"] = title
            if link and not item.get("link"):
                item["link"] = link
            if entry_ts_ms and not item.get("published_ms"):
                item["published_ms"] = entry_ts_ms
            return items

    items.append(
        {
            "item_key": item_key,
            "title": title or "",
            "link": link or "",
            "published_ms": entry_ts_ms or 0,
            "fail_count": 1,
            "last_error": reason or "",
            "last_seen_ms": now_ms,
            "miss_count": 0,
        }
    )
    return items


def prune_failed_items(items: List[Dict[str, Any]], now_ms: int) -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    for item in items:
        key = item.get("item_key")
        if not key:
            continue
        prev = seen.get(key)
        if not prev or int(item.get("last_seen_ms") or 0) >= int(prev.get("last_seen_ms") or 0):
            seen[key] = item

    max_age_ms = config.FAILED_ITEMS_MAX_AGE_DAYS * 24 * 60 * 60 * 1000
    pruned: List[Dict[str, Any]] = []
    for item in seen.values():
        miss_count = int(item.get("miss_count") or 0)
        if miss_count >= config.FAILED_ITEMS_MAX_MISS:
            continue
        seen_ms = int(item.get("last_seen_ms") or item.get("published_ms") or 0)
        if seen_ms and now_ms - seen_ms > max_age_ms:
            continue
        pruned.append(item)

    pruned.sort(key=lambda x: int(x.get("last_seen_ms") or 0), reverse=True)
    return pruned[: config.FAILED_ITEMS_MAX]


def cap_source_cursor_for_failed_items(
    latest_pub_ms: int,
    latest_key: str,
    failed_items: List[Dict[str, Any]],
) -> Tuple[int, str]:
    failed_pub_times: List[int] = []
    for item in failed_items or []:
        try:
            published_ms = int(item.get("published_ms") or 0)
        except Exception:
            continue
        if published_ms > 0:
            failed_pub_times.append(published_ms)
    if not latest_pub_ms or not failed_pub_times:
        return latest_pub_ms, latest_key

    cursor_cap = max(0, min(failed_pub_times) - 1)
    if cursor_cap < latest_pub_ms:
        return cursor_cap, ""
    return latest_pub_ms, latest_key


def normalize_source(record: Dict[str, Any]) -> Dict[str, Any]:
    fields = record.get("fields") or {}
    source_id = record.get("record_id") or ""
    enabled = is_checked(fields.get(config.RSS_FIELD_ENABLED))
    last_fetch_time = parse_ts_ms(fields.get(config.RSS_FIELD_LAST_FETCH_TIME))
    last_item_pub_time = parse_ts_ms(fields.get(config.RSS_FIELD_LAST_ITEM_PUB_TIME))
    consecutive_fail = parse_int(fields.get(config.RSS_FIELD_CONSECUTIVE_FAIL_COUNT)) or 0
    item_id_strategy = normalize_single_select(
        fields.get(config.RSS_FIELD_ITEM_ID_STRATEGY),
        config.ITEM_ID_STRATEGY_OPTIONS,
        config.DEFAULT_ITEM_ID_STRATEGY,
    )

    return {
        "record_id": record.get("record_id"),
        "source_id": source_id,
        "name": clean_feishu_value(fields.get(config.RSS_FIELD_NAME)),
        "feed_url": clean_feishu_value(fields.get(config.RSS_FIELD_FEED_URL)),
        "type": clean_feishu_value(fields.get(config.RSS_FIELD_TYPE)),
        "description": clean_feishu_value(fields.get(config.RSS_FIELD_DESCRIPTION)),
        "enabled": enabled,
        "last_fetch_time": last_fetch_time,
        "last_item_pub_time": last_item_pub_time,
        "last_item_guid": clean_feishu_value(fields.get(config.RSS_FIELD_LAST_ITEM_GUID)),
        "item_id_strategy": item_id_strategy,
        "content_hash_algo": config.DEFAULT_CONTENT_HASH_ALGO,
        "consecutive_fail_count": consecutive_fail,
        "failed_items": fields.get(config.RSS_FIELD_FAILED_ITEMS),
        "watch_state": clean_feishu_value(fields.get(config.RSS_FIELD_WATCH_STATE)),
    }


def should_fetch(source: Dict[str, Any], now_ms: int) -> bool:
    if not source.get("enabled"):
        return False
    interval_min = config.DEFAULT_FETCH_INTERVAL_MIN
    last_fetch = source.get("last_fetch_time") or 0
    last_item_pub = source.get("last_item_pub_time") or 0
    # Use the most recent observed timestamp to avoid over-fetching stale feeds.
    last_base = max(last_fetch, last_item_pub)
    if last_base <= 0:
        return True
    return now_ms - last_base >= interval_min * 60 * 1000


def normalize_entry_published_ts(entry: Dict[str, Any], now_ms: int) -> int:
    entry_ts = entry_published_ts(entry)
    if entry_ts and entry_ts * 1000 > now_ms:
        return int(now_ms // 1000)
    return entry_ts


def build_article_base_fields(article: Dict[str, Any], item_key: str) -> Dict[str, Any]:
    published = article.get("published")
    if isinstance(published, (int, float)) and published > 0:
        base_ts = published
    else:
        base_ts = time.time()
    return {
        "published_ts_ms": int(base_ts * 1000),
        "source": article.get("source") or "未知来源",
        "full_content": limit_feishu_cell_text(clean_html_to_text(article.get("content") or "")),
        "title": article.get("title") or "（无标题）",
        "link": article.get("link") or "",
        "item_key": item_key,
    }


def fallback_filtered_summary(base: Dict[str, Any], limit: int = 180) -> str:
    text = str(base.get("full_content") or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def is_filtered_table_enabled() -> bool:
    return bool(getattr(config, "FEISHU_FILTERED_TABLE_ID", "").strip())


@dataclass
class KeywordRecord:
    record_id: str
    canonical_name: str
    type: str
    parent_ids: List[str] = field(default_factory=list)
    owner_ids: List[str] = field(default_factory=list)
    news_count: int = 0
    filtered_count: int = 0


def normalize_keyword_alias(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().lower()


_COMPACT_KEYWORD_ALIAS_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+", re.IGNORECASE)


def compact_keyword_alias(value: str) -> str:
    normalized = normalize_keyword_alias(value)
    return _COMPACT_KEYWORD_ALIAS_RE.sub("", normalized)


def keyword_alias_index_keys(value: str) -> List[str]:
    normalized = normalize_keyword_alias(value)
    keys: List[str] = []
    if normalized:
        keys.append(normalized)
    compact = compact_keyword_alias(value)
    if compact and len(compact) >= 4:
        keys.append(f"compact:{compact}")
    return keys


def is_merged_keyword_note(value: Any) -> bool:
    return "[merged" in clean_feishu_value(value)


def parse_keyword_count_value(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, dict):
        if "value" in value:
            return parse_keyword_count_value(value.get("value"))
        if "text" in value:
            return parse_keyword_count_value(value.get("text"))
    if isinstance(value, list):
        for item in value:
            count = parse_keyword_count_value(item)
            if count:
                return count
        return 0
    raw = clean_feishu_value(value).replace(",", "").strip()
    return parse_int(raw) or 0


def keyword_record_usage_count(record: KeywordRecord) -> int:
    return max(0, record.news_count) + max(0, record.filtered_count)


def is_keyword_record_blocked(record: KeywordRecord) -> bool:
    return _is_keyword_name_blocked(record.canonical_name)


def should_replace_keyword_index_record(existing: KeywordRecord, candidate: KeywordRecord) -> bool:
    existing_blocked = is_keyword_record_blocked(existing)
    candidate_blocked = is_keyword_record_blocked(candidate)
    if existing_blocked != candidate_blocked:
        return existing_blocked and not candidate_blocked
    existing_usage = keyword_record_usage_count(existing)
    candidate_usage = keyword_record_usage_count(candidate)
    if candidate_usage != existing_usage:
        return candidate_usage > existing_usage
    return False


def put_keyword_index_record(
    index: Dict[str, KeywordRecord],
    key: str,
    candidate: KeywordRecord,
) -> None:
    existing = index.get(key)
    if not existing:
        index[key] = candidate
        return
    if existing.record_id == candidate.record_id:
        return
    if should_replace_keyword_index_record(existing, candidate):
        log(
            "[Keyword] duplicate alias key="
            f"{key} replace={existing.canonical_name}/{existing.record_id} "
            f"with={candidate.canonical_name}/{candidate.record_id}"
        )
        index[key] = candidate


def keyword_record_from_fields(record_id: str, fields: Dict[str, Any]) -> KeywordRecord:
    return KeywordRecord(
        record_id=record_id,
        canonical_name=clean_feishu_value(fields.get(config.KEYWORD_FIELD_CANONICAL_NAME)).strip(),
        type=clean_feishu_value(fields.get(config.KEYWORD_FIELD_TYPE)).strip().lower(),
        parent_ids=_keyword_record_ids_from_cell(fields.get(config.KEYWORD_FIELD_PARENT)),
        owner_ids=_keyword_record_ids_from_cell(fields.get(config.KEYWORD_FIELD_OWNERS)),
        news_count=parse_keyword_count_value(fields.get(config.KEYWORD_FIELD_NEWS_COUNT)),
        filtered_count=parse_keyword_count_value(fields.get(config.KEYWORD_FIELD_FILTERED_COUNT)),
    )


def keyword_record_to_snapshot_entry(record: KeywordRecord, aliases: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "record_id": record.record_id,
        "canonical_name": record.canonical_name,
        "type": record.type,
        "aliases": [str(alias).strip() for alias in aliases or [] if str(alias or "").strip()],
        "news_count": record.news_count,
        "filtered_count": record.filtered_count,
        "note": "",
        "parent_ids": keyword_link_values(record.parent_ids),
        "owner_ids": keyword_link_values(record.owner_ids),
    }


def keyword_snapshot_entry_from_record(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    record_id = clean_feishu_value(record.get("record_id")).strip()
    fields = record.get("fields") or {}
    canonical = clean_feishu_value(fields.get(config.KEYWORD_FIELD_CANONICAL_NAME)).strip()
    if not record_id or not canonical:
        return None
    aliases_text = clean_feishu_value(fields.get(config.KEYWORD_FIELD_ALIASES))
    aliases = [line.strip() for line in aliases_text.splitlines() if line.strip()]
    return {
        "record_id": record_id,
        "canonical_name": canonical,
        "type": clean_feishu_value(fields.get(config.KEYWORD_FIELD_TYPE)).strip().lower(),
        "aliases": aliases,
        "news_count": parse_keyword_count_value(fields.get(config.KEYWORD_FIELD_NEWS_COUNT)),
        "filtered_count": parse_keyword_count_value(fields.get(config.KEYWORD_FIELD_FILTERED_COUNT)),
        "note": clean_feishu_value(fields.get(config.KEYWORD_FIELD_NOTE)).strip(),
        "parent_ids": _keyword_record_ids_from_cell(fields.get(config.KEYWORD_FIELD_PARENT)),
        "owner_ids": _keyword_record_ids_from_cell(fields.get(config.KEYWORD_FIELD_OWNERS)),
    }


def keyword_snapshot_payload_from_records(records: List[Dict[str, Any]], source: str = "") -> Dict[str, Any]:
    entries = []
    for record in records:
        entry = keyword_snapshot_entry_from_record(record)
        if entry:
            entries.append(entry)
    return {
        "schema_version": 2,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "entry_count": len(entries),
        "entries": sorted(entries, key=lambda item: (item.get("type") or "", str(item.get("canonical_name") or "").lower(), item.get("record_id") or "")),
    }


def build_keyword_index_from_records(records: List[Dict[str, Any]]) -> Dict[str, KeywordRecord]:
    index: Dict[str, KeywordRecord] = {}
    for record in records:
        record_id = clean_feishu_value(record.get("record_id")).strip()
        if not record_id:
            continue
        fields = record.get("fields") or {}
        if is_merged_keyword_note(fields.get(config.KEYWORD_FIELD_NOTE)):
            continue
        canonical = clean_feishu_value(fields.get(config.KEYWORD_FIELD_CANONICAL_NAME)).strip()
        if not canonical or _is_keyword_name_blocked(canonical):
            continue
        aliases_text = clean_feishu_value(fields.get(config.KEYWORD_FIELD_ALIASES))
        aliases = [canonical]
        aliases.extend(line.strip() for line in aliases_text.splitlines() if line.strip())
        keyword_record = keyword_record_from_fields(record_id, fields)
        for alias in aliases:
            if _is_keyword_name_blocked(alias):
                continue
            for key in keyword_alias_index_keys(alias):
                put_keyword_index_record(index, key, keyword_record)
    return index


def _snapshot_aliases(raw_aliases: Any) -> List[str]:
    if isinstance(raw_aliases, list):
        return [str(alias).strip() for alias in raw_aliases if str(alias or "").strip()]
    text = clean_feishu_value(raw_aliases)
    return [line.strip() for line in text.splitlines() if line.strip()]


def _snapshot_record_ids(value: Any) -> List[str]:
    return keyword_link_values(_keyword_record_ids_from_cell(value))


def build_keyword_index_from_snapshot_payload(payload: Dict[str, Any]) -> Dict[str, KeywordRecord]:
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError("keyword snapshot must contain entries list")
    index: Dict[str, KeywordRecord] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        if is_merged_keyword_note(item.get("note")):
            continue
        record_id = clean_feishu_value(item.get("record_id")).strip()
        canonical = clean_feishu_value(item.get("canonical_name")).strip()
        if not record_id or not canonical or _is_keyword_name_blocked(canonical):
            continue
        keyword_record = KeywordRecord(
            record_id=record_id,
            canonical_name=canonical,
            type=clean_feishu_value(item.get("type")).strip().lower(),
            parent_ids=_snapshot_record_ids(item.get("parent_ids")),
            owner_ids=_snapshot_record_ids(item.get("owner_ids")),
            news_count=parse_keyword_count_value(item.get("news_count")),
            filtered_count=parse_keyword_count_value(item.get("filtered_count")),
        )
        aliases = [canonical, *_snapshot_aliases(item.get("aliases"))]
        for alias in aliases:
            if _is_keyword_name_blocked(alias):
                continue
            for key in keyword_alias_index_keys(alias):
                put_keyword_index_record(index, key, keyword_record)
    return index


def _resolve_path(raw_path: str) -> Path:
    path = Path(str(raw_path or "")).expanduser()
    if not path.is_absolute():
        path = config.BASE_DIR / path
    return path


def _load_json_payload(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _write_json_payload_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _keyword_snapshot_generated_at(payload: Optional[Dict[str, Any]]) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("generated_at") or "")


def _keyword_snapshot_is_usable(payload: Dict[str, Any]) -> bool:
    try:
        schema_version = int(payload.get("schema_version") or 0)
    except Exception:
        schema_version = 0
    entries = payload.get("entries")
    min_entries = max(0, int(getattr(config, "KEYWORD_SNAPSHOT_MIN_ENTRIES", 1000) or 0))
    return schema_version >= 2 and isinstance(entries, list) and len(entries) >= min_entries


def _keyword_snapshot_is_fresh(payload: Dict[str, Any]) -> bool:
    max_age_hours = float(getattr(config, "KEYWORD_SNAPSHOT_MAX_AGE_HOURS", 6) or 0)
    if max_age_hours <= 0:
        return True
    raw_generated_at = _keyword_snapshot_generated_at(payload)
    if not raw_generated_at:
        return False
    try:
        generated_at = dt.datetime.fromisoformat(raw_generated_at)
    except ValueError:
        return False
    now = dt.datetime.now(generated_at.tzinfo) if generated_at.tzinfo else dt.datetime.now()
    age_hours = (now - generated_at).total_seconds() / 3600
    return age_hours <= max_age_hours


def _keyword_snapshot_is_usable_for_ingest(payload: Dict[str, Any]) -> bool:
    return _keyword_snapshot_is_usable(payload) and _keyword_snapshot_is_fresh(payload)


def _download_keyword_snapshot_payload(url: str) -> Optional[Dict[str, Any]]:
    clean_url = clean_feishu_value(url).strip()
    if not clean_url:
        return None
    resp = requests.get(clean_url, timeout=max(1, int(getattr(config, "KEYWORD_SNAPSHOT_TIMEOUT", 8) or 8)))
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("keyword snapshot URL returned non-object JSON")
    return data


def _keyword_snapshot_git_fetch_due(now: Optional[float] = None) -> bool:
    interval_min = max(
        0,
        int(getattr(config, "KEYWORD_SNAPSHOT_GIT_FETCH_INTERVAL_MIN", 60) or 0),
    )
    if interval_min == 0:
        return True
    stamp_path = _resolve_path(getattr(config, "KEYWORD_SNAPSHOT_GIT_FETCH_STAMP_PATH", ""))
    if not stamp_path:
        return True
    try:
        modified_at = stamp_path.stat().st_mtime
    except OSError:
        return True
    current_time = time.time() if now is None else now
    return current_time - modified_at >= interval_min * 60


def _record_keyword_snapshot_git_fetch(now: Optional[float] = None) -> None:
    stamp_path = _resolve_path(getattr(config, "KEYWORD_SNAPSHOT_GIT_FETCH_STAMP_PATH", ""))
    if not stamp_path:
        return
    current_time = time.time() if now is None else now
    try:
        stamp_path.parent.mkdir(parents=True, exist_ok=True)
        stamp_path.write_text(f"{current_time}\n", encoding="utf-8")
        os.utime(stamp_path, (current_time, current_time))
    except OSError as exc:
        log(f"[Keyword] failed to record git fetch timestamp: {exc}")


def _load_keyword_snapshot_payload_from_git() -> Optional[Dict[str, Any]]:
    git_ref = clean_feishu_value(getattr(config, "KEYWORD_SNAPSHOT_GIT_REF", "")).strip()
    git_path = clean_feishu_value(getattr(config, "KEYWORD_SNAPSHOT_GIT_PATH", "")).strip()
    if not git_ref or not git_path:
        return None
    timeout = max(1, int(getattr(config, "KEYWORD_SNAPSHOT_GIT_TIMEOUT", 15) or 15))
    if getattr(config, "KEYWORD_SNAPSHOT_GIT_FETCH", True) and _keyword_snapshot_git_fetch_due():
        subprocess.run(
            ["git", "fetch", "--quiet", "origin", "main"],
            cwd=str(config.BASE_DIR),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        _record_keyword_snapshot_git_fetch()
    completed = subprocess.run(
        ["git", "show", f"{git_ref}:{git_path}"],
        cwd=str(config.BASE_DIR),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    data = json.loads(completed.stdout)
    if not isinstance(data, dict):
        raise ValueError("git keyword snapshot is not a JSON object")
    return data


def load_keyword_snapshot_payload_for_ingest() -> Optional[Dict[str, Any]]:
    runtime_path = _resolve_path(getattr(config, "KEYWORD_RUNTIME_SNAPSHOT_PATH", ""))
    runtime_payload: Optional[Dict[str, Any]] = None
    try:
        runtime_payload = _load_json_payload(runtime_path)
    except Exception as exc:
        log(f"[Keyword] runtime snapshot load failed: {exc}")

    snapshot_url = clean_feishu_value(getattr(config, "KEYWORD_SNAPSHOT_URL", "")).strip()
    if snapshot_url:
        try:
            remote_payload = _download_keyword_snapshot_payload(snapshot_url)
            if remote_payload and _keyword_snapshot_is_usable_for_ingest(remote_payload):
                remote_gen = _keyword_snapshot_generated_at(remote_payload)
                runtime_gen = _keyword_snapshot_generated_at(runtime_payload)
                if not runtime_payload or (remote_gen and remote_gen > runtime_gen):
                    _write_json_payload_atomic(runtime_path, remote_payload)
                    return remote_payload
                if runtime_payload and _keyword_snapshot_is_usable_for_ingest(runtime_payload):
                    return runtime_payload
                return remote_payload
            if remote_payload:
                log("[Keyword] remote snapshot is too old, too small, or stale; falling back")
        except Exception as exc:
            log(f"[Keyword] remote snapshot fetch failed: {exc}")

    try:
        git_payload = _load_keyword_snapshot_payload_from_git()
        if git_payload and _keyword_snapshot_is_usable_for_ingest(git_payload):
            git_gen = _keyword_snapshot_generated_at(git_payload)
            runtime_gen = _keyword_snapshot_generated_at(runtime_payload)
            if not runtime_payload or (git_gen and git_gen > runtime_gen):
                _write_json_payload_atomic(runtime_path, git_payload)
                return git_payload
            if runtime_payload and _keyword_snapshot_is_usable_for_ingest(runtime_payload):
                return runtime_payload
            return git_payload
        if git_payload:
            log("[Keyword] git snapshot is too old, too small, or stale; falling back")
    except Exception as exc:
        log(f"[Keyword] git snapshot load failed: {exc}")

    if runtime_payload and _keyword_snapshot_is_usable_for_ingest(runtime_payload):
        return runtime_payload
    if runtime_payload:
        log("[Keyword] runtime snapshot is too old, too small, or stale; falling back to live table")

    local_path = _resolve_path(getattr(config, "KEYWORD_SNAPSHOT_PATH", ""))
    try:
        local_payload = _load_json_payload(local_path)
        if local_payload and _keyword_snapshot_is_usable_for_ingest(local_payload):
            return local_payload
        if local_payload:
            log("[Keyword] local snapshot is too old, too small, or stale; falling back")
    except Exception as exc:
        log(f"[Keyword] local snapshot load failed: {exc}")
    return None


def persist_keyword_runtime_snapshot_record(record: KeywordRecord) -> None:
    if not getattr(config, "ENABLE_KEYWORD_SNAPSHOT_INDEX", True):
        return
    raw_path = clean_feishu_value(getattr(config, "KEYWORD_RUNTIME_SNAPSHOT_PATH", "")).strip()
    if not raw_path or not record.record_id or not record.canonical_name:
        return
    path = _resolve_path(raw_path)
    try:
        payload = _load_json_payload(path) or {
            "schema_version": 2,
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "source": "rss-ingest-runtime",
            "entry_count": 0,
            "entries": [],
        }
        if not _keyword_snapshot_is_usable(payload) or not _keyword_snapshot_is_fresh(payload):
            payload = {
                "schema_version": 2,
                "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
                "source": "rss-ingest-runtime",
                "entry_count": 0,
                "entries": [],
            }
        entries = [item for item in payload.get("entries") or [] if isinstance(item, dict) and item.get("record_id") != record.record_id]
        entries.append(keyword_record_to_snapshot_entry(record))
        payload["schema_version"] = 2
        payload["generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
        payload["source"] = "rss-ingest-runtime"
        payload["entries"] = sorted(entries, key=lambda item: (item.get("type") or "", str(item.get("canonical_name") or "").lower(), item.get("record_id") or ""))
        payload["entry_count"] = len(payload["entries"])
        _write_json_payload_atomic(path, payload)
    except Exception as exc:
        log(f"[Keyword] runtime snapshot update failed record_id={record.record_id}: {exc}")


_JUNK_KEYWORD_RE = re.compile(r"^[\d.]+$")
_METRIC_FACT_KEYWORD_RE = re.compile(
    r"(估值|营收|收入|用户数|用户|增长|同比|环比|融资额|支出|成本|利润|市值|调用量|token|tokens).*(\d|万|亿|千|%|％|q[1-4])",
    re.IGNORECASE,
)


def _is_junk_keyword(name: str) -> bool:
    stripped = name.strip()
    if not stripped:
        return True
    if _JUNK_KEYWORD_RE.match(stripped):
        return True
    if _METRIC_FACT_KEYWORD_RE.search(stripped):
        return True
    if len(stripped) <= 2 and not re.search(r"[一-鿿]", stripped):
        return True
    low = stripped.lower()
    if "http" in low or "www." in low:
        return True
    return False


def _is_keyword_name_blocked(name: str) -> bool:
    if _is_junk_keyword(name):
        return True
    if not _KEYWORD_NAME_BLOCKLIST:
        return False
    normalized = unicodedata.normalize("NFKC", name).strip().lower()
    return normalized in _KEYWORD_NAME_BLOCKLIST


def keyword_names_from_analysis(analysis: Dict[str, Any]) -> List[str]:
    keywords_raw = analysis.get("keywords") or []
    return [
        kw["name"].strip()
        for kw in keywords_raw
        if isinstance(kw, dict)
        and isinstance(kw.get("name"), str)
        and kw["name"].strip()
        and not _is_keyword_name_blocked(kw["name"].strip())
    ]


def format_keyword_names_text(names: List[str]) -> str:
    return " / ".join(name for name in names if str(name or "").strip())


def keyword_link_values(record_ids: Optional[List[Any]]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    raw_ids: List[Any] = []
    for item in record_ids or []:
        if isinstance(item, dict):
            linked_ids = item.get("link_record_ids")
            if isinstance(linked_ids, list):
                raw_ids.extend(linked_ids)
            record_id = item.get("record_id") or item.get("id")
            if record_id:
                raw_ids.append(record_id)
            continue
        raw_ids.append(item)
    for record_id in raw_ids:
        clean_id = clean_feishu_value(record_id).strip()
        if clean_id and clean_id not in seen:
            seen.add(clean_id)
            out.append(clean_id)
    return out


def expand_keyword_record_ids(
    record_ids: Optional[List[str]],
    keyword_index: Dict[str, KeywordRecord],
    max_depth: int = 5,
) -> List[str]:
    id_to_record: Dict[str, KeywordRecord] = {}
    for record in keyword_index.values():
        if record.record_id and record.record_id not in id_to_record:
            id_to_record[record.record_id] = record

    out: List[str] = []
    seen: set = set()

    def add(record_id: str, depth: int, allow_unknown: bool = True) -> None:
        clean_id = clean_feishu_value(record_id).strip()
        if not clean_id or clean_id in seen:
            return
        record = id_to_record.get(clean_id)
        if not record and not allow_unknown:
            return
        seen.add(clean_id)
        out.append(clean_id)
        if depth >= max_depth:
            return
        if not record:
            return
        for linked_id in [*record.parent_ids, *record.owner_ids]:
            add(linked_id, depth + 1, allow_unknown=False)

    for record_id in keyword_link_values(record_ids):
        add(record_id, 0)
    return out


def original_keyword_record_ids_from_expanded(
    record_ids: Optional[List[str]],
    keyword_records_by_id: Dict[str, KeywordRecord],
) -> List[str]:
    link_ids = keyword_link_values(record_ids)
    implied_ids: set = set()
    for record_id in link_ids:
        record = keyword_records_by_id.get(record_id)
        if not record:
            continue
        implied_ids.update(keyword_link_values(record.parent_ids))
        implied_ids.update(keyword_link_values(record.owner_ids))
    originals = [record_id for record_id in link_ids if record_id not in implied_ids]
    return originals or link_ids


def prefetch_keyword_index(tenant_token: str) -> Dict[str, KeywordRecord]:
    table_id = clean_feishu_value(getattr(config, "FEISHU_KEYWORD_TABLE_ID", "")).strip()
    if not table_id:
        return {}
    records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        table_id,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=500,
        max_pages=50,
    )
    if getattr(config, "ENABLE_KEYWORD_SNAPSHOT_INDEX", True):
        try:
            payload = keyword_snapshot_payload_from_records(records, source="feishu-live-prefetch")
            _write_json_payload_atomic(_resolve_path(getattr(config, "KEYWORD_RUNTIME_SNAPSHOT_PATH", "")), payload)
        except Exception as exc:
            log(f"[Keyword] runtime snapshot refresh from live table failed: {exc}")
    return build_keyword_index_from_records(records)


def prefetch_keyword_index_for_ingest(tenant_token: str) -> Dict[str, KeywordRecord]:
    table_id = clean_feishu_value(getattr(config, "FEISHU_KEYWORD_TABLE_ID", "")).strip()
    if not table_id:
        return {}
    if getattr(config, "ENABLE_KEYWORD_SNAPSHOT_INDEX", True):
        payload = load_keyword_snapshot_payload_for_ingest()
        if payload:
            index = build_keyword_index_from_snapshot_payload(payload)
            if index:
                log(
                    "[Keyword] loaded snapshot index "
                    f"entries={len(payload.get('entries') or [])} aliases={len(index)} "
                    f"generated_at={payload.get('generated_at') or ''}"
                )
                return index
    return prefetch_keyword_index(tenant_token)


def ensure_keyword_records(
    keywords: List[Dict[str, Any]],
    tenant_token: str,
    keyword_index: Dict[str, KeywordRecord],
    keyword_lock: threading.Lock,
    first_seen_ms: Optional[int] = None,
) -> List[str]:
    table_id = clean_feishu_value(getattr(config, "FEISHU_KEYWORD_TABLE_ID", "")).strip()
    if not table_id:
        return []

    record_ids: List[str] = []
    for kw in keywords or []:
        if not isinstance(kw, dict):
            continue
        name = str(kw.get("name") or "").strip()
        type_ = str(kw.get("type") or "").strip().lower()
        keys = keyword_alias_index_keys(name)
        if not name or not keys:
            continue
        if _is_keyword_name_blocked(name):
            global _KEYWORD_NAME_BLOCKED_COUNT
            _KEYWORD_NAME_BLOCKED_COUNT += 1
            continue

        with keyword_lock:
            existing = next((keyword_index[key] for key in keys if key in keyword_index), None)
            if existing:
                record_ids.append(existing.record_id)
                continue

            fields = {
                config.KEYWORD_FIELD_CANONICAL_NAME: name,
                config.KEYWORD_FIELD_TYPE: type_,
                config.KEYWORD_FIELD_FIRST_SEEN: int(first_seen_ms or time.time() * 1000),
            }
            ok, record_id = create_bitable_record_with_id(
                config.FEISHU_APP_TOKEN,
                table_id,
                tenant_token,
                fields,
                config.HTTP_TIMEOUT,
                config.HTTP_RETRIES,
            )
            if not ok or not record_id:
                log(f"[Keyword] create failed name={name}")
                continue
            keyword_record = KeywordRecord(record_id=record_id, canonical_name=name, type=type_)
            for key in keys:
                keyword_index[key] = keyword_record
            persist_keyword_runtime_snapshot_record(keyword_record)
            record_ids.append(record_id)
    return expand_keyword_record_ids(record_ids, keyword_index)


def build_news_fields(
    article: Dict[str, Any],
    analysis: Dict[str, Any],
    item_key: str,
    keyword_record_ids: Optional[List[str]] = None,
    image_file_tokens: Optional[List[str]] = None,
) -> Dict[str, Any]:
    base = build_article_base_fields(article, item_key)
    title_zh = (analysis.get("title_zh") or "").strip()
    title_text = title_zh if title_zh else base["title"]

    score = parse_score(analysis.get("score"))
    categories = analysis.get("categories") or []
    if not isinstance(categories, list):
        categories = [str(categories)]

    qa = normalize_qa(analysis.get("qa") or [])
    summary = build_summary(qa)

    keyword_names = keyword_names_from_analysis(analysis)

    fact_summary = screen_fact_summary(analysis)

    fields = {
        config.NEWS_FIELD_TITLE: {"text": title_text, "link": base["link"]},
        config.NEWS_FIELD_SUMMARY: summary,
        config.NEWS_FIELD_PUBLISHED_MS: base["published_ts_ms"],
        config.NEWS_FIELD_SOURCE: base["source"],
        config.NEWS_FIELD_FULL_CONTENT: base["full_content"],
        config.NEWS_FIELD_ITEM_KEY: base["item_key"],
        config.NEWS_FIELD_KEYWORDS: format_keyword_names_text(keyword_names),
    }
    if score is not None:
        fields[config.NEWS_FIELD_SCORE] = score
    if categories:
        fields[config.NEWS_FIELD_CATEGORIES] = categories
    if fact_summary:
        fields[config.NEWS_FIELD_BRIEF_SUMMARY] = fact_summary
    links = keyword_link_values(keyword_record_ids)
    if links:
        fields[config.NEWS_FIELD_KEYWORD_RECORDS] = links
    image_values = format_image_attachment_tokens(image_file_tokens)
    if image_values:
        fields[config.NEWS_FIELD_IMAGES] = image_values
    return fields


def build_filtered_analysis(
    analysis: Dict[str, Any],
    filter_method: str,
    filter_reason: str,
    **meta: Any,
) -> Dict[str, Any]:
    out = dict(analysis)
    out["reason"] = str(filter_reason or "").strip()
    return attach_llm_meta(out, filter_method=filter_method, filter_reason=out["reason"], **meta)


def format_dedup_filter_table_reason(candidate: Optional[DedupCandidate]) -> str:
    if not candidate:
        return ""
    title = str(candidate.title or "").strip()
    summary = str(candidate.summary or "").strip()
    if title and summary:
        return f"相同新闻：{title}\n摘要：{summary}"
    if title:
        return f"相同新闻：{title}"
    if summary:
        return f"相同新闻摘要：{summary}"
    return ""


def build_filtered_fields(
    article: Dict[str, Any],
    analysis: Dict[str, Any],
    item_key: str,
    keyword_record_ids: Optional[List[str]] = None,
    image_file_tokens: Optional[List[str]] = None,
) -> Dict[str, Any]:
    base = build_article_base_fields(article, item_key)
    llm_meta = get_llm_meta(analysis)
    filter_method = str(llm_meta.get("filter_method") or "").strip()
    if not filter_method:
        filter_method = "关键词过滤" if llm_meta.get("keyword_filtered") else "初筛过滤"
    keyword_hit = str(llm_meta.get("keyword_hit") or "").strip()
    filter_table_reason = str(llm_meta.get("filter_table_reason") or "").strip()
    if filter_method == "LLM文本去重":
        filter_reason = filter_table_reason
    else:
        filter_reason = str(llm_meta.get("filter_reason") or analysis.get("reason") or "").strip()
    if keyword_hit:
        filter_reason = f"{filter_reason}\n命中关键词：{keyword_hit}"

    keyword_names = keyword_names_from_analysis(analysis)

    title_zh = str(analysis.get("title_zh") or "").strip()
    title_text = title_zh if title_zh else base["title"]
    summary = (
        screen_fact_summary(analysis)
        or fallback_filtered_summary(base)
    )

    fields = {
        config.FILTERED_FIELD_TITLE: {"text": title_text, "link": base["link"]},
        config.FILTERED_FIELD_FILTER_METHOD: filter_method,
        config.FILTERED_FIELD_FILTER_REASON: filter_reason,
        config.FILTERED_FIELD_PUBLISHED_MS: base["published_ts_ms"],
        config.FILTERED_FIELD_SOURCE: base["source"],
        config.FILTERED_FIELD_FULL_CONTENT: base["full_content"],
        config.FILTERED_FIELD_ITEM_KEY: base["item_key"],
        config.FILTERED_FIELD_KEYWORDS: format_keyword_names_text(keyword_names),
    }
    if summary:
        fields[config.FILTERED_FIELD_SUMMARY] = summary
    links = keyword_link_values(keyword_record_ids)
    if links:
        fields[config.FILTERED_FIELD_KEYWORD_RECORDS] = links
    image_values = format_image_attachment_tokens(image_file_tokens)
    if image_values:
        fields[config.FILTERED_FIELD_IMAGES] = image_values
    return fields


def create_record_with_keyword_multiselect_fallback(
    app_token: str,
    table_id: str,
    tenant_token: str,
    fields: Dict[str, Any],
    keyword_field: str,
    timeout: int,
    retries: int,
    fallback_fields: Optional[List[str]] = None,
) -> Tuple[bool, Optional[str]]:
    ok, record_id = create_bitable_record_with_id(
        app_token,
        table_id,
        tenant_token,
        fields,
        timeout,
        retries,
    )
    if ok:
        return ok, record_id

    removable = [field for field in [keyword_field, *(fallback_fields or [])] if field in fields and fields.get(field)]
    attempted: set = set()
    for field in removable:
        retry_fields = dict(fields)
        retry_fields.pop(field, None)
        key = tuple(sorted(retry_fields.keys()))
        if key in attempted:
            continue
        attempted.add(key)
        log(f"[Feishu] create failed with {field}; retrying without field")
        ok, record_id = create_bitable_record_with_id(
            app_token,
            table_id,
            tenant_token,
            retry_fields,
            timeout,
            retries,
        )
        if ok:
            return ok, record_id

    if len(removable) >= 2:
        retry_fields = dict(fields)
        for field in removable:
            retry_fields.pop(field, None)
        key = tuple(sorted(retry_fields.keys()))
        if key not in attempted:
            log("[Feishu] create failed with optional fields; retrying without all optional fields")
            return create_bitable_record_with_id(
                app_token,
                table_id,
                tenant_token,
                retry_fields,
                timeout,
                retries,
            )
    return False, None


def persist_filtered_article(
    article: Dict[str, Any],
    analysis: Dict[str, Any],
    item_key: str,
    tenant_token: str,
    keyword_record_ids: Optional[List[str]] = None,
) -> bool:
    if not is_filtered_table_enabled():
        return False
    image_file_tokens = upload_article_images_for_attachment(article, tenant_token)
    ok, _ = create_record_with_keyword_multiselect_fallback(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_FILTERED_TABLE_ID,
        tenant_token,
        build_filtered_fields(
            article,
            analysis,
            item_key,
            keyword_record_ids=keyword_record_ids,
            image_file_tokens=image_file_tokens,
        ),
        config.FILTERED_FIELD_KEYWORDS,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        fallback_fields=[config.FILTERED_FIELD_IMAGES],
    )
    return ok


def should_skip_filtered_table(article: Dict[str, Any]) -> bool:
    source = str(article.get("source") or "").lower()
    link = str(article.get("link") or "").lower()
    return "linux do" in source or "linux.do" in source or "linux.do" in link


def record_filtered_outcome(
    article: Dict[str, Any],
    analysis: Dict[str, Any],
    item_key: str,
    tenant_token: str,
    existing_keys: set,
    stats: Dict[str, int],
    lock: threading.Lock,
    keyword_record_ids: Optional[List[str]] = None,
) -> bool:
    logged = False
    skip_filtered_table = should_skip_filtered_table(article)
    if is_filtered_table_enabled() and not skip_filtered_table:
        logged = persist_filtered_article(article, analysis, item_key, tenant_token, keyword_record_ids=keyword_record_ids)
    processed = skip_filtered_table or not is_filtered_table_enabled() or logged
    with lock:
        if skip_filtered_table:
            stats["filtered_skipped"] = stats.get("filtered_skipped", 0) + 1
        elif is_filtered_table_enabled():
            if logged:
                stats["filtered_logged"] += 1
            else:
                stats["filtered_log_failed"] += 1
        if processed:
            existing_keys.add(item_key)
    return processed


def sync_app_token() -> str:
    token = clean_feishu_value(getattr(config, "FEISHU_SYNC_APP_TOKEN", "")).strip()
    if token:
        return token
    return config.FEISHU_APP_TOKEN


def _item_key_time_filter(field_name: str, since_ms: int) -> Dict[str, Any]:
    return {
        "conjunction": "and",
        "conditions": [
            {
                "field_name": field_name,
                "operator": "isGreater",
                "value": ["ExactDate", str(int(since_ms))],
            }
        ],
    }


def compute_item_key_prefetch_since_ms(
    sources: List[Dict[str, Any]],
    now_ms: Optional[int] = None,
) -> int:
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    cursors = []
    for source in sources or []:
        cursor = int(source.get("last_item_pub_time") or source.get("last_fetch_time") or 0)
        if cursor > 0:
            cursors.append(cursor)
    if cursors:
        lookback_ms = max(0, int(getattr(config, "RSS_FETCH_LOOKBACK_MINUTES", 0) or 0)) * 60 * 1000
        return max(0, min(cursors) - lookback_ms)
    default_days = max(1, int(getattr(config, "ITEM_KEY_PREFETCH_DEFAULT_DAYS", 30) or 30))
    return max(0, now_ms - default_days * 86400 * 1000)


def prefetch_item_key_record_map(
    table_id: str,
    tenant_token: str,
    app_token: Optional[str] = None,
    since_ms: Optional[int] = None,
    published_field: Optional[str] = None,
    created_field: Optional[str] = None,
) -> Dict[str, str]:
    if not table_id:
        return {}
    target_app_token = app_token or config.FEISHU_APP_TOKEN
    records_by_id: Dict[str, Dict[str, Any]] = {}
    if since_ms is not None and (published_field or created_field):
        max_pages = max(1, int(getattr(config, "NEWS_ITEM_KEY_PREFETCH_MAX_PAGES", 50) or 50))
        for field_name in dict.fromkeys([published_field, created_field]):
            if not field_name:
                continue
            records = list_bitable_records(
                target_app_token,
                table_id,
                tenant_token,
                config.HTTP_TIMEOUT,
                config.HTTP_RETRIES,
                page_size=config.NEWS_ITEM_KEY_PREFETCH_LIMIT,
                max_pages=max_pages,
                filter_obj=_item_key_time_filter(field_name, int(since_ms)),
                sort=[{"field_name": field_name, "desc": True}],
                allow_partial=False,
            )
            for record in records:
                record_id = clean_feishu_value(record.get("record_id")).strip()
                if record_id:
                    records_by_id[record_id] = record
    else:
        sort_field = config.NEWS_FIELD_CREATED_TIME or config.NEWS_FIELD_PUBLISHED_MS
        records = list_bitable_records(
            target_app_token,
            table_id,
            tenant_token,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
            page_size=config.NEWS_ITEM_KEY_PREFETCH_LIMIT,
            max_pages=1,
            sort=[{"field_name": sort_field, "desc": True}],
            allow_partial=True,
        )
        for record in records:
            record_id = clean_feishu_value(record.get("record_id")).strip()
            if record_id:
                records_by_id[record_id] = record

    out: Dict[str, str] = {}
    for record in records_by_id.values():
        record_id = clean_feishu_value(record.get("record_id")).strip()
        fields = record.get("fields") or {}
        raw_key = fields.get(config.NEWS_FIELD_ITEM_KEY)
        key = clean_feishu_value(raw_key).strip()
        if key:
            out[key] = record_id
    return out


def prefetch_recent_item_keys(
    tenant_token: str,
    sources: Optional[List[Dict[str, Any]]] = None,
    now_ms: Optional[int] = None,
) -> set:
    since_ms = compute_item_key_prefetch_since_ms(sources, now_ms) if sources is not None else None
    keys = set(
        prefetch_item_key_record_map(
            config.FEISHU_NEWS_TABLE_ID,
            tenant_token,
            app_token=config.FEISHU_APP_TOKEN,
            since_ms=since_ms,
            published_field=config.NEWS_FIELD_PUBLISHED_MS if since_ms is not None else None,
            created_field=config.NEWS_FIELD_CREATED_TIME if since_ms is not None else None,
        ).keys()
    )
    if is_filtered_table_enabled():
        keys.update(
            prefetch_item_key_record_map(
                config.FEISHU_FILTERED_TABLE_ID,
                tenant_token,
                app_token=config.FEISHU_APP_TOKEN,
                since_ms=since_ms,
                published_field=config.FILTERED_FIELD_PUBLISHED_MS if since_ms is not None else None,
                created_field=config.FILTERED_FIELD_CREATED_TIME if since_ms is not None else None,
            ).keys()
        )
    return keys


def prefetch_recent_item_keys_with_retries(
    tenant_token: str,
    sources: Optional[List[Dict[str, Any]]] = None,
) -> set:
    attempts = max(1, int(getattr(config, "RSS_INGEST_ITEM_KEY_PREFETCH_ATTEMPTS", 2) or 2))
    last_err: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            if sources is None:
                return prefetch_recent_item_keys(tenant_token)
            return prefetch_recent_item_keys(tenant_token, sources=sources)
        except Exception as exc:
            last_err = exc
            log(f"[Dedup] item_key prefetch attempt {attempt}/{attempts} failed: {exc}")
            if attempt < attempts:
                time.sleep(min(8.0, 0.8 * attempt))
    raise RuntimeError(f"item_key prefetch failed after {attempts} attempts: {last_err}") from last_err


def sync_secondary_records(
    pending_items: List[Dict[str, Any]],
    tenant_token: str,
    secondary_table_id: str,
    secondary_record_map: Dict[str, str],
    stats: Dict[str, int],
    secondary_app_token: Optional[str] = None,
) -> None:
    if not pending_items:
        return
    app_token = secondary_app_token or sync_app_token()
    seen: set = set()
    for item in pending_items:
        item_key = clean_feishu_value(item.get("item_key")).strip()
        if not item_key or item_key in seen:
            continue
        seen.add(item_key)
        fields = item.get("fields") or {}
        record_id = secondary_record_map.get(item_key)
        ok = False
        if record_id:
            ok = update_bitable_record_fields(
                app_token,
                secondary_table_id,
                tenant_token,
                record_id,
                fields,
                config.HTTP_TIMEOUT,
                config.HTTP_RETRIES,
            )
        else:
            ok, new_record_id = create_bitable_record_with_id(
                app_token,
                secondary_table_id,
                tenant_token,
                fields,
                config.HTTP_TIMEOUT,
                config.HTTP_RETRIES,
            )
            if ok and new_record_id:
                secondary_record_map[item_key] = new_record_id
        if ok:
            stats["secondary_sync_ok"] += 1
        else:
            stats["secondary_sync_failed"] += 1
            log(f"[Sync] secondary sync failed item_key={item_key}")


def build_secondary_sync_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(fields)
    out.pop(config.NEWS_FIELD_KEYWORD_RECORDS, None)
    brief_summary = out.pop(config.NEWS_FIELD_BRIEF_SUMMARY, None)
    qa_summary = out.pop(config.NEWS_FIELD_SUMMARY, None)
    sync_summary_field = clean_feishu_value(getattr(config, "FEISHU_SYNC_FIELD_SUMMARY", "")).strip()
    summary_value = brief_summary if brief_summary not in (None, "") else qa_summary
    if sync_summary_field and summary_value not in (None, ""):
        out[sync_summary_field] = summary_value
    return out


def update_source_record_fields(
    tenant_token: str,
    record_id: str,
    fields: Dict[str, Any],
) -> bool:
    ok = update_bitable_record_fields(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_RSS_TABLE_ID,
        tenant_token,
        record_id,
        fields,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )
    if ok is not False or config.RSS_FIELD_WATCH_STATE not in fields:
        return ok is not False

    fallback_fields = dict(fields)
    fallback_fields.pop(config.RSS_FIELD_WATCH_STATE, None)
    log("[HTML] watch_state write rejected; retrying source update without watch_state")
    retry_ok = update_bitable_record_fields(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_RSS_TABLE_ID,
        tenant_token,
        record_id,
        fallback_fields,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )
    if retry_ok is not False:
        log("[HTML] add RSS source table text field 'watch_state' to persist html_watch cursors")
    return retry_ok is not False


def split_sources_and_queue(
    sources: List[Dict[str, Any]],
    existing_keys: set,
    tenant_token: str,
    all_sources: Optional[List[Dict[str, Any]]] = None,
) -> tuple[list, dict, dict]:
    queue: List[Dict[str, Any]] = []
    queued_item_keys = set(existing_keys)
    source_states: Dict[str, Dict[str, Any]] = {}
    stats = {
        "sources_processed": 0,
        "sources_skipped": 0,
        "sources_failed": 0,
        "entries_fetched": 0,
        "queue_total": 0,
        "aihot_allowed": 0,
        "aihot_allowed_twitter": 0,
        "aihot_allowed_selected": 0,
        "aihot_skipped_enabled": 0,
        "aihot_skipped_scope": 0,
    }
    html_host_locks: Dict[str, threading.Lock] = {}
    html_host_locks_guard = threading.Lock()

    def html_host_lock(url: str) -> threading.Lock:
        host = (urlparse(url or "").hostname or "").lower()
        with html_host_locks_guard:
            if host not in html_host_locks:
                html_host_locks[host] = threading.Lock()
            return html_host_locks[host]

    def fetch_source(source: Dict[str, Any], retry: bool = False) -> Dict[str, Any]:
        now_ms = int(time.time() * 1000)
        if not source.get("feed_url"):
            return {"status": "skipped", "source": source, "now_ms": now_ms}

        if not source.get("enabled"):
            update_bitable_record_fields(
                config.FEISHU_APP_TOKEN,
                config.FEISHU_RSS_TABLE_ID,
                tenant_token,
                source["record_id"],
                {
                    config.RSS_FIELD_STATUS: config.STATUS_IDLE,
                },
                config.HTTP_TIMEOUT,
                config.HTTP_RETRIES,
            )
            return {"status": "skipped", "source": source, "now_ms": now_ms}

        if not should_fetch(source, now_ms):
            return {"status": "skipped", "source": source, "now_ms": now_ms}

        consecutive_fail = source.get("consecutive_fail_count") or 0

        try:
            if is_html_watch_source(source):
                if not should_fetch_html_watch(
                    source,
                    now_ms,
                    interval_min=int(getattr(config, "HTML_WATCH_FETCH_INTERVAL_MIN", 10) or 10),
                ):
                    return {"status": "skipped", "source": source, "now_ms": now_ms}
                log(f"[HTML] {'retry fetching' if retry else 'fetching'} {source.get('name') or source.get('feed_url')}")
                with html_host_lock(source.get("feed_url") or ""):
                    html_result = fetch_html_watch(
                        source,
                        now_ms,
                        timeout=min(config.HTTP_TIMEOUT, 12),
                    )
                if html_result.status not in {"ok", "unchanged"}:
                    fail_count = consecutive_fail + 1
                    status = derive_overall_status(fail_count, True)
                    fetch_status = (
                        config.FETCH_STATUS_HTTP_ERROR
                        if html_result.status in {"rate_limited", "blocked"}
                        else config.FETCH_STATUS_PARSE_ERROR
                    )
                    update_source_record_fields(
                        tenant_token,
                        source["record_id"],
                        {
                            config.RSS_FIELD_STATUS: status,
                            config.RSS_FIELD_LAST_FETCH_STATUS: fetch_status,
                            config.RSS_FIELD_CONSECUTIVE_FAIL_COUNT: fail_count,
                            config.RSS_FIELD_LAST_FETCH_TIME: now_ms,
                            config.RSS_FIELD_WATCH_STATE: serialize_watch_state(html_result.watch_state),
                        },
                    )
                    log(f"[HTML] fetch failed {source['feed_url']}: {html_result.error}")
                    return {
                        "status": "skipped",
                        "source": source,
                        "now_ms": now_ms,
                        "fetch_status": fetch_status,
                        "error": html_result.error,
                    }
                feed = SimpleNamespace(entries=html_result.entries, feed={"title": source.get("name") or source.get("feed_url")})
                return {
                    "status": "ok",
                    "source": source,
                    "now_ms": now_ms,
                    "feed": feed,
                    "watch_state": html_result.watch_state,
                    "html_watch_status": html_result.status,
                }

            log(f"[RSS] {'retry fetching' if retry else 'fetching'} {source.get('name') or source.get('feed_url')}")
            host = (urlparse(source.get("feed_url") or "").netloc or "").lower()
            host_semaphore = host_semaphores.get(host)
            if host_semaphore is None:
                feed = fetch_feed(source["feed_url"], config.HTTP_TIMEOUT, config.HTTP_RETRIES, headers={"User-Agent": "NewsDataRSS/1.0"})
            else:
                with host_semaphore:
                    feed = fetch_feed(source["feed_url"], config.HTTP_TIMEOUT, config.HTTP_RETRIES, headers={"User-Agent": "NewsDataRSS/1.0"})
        except Exception as exc:
            fail_count = consecutive_fail + 1
            status = derive_overall_status(fail_count, True)
            fetch_status = derive_fetch_status(exc)
            update_bitable_record_fields(
                config.FEISHU_APP_TOKEN,
                config.FEISHU_RSS_TABLE_ID,
                tenant_token,
                source["record_id"],
                {
                    config.RSS_FIELD_STATUS: status,
                    config.RSS_FIELD_LAST_FETCH_STATUS: fetch_status,
                    config.RSS_FIELD_CONSECUTIVE_FAIL_COUNT: fail_count,
                    config.RSS_FIELD_LAST_FETCH_TIME: now_ms,
                },
                config.HTTP_TIMEOUT,
                config.HTTP_RETRIES,
            )
            log(f"[RSS] fetch failed {source['feed_url']}: {exc}")
            return {
                "status": "skipped",
                "source": source,
                "now_ms": now_ms,
                "fetch_status": fetch_status,
                "error": str(exc),
            }

        return {"status": "ok", "source": source, "now_ms": now_ms, "feed": feed}

    fetch_workers = max(1, int(getattr(config, "RSS_FETCH_CONCURRENCY", 1) or 1))
    per_host_workers = max(1, int(getattr(config, "RSS_FETCH_PER_HOST_CONCURRENCY", fetch_workers) or fetch_workers))
    host_semaphores: Dict[str, threading.BoundedSemaphore] = {}
    for source in sources:
        host = (urlparse(source.get("feed_url") or "").netloc or "").lower()
        if host and host not in host_semaphores:
            host_semaphores[host] = threading.BoundedSemaphore(per_host_workers)
    fetch_results: List[Optional[Dict[str, Any]]] = [None] * len(sources)
    if sources:
        log(f"[RSS] fetch concurrency={fetch_workers} per_host={per_host_workers}")
    with ThreadPoolExecutor(max_workers=fetch_workers) as executor:
        futures = {executor.submit(fetch_source, source): index for index, source in enumerate(sources)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                fetch_results[index] = future.result()
            except Exception as exc:
                source = sources[index]
                log(f"[RSS] fetch worker failed {source.get('feed_url')}: {exc}")
                fetch_results[index] = {
                    "status": "skipped",
                    "source": source,
                    "now_ms": int(time.time() * 1000),
                    "error": str(exc),
                }

    retry_sources = [
        (index, result["source"])
        for index, result in enumerate(fetch_results)
        if result
        and result.get("status") != "ok"
        and result.get("fetch_status") == config.FETCH_STATUS_TIMEOUT
        and result.get("source", {}).get("feed_url")
        and not is_html_watch_source(result.get("source", {}))
    ]
    retry_workers = max(1, int(getattr(config, "RSS_FETCH_RETRY_CONCURRENCY", 4) or 4))
    if retry_sources:
        log(f"[RSS] retry timeout sources={len(retry_sources)} concurrency={retry_workers} per_host={per_host_workers}")
        with ThreadPoolExecutor(max_workers=retry_workers) as executor:
            futures = {executor.submit(fetch_source, source, True): index for index, source in retry_sources}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    retry_result = future.result()
                except Exception as exc:
                    source = sources[index]
                    log(f"[RSS] retry worker failed {source.get('feed_url')}: {exc}")
                    continue
                if retry_result.get("status") == "ok":
                    fetch_results[index] = retry_result

    for result in fetch_results:
        if not result or result.get("status") != "ok":
            stats["sources_skipped"] += 1
            if not result or result.get("error") or result.get("fetch_status"):
                stats["sources_failed"] += 1
            continue

        source = result["source"]
        now_ms = result["now_ms"]
        feed = result["feed"]
        source_is_aihot = aihot_filter.is_aihot_source(source)
        aihot_filter_sources = all_sources or sources
        last_item_pub_time = source.get("last_item_pub_time") or 0
        cutoff_ms = last_item_pub_time or (source.get("last_fetch_time") or 0)
        lookback_minutes = max(0, int(getattr(config, "RSS_FETCH_LOOKBACK_MINUTES", 0) or 0))
        entry_cutoff_ms = max(0, cutoff_ms - lookback_minutes * 60 * 1000) if cutoff_ms else 0

        entries = feed.entries or []
        log(f"[RSS] fetched entries={len(entries)} for {source.get('name') or source.get('feed_url')}")
        stats["entries_fetched"] += len(entries)
        if config.MAX_ENTRIES_PER_FEED and len(entries) > config.MAX_ENTRIES_PER_FEED:
            entries = entries[: config.MAX_ENTRIES_PER_FEED]
        if source_is_aihot:
            entries = [aihot_filter.entry_for_ingest(entry, source=source) for entry in entries]

        failed_items = parse_failed_items(source.get("failed_items"))
        entry_map: Dict[str, Dict[str, Any]] = {}
        for entry in entries:
            entry_key = build_item_key(entry, source.get("item_id_strategy"), source.get("content_hash_algo"))
            if entry_key:
                entry_map[entry_key] = entry

        latest_pub_ms = int(last_item_pub_time or 0)
        latest_key = str(source.get("last_item_guid") or "") if latest_pub_ms else ""
        processed_keys: set = set()
        updated_failed_items: List[Dict[str, Any]] = []

        if failed_items:
            retry_budget = config.FAILED_ITEMS_RETRY_LIMIT
            for item in failed_items:
                item_key = item.get("item_key") or ""
                if not item_key:
                    continue
                entry = entry_map.get(item_key)
                if entry is None:
                    item["miss_count"] = int(item.get("miss_count") or 0) + 1
                    item["last_seen_ms"] = now_ms
                    updated_failed_items.append(item)
                    continue
                if item_key in queued_item_keys:
                    processed_keys.add(item_key)
                    continue
                if retry_budget <= 0:
                    updated_failed_items.append(item)
                    continue
                if source_is_aihot:
                    decision = aihot_filter.decide_aihot_entry(entry, aihot_filter_sources, source=source)
                    if decision.action != "allow":
                        processed_keys.add(item_key)
                        continue
                    stats["aihot_allowed"] += 1
                    if decision.reason == "twitter_entry":
                        stats["aihot_allowed_twitter"] += 1
                    elif decision.reason == "selected_uncovered_by_enabled_source":
                        stats["aihot_allowed_selected"] += 1
                retry_budget -= 1

                entry_ts = normalize_entry_published_ts(entry, now_ms)
                entry_ts_ms = entry_ts * 1000 if entry_ts else 0
                extraction = extract_article_text(
                    entry.get("link") or "",
                    source.get("name") or source.get("feed_url"),
                    source.get("feed_url") or "",
                    entry,
                    timeout=min(config.HTTP_TIMEOUT, 12),
                    force_fetch=source_is_aihot,
                )
                article = {
                    "title": entry.get("title") or "",
                    "content": extraction.get("text") or "",
                    "link": entry.get("link") or "",
                    "published": entry_ts,
                    "source": source.get("name") or source.get("feed_url"),
                    "extraction": extraction,
                }
                if is_aipoju_article(article):
                    article["image_urls"] = collect_article_image_urls(article, entry=entry)

                queue.append(
                    {
                        "source_id": source["record_id"],
                        "item_key": item_key,
                        "article": article,
                        "entry_ts": entry_ts,
                        "entry_ts_ms": entry_ts_ms,
                        "from_failed": True,
                    }
                )
                queued_item_keys.add(item_key)
                processed_keys.add(item_key)

                if entry_ts_ms > latest_pub_ms:
                    latest_pub_ms = entry_ts_ms
                    latest_key = item_key

        for entry in entries:
            entry_ts = normalize_entry_published_ts(entry, now_ms)
            entry_ts_ms = entry_ts * 1000 if entry_ts else 0
            if entry_ts_ms and entry_cutoff_ms and entry_ts_ms <= entry_cutoff_ms:
                continue

            item_key = build_item_key(entry, source.get("item_id_strategy"), source.get("content_hash_algo"))
            if not item_key:
                continue
            if entry_ts_ms > latest_pub_ms:
                latest_pub_ms = entry_ts_ms
                latest_key = item_key
            if item_key in processed_keys:
                continue
            if item_key in queued_item_keys:
                continue

            if source_is_aihot:
                decision = aihot_filter.decide_aihot_entry(entry, aihot_filter_sources, source=source)
                if decision.action != "allow":
                    if decision.reason == "covered_by_enabled_source":
                        stats["aihot_skipped_enabled"] += 1
                    else:
                        stats["aihot_skipped_scope"] += 1
                    continue
                stats["aihot_allowed"] += 1
                if decision.reason == "twitter_entry":
                    stats["aihot_allowed_twitter"] += 1
                elif decision.reason == "selected_uncovered_by_enabled_source":
                    stats["aihot_allowed_selected"] += 1

            extraction = extract_article_text(
                entry.get("link") or "",
                source.get("name") or source.get("feed_url"),
                source.get("feed_url") or "",
                entry,
                timeout=min(config.HTTP_TIMEOUT, 12),
                force_fetch=source_is_aihot,
            )
            article = {
                "title": entry.get("title") or "",
                "content": extraction.get("text") or "",
                "link": entry.get("link") or "",
                "published": entry_ts,
                "source": source.get("name") or source.get("feed_url"),
                "extraction": extraction,
            }
            if is_aipoju_article(article):
                article["image_urls"] = collect_article_image_urls(article, entry=entry)

            queue.append(
                {
                    "source_id": source["record_id"],
                    "item_key": item_key,
                    "article": article,
                    "entry_ts": entry_ts,
                    "entry_ts_ms": entry_ts_ms,
                    "from_failed": False,
                }
            )
            queued_item_keys.add(item_key)

        source_states[source["record_id"]] = {
            "source": source,
            "now_ms": now_ms,
            "latest_pub_ms": latest_pub_ms,
            "latest_key": latest_key,
            "updated_failed_items": updated_failed_items,
            "new_count": 0,
            "watch_state": result.get("watch_state"),
        }
        stats["sources_processed"] += 1

    stats["queue_total"] = len(queue)
    return queue, source_states, stats


def run_llm_queue(
    queue: List[Dict[str, Any]],
    source_states: Dict[str, Dict[str, Any]],
    tenant_token: str,
    existing_keys: set,
    stats: Dict[str, int],
    prompt_config: Optional[Dict[str, Any]] = None,
    secondary_pending_items: Optional[List[Dict[str, Any]]] = None,
    keyword_index: Optional[Dict[str, KeywordRecord]] = None,
) -> None:
    total = len(queue)
    if total <= 0:
        log("[LLM] queue empty")
        return

    stats.setdefault("llm_filtered", 0)
    stats.setdefault("llm_gemini_used", 0)
    stats.setdefault("llm_deepseek_used", 0)
    stats.setdefault("filtered_logged", 0)
    stats.setdefault("filtered_log_failed", 0)
    stats.setdefault("filtered_skipped", 0)
    stats.setdefault("entries_low_score", 0)
    stats.setdefault("entries_written", 0)
    stats.setdefault("text_dedup_skipped", 0)
    lock = threading.Lock()
    keyword_lock = threading.Lock()
    dedup_store = load_dedup_store(tenant_token) if ENABLE_TEXT_DEDUP else DedupCandidateStore()
    keyword_index = keyword_index if keyword_index is not None else {}

    def handle_item(item: Dict[str, Any]) -> None:
        state = source_states[item["source_id"]]
        article = item["article"]

        def remember_write_failure(reason: str) -> None:
            upsert_failed_item(
                state["updated_failed_items"],
                item["item_key"],
                item["entry_ts_ms"],
                article.get("title") or "",
                article.get("link") or "",
                reason,
                state["now_ms"],
            )

        analysis = _analyze_with_llm_compat(
            article,
            prompt_config=prompt_config,
            include_summary=False,
        )
        provider_used = analysis_provider_used(analysis)
        with lock:
            if provider_used == "gemini":
                stats["llm_gemini_used"] += 1
            elif provider_used == "deepseek":
                stats["llm_deepseek_used"] += 1
        if get_analysis_action(analysis) == "pass":
            if should_skip_filtered_table(article):
                keyword_record_ids = []
            else:
                keyword_record_ids = ensure_keyword_records(
                    analysis.get("keywords") or [],
                    tenant_token,
                    keyword_index,
                    keyword_lock,
                    first_seen_ms=item.get("entry_ts_ms"),
                )
            with lock:
                stats["llm_filtered"] += 1
            recorded = record_filtered_outcome(
                article,
                analysis,
                item["item_key"],
                tenant_token,
                existing_keys,
                stats,
                lock,
                keyword_record_ids=keyword_record_ids,
            )
            if not recorded:
                with lock:
                    remember_write_failure("filtered_create_failed")
            return
        categories = analysis.get("categories") or []
        if isinstance(categories, list) and any(c in FAILED_CATEGORIES for c in categories):
            with lock:
                stats["llm_failed"] += 1
                upsert_failed_item(
                    state["updated_failed_items"],
                    item["item_key"],
                    item["entry_ts_ms"],
                    article.get("title") or "",
                    article.get("link") or "",
                    str(analysis.get("summary") or "llm_failed"),
                    state["now_ms"],
                )
            return

        with lock:
            stats["llm_success"] += 1

        keyword_record_ids: List[str] = []

        def ensure_current_keyword_records() -> List[str]:
            nonlocal keyword_record_ids
            if not keyword_record_ids:
                keyword_record_ids = ensure_keyword_records(
                    analysis.get("keywords") or [],
                    tenant_token,
                    keyword_index,
                    keyword_lock,
                    first_seen_ms=item.get("entry_ts_ms"),
                )
            return keyword_record_ids

        score = parse_score(analysis.get("score"))
        created_news = False
        if score is None or score >= config.FEISHU_MIN_SCORE:
            if ENABLE_TEXT_DEDUP and dedup_store.size() > 0:
                title_zh = str(analysis.get("title_zh") or article.get("title") or "").strip()
                fact_summary = screen_fact_summary(analysis)
                kw_names = keyword_names_from_analysis(analysis)
                keywords_str = ", ".join(kw_names)
                current_keyword_record_ids = [] if should_skip_filtered_table(article) else ensure_current_keyword_records()
                candidates_text, candidate_by_id = dedup_store.build_candidates_context(
                    exclude_item_key=item["item_key"],
                    keywords=kw_names,
                    keyword_record_ids=current_keyword_record_ids,
                    max_candidates=TEXT_DEDUP_MAX_CANDIDATES,
                )
                match = llm_dedup_check(
                    title_zh, fact_summary, keywords_str, candidates_text,
                )

                if match:
                    reason_text = match.get("reason") or "LLM判定重复"
                    matched_id = match.get("matched_id") or ""
                    matched_title = match.get("matched_title") or ""
                    log(f"[TextDedup] LLM duplicate matched={matched_id} title={title_zh[:60]}")
                    matched_suffix = f"{matched_id}：{matched_title}" if matched_title else str(matched_id)
                    dedup_reason = f"LLM文本去重：{reason_text}（匹配 {matched_suffix}）"
                    matched_candidate = candidate_by_id.get(str(matched_id))
                    dedup_analysis = dict(analysis)
                    if fact_summary and not dedup_analysis.get("summary"):
                        dedup_analysis["summary"] = fact_summary
                    filtered_analysis = build_filtered_analysis(
                        dedup_analysis, "LLM文本去重", dedup_reason,
                        text_dedup=True,
                        filter_table_reason=format_dedup_filter_table_reason(matched_candidate),
                    )
                    recorded = record_filtered_outcome(
                        article, filtered_analysis, item["item_key"],
                        tenant_token, existing_keys, stats, lock,
                        keyword_record_ids=current_keyword_record_ids,
                    )
                    if not recorded:
                        with lock:
                            remember_write_failure("filtered_create_failed")
                        return
                    with lock:
                        stats["text_dedup_skipped"] += 1
                    return

            if not normalize_qa(analysis.get("qa") or []):
                analysis = summarize_with_llm(
                    article,
                    analysis,
                    prompt_config=prompt_config,
                )
                summary_categories = analysis.get("categories") or []
                if isinstance(summary_categories, list) and any(c in FAILED_CATEGORIES for c in summary_categories):
                    with lock:
                        stats["llm_failed"] += 1
                        upsert_failed_item(
                            state["updated_failed_items"],
                            item["item_key"],
                            item["entry_ts_ms"],
                            article.get("title") or "",
                            article.get("link") or "",
                            str(analysis.get("summary") or "summary_failed"),
                            state["now_ms"],
                        )
                    return

            image_file_tokens = upload_article_images_for_attachment(article, tenant_token)
            fields = build_news_fields(
                article,
                analysis,
                item["item_key"],
                keyword_record_ids=ensure_current_keyword_records(),
                image_file_tokens=image_file_tokens,
            )
            ok, _ = create_record_with_keyword_multiselect_fallback(
                config.FEISHU_APP_TOKEN,
                config.FEISHU_NEWS_TABLE_ID,
                tenant_token,
                fields,
                config.NEWS_FIELD_KEYWORDS,
                config.HTTP_TIMEOUT,
                config.HTTP_RETRIES,
                fallback_fields=[config.NEWS_FIELD_IMAGES],
            )
            if not ok:
                with lock:
                    stats["feishu_create_failed"] += 1
                    remember_write_failure("news_create_failed")
            else:
                created_news = True
                if ENABLE_TEXT_DEDUP:
                    t_zh = str(analysis.get("title_zh") or article.get("title") or "").strip()
                    b_sum = screen_fact_summary(analysis)
                    kw_n = keyword_names_from_analysis(analysis)
                    with lock:
                        dedup_store.add(
                            item["item_key"],
                            t_zh,
                            b_sum,
                            ", ".join(kw_n),
                            keyword_record_ids=keyword_record_ids,
                        )
                if secondary_pending_items is not None:
                    with lock:
                        secondary_fields = build_secondary_sync_fields(fields)
                        secondary_pending_items.append({"item_key": item["item_key"], "fields": secondary_fields})
        else:
            low_score = float(score or 0.0)
            low_score_reason = f"低于入库阈值：{low_score:.1f} < {config.FEISHU_MIN_SCORE:.1f}"
            original_reason = str(analysis.get("reason") or "").strip()
            if original_reason:
                low_score_reason = f"{low_score_reason}；{original_reason}"
            filtered_analysis = build_filtered_analysis(
                analysis,
                "低分淘汰",
                low_score_reason,
                low_score_filtered=True,
                score=low_score,
            )
            recorded = record_filtered_outcome(
                article,
                filtered_analysis,
                item["item_key"],
                tenant_token,
                existing_keys,
                stats,
                lock,
                keyword_record_ids=[] if should_skip_filtered_table(article) else ensure_current_keyword_records(),
            )
            with lock:
                if not recorded:
                    remember_write_failure("filtered_create_failed")
                stats["entries_low_score"] += 1
        with lock:
            stats["entries_processed"] += 1
            if created_news:
                existing_keys.add(item["item_key"])
                stats["entries_written"] += 1
                stats["entries_new"] += 1
                state["new_count"] += 1

    def consume_futures(future_items: Dict[Any, Dict[str, Any]]) -> None:
        done = 0
        for future in as_completed(future_items):
            item = future_items[future]
            try:
                future.result()
            except Exception as exc:
                with lock:
                    stats["llm_failed"] += 1
                    state = source_states[item["source_id"]]
                    article = item["article"]
                    upsert_failed_item(
                        state["updated_failed_items"],
                        item["item_key"],
                        item["entry_ts_ms"],
                        article.get("title") or "",
                        article.get("link") or "",
                        f"worker_exception:{type(exc).__name__}:{exc}",
                        state["now_ms"],
                    )
                log(f"[LLM] task failed: {exc}")
            done += 1
            bar = render_progress(done, total, width=config.PROGRESS_BAR_WIDTH)
            msg = f"[LLM] {bar} ok={stats['llm_success']} filtered={stats['llm_filtered']} fail={stats['llm_failed']}"
            if sys.stdout.isatty():
                sys.stdout.write("\r" + msg)
                sys.stdout.flush()
            else:
                log(msg)
        if sys.stdout.isatty():
            sys.stdout.write("\n")
            sys.stdout.flush()

    with ThreadPoolExecutor(max_workers=config.LLM_CONCURRENCY) as executor:
        future_items = {executor.submit(handle_item, item): item for item in queue}
        consume_futures(future_items)

def main() -> int:
    required = []
    if not config.FEISHU_APP_ID:
        required.append("FEISHU_APP_ID")
    if not config.FEISHU_APP_SECRET:
        required.append("FEISHU_APP_SECRET")
    if not config.FEISHU_APP_TOKEN:
        required.append("FEISHU_APP_TOKEN")
    if not config.FEISHU_NEWS_TABLE_ID:
        required.append("FEISHU_NEWS_TABLE_ID")
    if not config.FEISHU_RSS_TABLE_ID:
        required.append("FEISHU_RSS_TABLE_ID")
    if required:
        notify_config_missing("missing: " + ", ".join(required))
        log(f"[Config] missing: {', '.join(required)}")
        return 1
    try:
        tenant_token = get_tenant_access_token(
            config.FEISHU_APP_ID,
            config.FEISHU_APP_SECRET,
            config.HTTP_TIMEOUT,
            config.HTTP_RETRIES,
        )
    except Exception as exc:
        notify_auth_failure("Feishu", f"get tenant token failed: {exc}")
        log(f"[Feishu] get tenant token failed: {exc}")
        return 1
    set_notify_tenant_token(tenant_token)
    try:
        prompt_config = load_local_prompt_sections()
        global _KEYWORD_NAME_BLOCKLIST
        _KEYWORD_NAME_BLOCKLIST = prompt_config.get("keyword_name_blocklist") or set()
        log(
            "[Prompt] loaded local rules "
            f"path={prompt_config.get('path', config.LOCAL_PROMPT_RULES_PATH)} "
            f"keywords={len(prompt_config.get('keyword_blocklist') or [])} "
            f"name_blocklist={len(_KEYWORD_NAME_BLOCKLIST)}"
        )
    except Exception as exc:
        notify_config_missing(f"prompt rules load failed: {exc}")
        log(f"[Prompt] load failed: {exc}")
        return 1

    secondary_sync_enabled = bool(config.ENABLE_SECONDARY_SYNC and config.FEISHU_SYNC_TABLE_ID)
    if config.ENABLE_SECONDARY_SYNC and not config.FEISHU_SYNC_TABLE_ID:
        log("[Sync] secondary sync disabled, missing FEISHU_SYNC_TABLE_ID")
    secondary_app = sync_app_token() if secondary_sync_enabled else ""
    if secondary_sync_enabled and not getattr(config, "FEISHU_SYNC_APP_TOKEN", ""):
        log("[Sync] FEISHU_SYNC_APP_TOKEN not set, fallback to FEISHU_APP_TOKEN")

    records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_RSS_TABLE_ID,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )

    sources = [normalize_source(r) for r in records if r.get("record_id")]
    try:
        runtime_selection = prepare_sources_for_runtime(
            sources,
            mode=getattr(config, "RSS_SOURCE_MODE", "all"),
            override_file=getattr(config, "RSS_SOURCE_OVERRIDE_FILE", ""),
        )
    except SourceRuntimeConfigError as exc:
        log(f"[RSS] runtime source configuration failed: {exc}")
        return 1
    sources = runtime_selection.sources
    if runtime_selection.skipped or runtime_selection.overrides_applied:
        log(
            "[RSS] runtime source routing "
            f"mode={getattr(config, 'RSS_SOURCE_MODE', 'all')} "
            f"overrides={runtime_selection.overrides_applied} "
            f"skipped={len(runtime_selection.skipped)}"
        )
        for skipped_source in runtime_selection.skipped:
            log(
                "[RSS] runtime skipped "
                f"{skipped_source.name or skipped_source.record_id}: {skipped_source.reason}"
            )
    enabled_sources = [s for s in sources if s.get("enabled")]
    log(f"[RSS] sources total={len(sources)} enabled={len(enabled_sources)}")
    try:
        existing_keys = prefetch_recent_item_keys_with_retries(tenant_token, enabled_sources)
        log(f"[Dedup] prefetched keys: {len(existing_keys)}")
    except Exception as exc:
        log(f"[Dedup] prefetch failed; aborting this run to avoid duplicate writes: {exc}")
        return 1

    keyword_index: Dict[str, KeywordRecord] = {}
    if clean_feishu_value(getattr(config, "FEISHU_KEYWORD_TABLE_ID", "")).strip():
        try:
            keyword_index = prefetch_keyword_index_for_ingest(tenant_token)
            log(f"[Keyword] prefetched aliases: {len(keyword_index)}")
        except Exception as exc:
            log(f"[Keyword] prefetch failed: {exc}")
            return 1

    secondary_record_map: Dict[str, str] = {}
    if secondary_sync_enabled:
        try:
            secondary_record_map = prefetch_item_key_record_map(
                config.FEISHU_SYNC_TABLE_ID,
                tenant_token,
                app_token=secondary_app,
            )
            log(f"[Sync] secondary prefetched keys: {len(secondary_record_map)}")
        except Exception as exc:
            log(f"[Sync] secondary prefetch failed: {exc}")
            secondary_record_map = {}

    queue, source_states, fetch_stats = split_sources_and_queue(enabled_sources, existing_keys, tenant_token, all_sources=sources)
    stats = {
        "llm_success": 0,
        "llm_filtered": 0,
        "llm_failed": 0,
        "llm_gemini_used": 0,
        "llm_deepseek_used": 0,
        "filtered_logged": 0,
        "filtered_log_failed": 0,
        "feishu_create_failed": 0,
        "entries_processed": 0,
        "entries_new": 0,
        "entries_written": 0,
        "entries_low_score": 0,
        "text_dedup_skipped": 0,
        "secondary_sync_ok": 0,
        "secondary_sync_failed": 0,
    }
    stats.update(fetch_stats)
    log(f"[Queue] total={stats['queue_total']} sources_processed={stats['sources_processed']} sources_skipped={stats['sources_skipped']}")

    secondary_pending_items: Optional[List[Dict[str, Any]]] = [] if secondary_sync_enabled else None
    run_llm_queue(
        queue,
        source_states,
        tenant_token,
        existing_keys,
        stats,
        prompt_config=prompt_config,
        secondary_pending_items=secondary_pending_items,
        keyword_index=keyword_index,
    )

    if secondary_sync_enabled and secondary_pending_items is not None:
        sync_secondary_records(
            secondary_pending_items,
            tenant_token,
            config.FEISHU_SYNC_TABLE_ID,
            secondary_record_map,
            stats,
            secondary_app_token=secondary_app,
        )

    for state in source_states.values():
        source = state["source"]
        pruned_failed_items = prune_failed_items(state["updated_failed_items"], state["now_ms"])
        latest_pub_ms, latest_key = cap_source_cursor_for_failed_items(
            state["latest_pub_ms"],
            state["latest_key"],
            pruned_failed_items,
        )
        update_fields: Dict[str, Any] = {
            config.RSS_FIELD_STATUS: config.STATUS_OK,
            config.RSS_FIELD_LAST_FETCH_STATUS: config.FETCH_STATUS_SUCCESS,
            config.RSS_FIELD_CONSECUTIVE_FAIL_COUNT: 0,
            config.RSS_FIELD_LAST_FETCH_TIME: state["now_ms"],
            config.RSS_FIELD_FAILED_ITEMS: serialize_failed_items(pruned_failed_items),
        }
        if latest_pub_ms:
            update_fields[config.RSS_FIELD_LAST_ITEM_PUB_TIME] = latest_pub_ms
        if latest_key:
            update_fields[config.RSS_FIELD_LAST_ITEM_GUID] = latest_key
        if state.get("watch_state") is not None:
            update_fields[config.RSS_FIELD_WATCH_STATE] = serialize_watch_state(state["watch_state"])

        update_source_record_fields(
            tenant_token,
            source["record_id"],
            update_fields,
        )
        log(f"[RSS] {source.get('name') or source.get('feed_url')} new={state['new_count']}")

    log(
        "[Summary] "
        f"sources_done={stats['sources_processed']} "
        f"sources_skipped={stats['sources_skipped']} "
        f"sources_failed={stats.get('sources_failed', 0)} "
        f"entries_fetched={stats['entries_fetched']} "
        f"queue_total={stats['queue_total']} "
        f"aihot_allowed={stats.get('aihot_allowed', 0)} "
        f"aihot_allowed_twitter={stats.get('aihot_allowed_twitter', 0)} "
        f"aihot_allowed_selected={stats.get('aihot_allowed_selected', 0)} "
        f"aihot_skipped_enabled={stats.get('aihot_skipped_enabled', 0)} "
        f"aihot_skipped_scope={stats.get('aihot_skipped_scope', 0)} "
        f"processed={stats['entries_processed']} "
        f"new={stats['entries_new']} "
        f"written={stats['entries_written']} "
        f"low_score={stats['entries_low_score']} "
        f"llm_ok={stats['llm_success']} "
        f"llm_filtered={stats['llm_filtered']} "
        f"llm_failed={stats['llm_failed']} "
        f"llm_gemini={stats['llm_gemini_used']} "
        f"llm_deepseek={stats['llm_deepseek_used']} "
        f"filtered_logged={stats['filtered_logged']} "
        f"filtered_log_failed={stats['filtered_log_failed']} "
        f"feishu_failed={stats['feishu_create_failed']} "
        f"text_dedup_skipped={stats['text_dedup_skipped']} "
        f"sync_ok={stats['secondary_sync_ok']} "
        f"sync_failed={stats['secondary_sync_failed']}"
    )
    fatal_source_failures = source_failures_are_fatal(stats)
    if stats.get("sources_failed") and not fatal_source_failures:
        attempted_sources = int(stats.get("sources_processed", 0) or 0) + int(stats.get("sources_skipped", 0) or 0)
        log(
            "[RSS] degraded source failures tolerated "
            f"failed={stats['sources_failed']} attempted={attempted_sources}"
        )
    failure_fields = (
        "feishu_create_failed",
        "filtered_log_failed",
        "secondary_sync_failed",
    )
    has_processing_failure = any(int(stats.get(field, 0) or 0) > 0 for field in failure_fields)
    return 1 if fatal_source_failures or has_processing_failure else 0


def source_failures_are_fatal(stats: Dict[str, int]) -> bool:
    failed = max(0, int(stats.get("sources_failed", 0) or 0))
    if failed <= 0:
        return False
    processed = max(0, int(stats.get("sources_processed", 0) or 0))
    skipped = max(0, int(stats.get("sources_skipped", 0) or 0))
    if processed <= 0:
        return True
    attempted = max(1, processed + skipped)
    count_limit = max(1, int(getattr(config, "RSS_SOURCE_FAILURE_EXIT_COUNT", 10) or 10))
    ratio_limit = float(getattr(config, "RSS_SOURCE_FAILURE_EXIT_RATIO", 0.25) or 0.25)
    ratio_limit = min(1.0, max(0.0, ratio_limit))
    return failed >= count_limit and failed / attempted >= ratio_limit


def cli_entrypoint(run_log_path: Optional[str] = None) -> int:
    code = 1
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    run_log_file = None
    resolved_log_path: Optional[Path] = None
    effective_log_path = str(
        run_log_path or os.getenv("RSS_INGEST_ALERT_LOG_PATH", "")
    ).strip()
    if effective_log_path:
        candidate = Path(effective_log_path).expanduser()
        resolved_log_path = candidate if candidate.is_absolute() else config.BASE_DIR / candidate
        try:
            resolved_log_path.parent.mkdir(parents=True, exist_ok=True)
            run_log_file = resolved_log_path.open("a", encoding="utf-8", buffering=1)
            sys.stdout = _TeeStream(original_stdout, run_log_file)
            sys.stderr = _TeeStream(original_stderr, run_log_file)
        except OSError as exc:
            print(f"[RSS] run log open failed: {exc}", file=original_stderr, flush=True)
            run_log_file = None
    try:
        log(f"[runner] rss-ingest started {dt.datetime.now().isoformat(timespec='seconds')}")
        code = run_with_single_instance_lock()
        return code
    except Exception as exc:
        log(f"[RSS] fatal error: {type(exc).__name__}: {exc}")
        raise
    finally:
        log(f"[runner] rss-ingest finished exit={code} {dt.datetime.now().isoformat(timespec='seconds')}")
        if run_log_file is not None:
            sys.stdout.flush()
            sys.stderr.flush()
        if code:
            try:
                import task_alerts

                alert_kwargs = {"log_path": str(resolved_log_path)} if resolved_log_path else {}
                task_alerts.notify_failure("rss-ingest-fetch", code, **alert_kwargs)
            except Exception as exc:
                log(f"[RSS] failure alert error (ignored): {exc}")
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        if run_log_file is not None:
            run_log_file.close()


if __name__ == "__main__":
    configured_run_log = os.getenv("RSS_INGEST_ALERT_LOG_PATH", "").strip()
    default_run_log = (
        config.BASE_DIR
        / "out"
        / "rss-ingest"
        / "logs"
        / f"rss-ingest-{dt.datetime.now():%Y%m%d}.log"
    )
    raise SystemExit(cli_entrypoint(configured_run_log or str(default_run_log)))
