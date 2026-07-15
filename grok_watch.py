"""grok_watch: 定时调 Grok 搜 X 真料，写本地 RSS 喂给 rss_ingest。

独立于 RSS 主流程，不依赖飞书。产物 data/grok-feeds/<topic>.xml。
默认使用网页端 grok.com + gpt-browser 登录态；CLI/API transport 必须显式开启。
已知约束：Grok 返回的 author/posted_at 不可信，一律以 fxtwitter 回查为准。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
from email.utils import formatdate
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

import requests

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_TOPICS_PATH = PROJECT_ROOT / "docs" / "local-grok-topics.json"
DEFAULT_STATE_PATH = PROJECT_ROOT / "data" / "grok_watch_state.json"
DEFAULT_FEED_DIR = PROJECT_ROOT / "data" / "grok-feeds"
DEFAULT_LOCK_PATH = PROJECT_ROOT / "data" / ".grok_watch.lock"
DEFAULT_GROK_CWD = Path.home() / ".grok-search" / "watch-cwd"
DEFAULT_GROK_BROWSER_CLI = PROJECT_ROOT.parent / "solo-company" / "skills" / "grok-browser" / "bin" / "cli.js"
DEFAULT_LEGACY_GROK_WEB_RUNNER = PROJECT_ROOT / "tools" / "grok_web_runner.js"


def _default_gpt_browser_endpoint_file() -> str:
    local_app_data = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return str(Path(local_app_data) / "gpt-browser" / "state" / "chrome-ws-endpoint.txt")


def _default_gpt_browser_node_modules() -> str:
    app_data = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return str(Path(app_data) / "npm" / "node_modules" / "gpt-browser" / "node_modules")


def _load_env_file(path: Path) -> None:
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "y"}


if not _env_truthy("RSS_INGEST_SKIP_LOCAL_ENV"):
    _load_env_file(PROJECT_ROOT / "rss-ingest-local.env")
    _load_env_file(PROJECT_ROOT.parent / "local.env")
    _load_env_file(PROJECT_ROOT.parent / ".env")


GROK_COMMAND = os.environ.get("GROK_WATCH_COMMAND", "grok")
GROK_TIMEOUT_S = int(os.environ.get("GROK_WATCH_TIMEOUT_S", "420"))
GROK_MAX_TURNS = int(os.environ.get("GROK_WATCH_MAX_TURNS", "12"))
GROK_API_URL = os.environ.get("GROK_WATCH_API_URL", "https://api.x.ai/v1/responses").strip()
GROK_API_TIMEOUT_S = int(os.environ.get("GROK_WATCH_API_TIMEOUT_S", str(GROK_TIMEOUT_S)))
# 专用 leader socket：grok CLI 的 leader 进程在交互式会话 / subagent-cc / grok_watch
# 间共享时，leader 生命周期变化会向客户端进程组广播控制台信号，无头环境下表现为
# 偶发 0xC000013A 整组被杀。隔离 leader 后互不影响。
GROK_LEADER_SOCKET = os.environ.get(
    "GROK_WATCH_LEADER_SOCKET",
    str(Path.home() / ".grok-search" / "leader-watch.sock"),
)
GROK_CWD = Path(os.environ.get("GROK_WATCH_CWD", str(DEFAULT_GROK_CWD)))
GROK_AGENT_COMMAND = os.environ.get("GROK_WATCH_AGENT_COMMAND", "")
GROK_WEB_COMMAND = os.environ.get("GROK_WATCH_WEB_COMMAND", "node").strip() or "node"
GROK_BROWSER_CLI = Path(os.environ.get("GROK_WATCH_GROK_BROWSER_CLI", str(DEFAULT_GROK_BROWSER_CLI)))
GROK_LEGACY_WEB_RUNNER = Path(os.environ.get("GROK_WATCH_WEB_RUNNER", str(DEFAULT_LEGACY_GROK_WEB_RUNNER)))
GROK_ALLOW_LEGACY_WEB_RUNNER = _env_truthy("GROK_WATCH_ALLOW_LEGACY_WEB_RUNNER")
GROK_WEB_ENDPOINT_FILE = os.environ.get(
    "GROK_WATCH_WEB_ENDPOINT_FILE",
    os.environ.get("BROWSER_ARTICLE_FETCH_ENDPOINT_FILE", "") or _default_gpt_browser_endpoint_file(),
).strip()
GROK_WEB_NODE_MODULES = os.environ.get(
    "GROK_WATCH_WEB_NODE_MODULES",
    os.environ.get("BROWSER_ARTICLE_FETCH_NODE_MODULES", "") or _default_gpt_browser_node_modules(),
).strip()
GROK_WEB_URL = os.environ.get(
    "GROK_WATCH_WEB_URL",
    "https://grok.com/?q=&reasoningMode=none&voice=false",
).strip()
GROK_WEB_MODEL = os.environ.get("GROK_WATCH_WEB_MODEL", "Expert").strip()
GROK_WEB_TIMEOUT_S = int(os.environ.get("GROK_WATCH_WEB_TIMEOUT_S", str(GROK_TIMEOUT_S)))
GROK_WEB_KEEP_PAGE = os.environ.get("GROK_WATCH_WEB_KEEP_PAGE", "").strip()
GROK_GPT_BROWSER_COMMAND = os.environ.get("GROK_WATCH_GPT_BROWSER_COMMAND", "gpt-browser").strip() or "gpt-browser"
GROK_GPT_BROWSER_LAUNCH_TIMEOUT_S = int(os.environ.get("GROK_WATCH_GPT_BROWSER_LAUNCH_TIMEOUT_S", "90"))
GROK_SYSTEM_PROMPT = (
    "You are a strict JSON-only social and AI news search worker. Use only Grok's native X/web search capability. "
    "Do not inspect tools, files, directories, MCP descriptors, or available skills. "
    "Do not use built-in local tools, web_fetch, browser automation, or page-opening tools. "
    "Do not invoke skills, plugins, hooks, subagents, MCP, browser automation, shell commands, local files, or external scripts. "
    "Do not open local browser tabs or social/news pages through browser automation. "
    "Return only the JSON requested by the user prompt."
)
GROK_DISALLOWED_TOOLS = "update_goal,Bash,PowerShell,Shell,MCPTool,Read,Write,Edit,Glob,Grep,LS"
FXTWITTER_TIMEOUT_S = 20
REDDIT_TIMEOUT_S = 20
SCHEDULE_GRACE_MINUTES = int(os.environ.get("GROK_WATCH_SCHEDULE_GRACE_MINUTES", "20"))
SEEN_MAX_AGE_DAYS = 14
FEED_ITEM_MAX_AGE_H = 72
FEED_MAX_ITEMS = 50
MAX_TOPICS_PER_RUN = int(os.environ.get("GROK_WATCH_MAX_TOPICS_PER_RUN", "2"))
DETACHED_PROCESS = 0x00000008
_LEADER_ENSURED = False

TOPIC_REQUIRED_FIELDS = ("key", "name", "interval_hours", "window_hours", "prompt_file")


class GrokTimeout(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[GrokWatch] {message}", flush=True)


def _win_flag(name: str) -> int:
    return int(getattr(subprocess, name, 0)) if os.name == "nt" else 0


def _grok_cli_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GROK_HOME", str(Path.home() / ".grok-search"))
    for vendor in ("CLAUDE", "CURSOR"):
        for cell in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS"):
            env[f"GROK_{vendor}_{cell}_ENABLED"] = "false"
    env["GROK_SUBAGENTS"] = "0"
    env["GROK_MEMORY"] = "0"
    return env


def _kill_tree_win(pid: int) -> None:
    if os.name != "nt":
        return
    try:
        proc = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=_win_flag("CREATE_NO_WINDOW"),
        )
    except Exception as exc:
        log(f"taskkill pid={pid} failed: {exc}")
        return
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        log(f"taskkill pid={pid} exit={proc.returncode}: {detail[-400:]}")


def _agent_command_for_grok(command: str) -> Optional[Path]:
    if GROK_AGENT_COMMAND:
        return Path(GROK_AGENT_COMMAND)
    command_path = Path(command)
    resolved = command_path if command_path.exists() else None
    if resolved is None:
        found = shutil.which(command)
        if found:
            resolved = Path(found)
    if resolved:
        return resolved.resolve().parent / "agent.exe"
    return Path.home() / ".grok" / "bin" / "agent.exe"


def _agent_running_via_tasklist() -> Optional[bool]:
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq agent.exe"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=_win_flag("CREATE_NO_WINDOW"),
        )
    except Exception as exc:
        log(f"tasklist agent.exe check failed: {exc}")
        return None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        log(f"tasklist agent.exe check exit={proc.returncode}: {detail[-300:]}")
        return None
    return "agent.exe" in (proc.stdout or "").lower()


def _agent_running_via_powershell() -> Optional[bool]:
    try:
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "if (Get-Process -Name agent -ErrorAction SilentlyContinue) { 'agent.exe' }",
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=_win_flag("CREATE_NO_WINDOW"),
        )
    except Exception as exc:
        log(f"powershell agent.exe check failed: {exc}")
        return None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        log(f"powershell agent.exe check exit={proc.returncode}: {detail[-300:]}")
        return None
    return "agent.exe" in (proc.stdout or "").lower()


def _is_agent_running() -> Optional[bool]:
    if os.name != "nt":
        return None
    running = _agent_running_via_tasklist()
    if running is not None:
        return running
    return _agent_running_via_powershell()


def _cleanup_stale_leader_socket(agent_running: Optional[bool]) -> None:
    if os.name != "nt" or agent_running is not False:
        return
    socket_path = Path(GROK_LEADER_SOCKET)
    if not socket_path.exists():
        return
    for path in (socket_path, socket_path.with_suffix(".lock")):
        try:
            if path.exists():
                path.unlink()
                log(f"removed stale Grok leader file: {path}")
        except OSError as exc:
            log(f"failed to remove stale Grok leader file {path}: {exc}")


def ensure_leader_running(command: str = "") -> None:
    global _LEADER_ENSURED
    if _LEADER_ENSURED or os.name != "nt":
        return
    _LEADER_ENSURED = True

    command = command or GROK_COMMAND
    agent_path = _agent_command_for_grok(command)
    if not agent_path.exists():
        log(f"Grok agent.exe not found; skip hidden leader prelaunch: {agent_path}")
        return

    agent_running = _is_agent_running()
    _cleanup_stale_leader_socket(agent_running)
    socket_path = Path(GROK_LEADER_SOCKET)
    if socket_path.exists() and agent_running is not False:
        return

    args = [
        str(agent_path),
        "agent",
        "leader",
        "--leader-socket",
        GROK_LEADER_SOCKET,
        "--no-exit-on-disconnect",
        "--no-auto-update",
    ]
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_win_flag("CREATE_NO_WINDOW") | DETACHED_PROCESS,
        )
    except Exception as exc:
        log(f"failed to prelaunch Grok leader: {exc}")
        return
    time.sleep(0.5)
    if proc.poll() is not None:
        log(f"Grok leader prelaunch exited early code={proc.returncode}")


def default_state() -> Dict[str, Any]:
    return {"topics": {}, "seen_posts": {}, "seen_text": {}}


def load_topics(path: Path) -> List[Dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    topics: List[Dict[str, Any]] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        if any(field not in entry for field in TOPIC_REQUIRED_FIELDS):
            log(f"skip invalid topic entry: {entry.get('key', '?')}")
            continue
        topics.append(entry)
    return topics


def load_state(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default_state()
    if not isinstance(data, dict):
        return default_state()
    base = default_state()
    for key in base:
        if isinstance(data.get(key), dict):
            base[key] = data[key]
    return base


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def topic_due(topic: Dict[str, Any], state: Dict[str, Any], now_ms: int) -> bool:
    scheduled_times = topic_due_by_schedule_times(topic, state, now_ms)
    if scheduled_times is not None:
        return scheduled_times
    scheduled = topic_due_by_schedule_hour(topic, state, now_ms)
    if scheduled is not None:
        return scheduled
    last = int((state.get("topics", {}).get(topic["key"]) or {}).get("last_run_ms") or 0)
    return now_ms >= topic_next_due_ms(topic, state)


def _parse_schedule_time(value: Any) -> Optional[Tuple[int, int]]:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _slot_ms_for_today(now_ms: int, hour: int, minute: int) -> int:
    current = dt.datetime.fromtimestamp(now_ms / 1000)
    slot = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return int(slot.timestamp() * 1000)


def topic_due_by_schedule_times(topic: Dict[str, Any], state: Dict[str, Any], now_ms: int) -> Optional[bool]:
    raw_times = topic.get("schedule_times")
    if raw_times is None:
        return None
    if not isinstance(raw_times, list):
        return None
    last = int((state.get("topics", {}).get(topic["key"]) or {}).get("last_run_ms") or 0)
    grace_ms = max(1, SCHEDULE_GRACE_MINUTES) * 60 * 1000
    for raw in raw_times:
        parsed = _parse_schedule_time(raw)
        if not parsed:
            continue
        slot_ms = _slot_ms_for_today(now_ms, parsed[0], parsed[1])
        if slot_ms <= now_ms < slot_ms + grace_ms:
            if last >= slot_ms:
                return False
            return True
    return False


def topic_due_by_schedule_hour(topic: Dict[str, Any], state: Dict[str, Any], now_ms: int) -> Optional[bool]:
    if "schedule_hour" not in topic:
        return None
    try:
        schedule_hour = int(topic.get("schedule_hour"))
        interval_hours = int(topic["interval_hours"])
    except (TypeError, ValueError):
        return None
    if interval_hours <= 0:
        return None
    current = dt.datetime.fromtimestamp(now_ms / 1000)
    if current.hour % interval_hours != schedule_hour % interval_hours:
        return False
    slot_start = current.replace(minute=0, second=0, microsecond=0)
    slot_start_ms = int(slot_start.timestamp() * 1000)
    last = int((state.get("topics", {}).get(topic["key"]) or {}).get("last_run_ms") or 0)
    return last < slot_start_ms


def topic_next_due_ms(topic: Dict[str, Any], state: Dict[str, Any]) -> int:
    last = int((state.get("topics", {}).get(topic["key"]) or {}).get("last_run_ms") or 0)
    return last + int(topic["interval_hours"]) * 3600 * 1000


def select_due_topics(
    topics: List[Dict[str, Any]],
    state: Dict[str, Any],
    now_ms: int,
    *,
    force: bool = False,
    max_topics: int = MAX_TOPICS_PER_RUN,
) -> List[Dict[str, Any]]:
    due = [t for t in topics if force or topic_due(t, state, now_ms)]
    if force or max_topics <= 0 or len(due) <= max_topics:
        return due
    by_key = {topic["key"]: index for index, topic in enumerate(topics)}
    due.sort(key=lambda topic: (topic_next_due_ms(topic, state), by_key.get(topic["key"], 0)))
    return due[:max_topics]


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$", re.MULTILINE)
_STATUS_RE = re.compile(
    r"https?://(?:www\.)?(?:x|twitter)\.com/((?:i/web|i|[A-Za-z0-9_]{1,30}))/status(?:es)?/(\d{1,25})"
)
_REDDIT_HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com"}


def extract_items(text: str) -> List[Dict[str, Any]]:
    cleaned = _FENCE_RE.sub("", (text or "").strip()).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(cleaned[start : end + 1])
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def canonical_status(url: str) -> Optional[Tuple[str, str]]:
    match = _STATUS_RE.search(str(url or ""))
    if not match:
        return None
    handle = match.group(1)
    if handle.startswith("i"):
        handle = "i"
    return match.group(2), handle


def canonical_reddit(url: str) -> Optional[Tuple[str, str, str]]:
    parsed = urlparse(str(url or "").strip())
    host = parsed.netloc.lower()
    if host not in _REDDIT_HOSTS:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[0].lower() != "r" or parts[2].lower() != "comments":
        return None
    subreddit = parts[1]
    post_id = parts[3]
    comment_id = parts[5] if len(parts) >= 6 else (parse_qs(parsed.query).get("comment") or [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9_]+", post_id):
        return None
    if comment_id and not re.fullmatch(r"[A-Za-z0-9_]+", comment_id):
        comment_id = ""
    source_id = f"{post_id}:{comment_id}" if comment_id else post_id
    return source_id, subreddit, comment_id


def text_hash(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    compact = re.sub(r"\s+", "", normalized)
    return hashlib.sha1(compact.encode("utf-8")).hexdigest()[:16]


def _clean_x_image_url(value: Any) -> str:
    text = html.unescape(str(value or "")).strip()
    if not text.startswith(("http://", "https://")):
        return ""
    path = urlparse(text).path.lower()
    if re.search(r"\.(?:mp4|m3u8)(?:$|\?)", text.lower()) or path.endswith((".mp4", ".m3u8")):
        return ""
    if re.search(r"\.(?:png|jpe?g|webp|gif)$", path) or "pbs.twimg.com" in text.lower():
        return text
    return ""


def x_media_image_urls(tweet: Dict[str, Any]) -> List[str]:
    media = tweet.get("media") or {}
    urls: List[str] = []

    for item in media.get("photos") or []:
        if isinstance(item, dict):
            urls.append(_clean_x_image_url(item.get("url")))

    for item in media.get("videos") or []:
        if isinstance(item, dict):
            urls.append(_clean_x_image_url(item.get("thumbnail_url")))

    for item in media.get("all") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").lower()
        value = item.get("thumbnail_url") if kind in {"video", "gif", "animated_gif"} else item.get("url")
        urls.append(_clean_x_image_url(value))

    return list(dict.fromkeys(url for url in urls if url))[:8]


def fxtwitter_lookup(status_id: str, handle: str = "i") -> Optional[Dict[str, Any]]:
    url = f"https://api.fxtwitter.com/{handle or 'i'}/status/{status_id}"
    data = None
    for attempt in range(2):
        try:
            resp = requests.get(
                url,
                timeout=FXTWITTER_TIMEOUT_S,
                headers={"User-Agent": "rss-ingest-grok-watch/1.0"},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            break
        except Exception as exc:
            log(f"fxtwitter lookup attempt {attempt + 1} failed id={status_id}: {exc}")
            if attempt == 0:
                time.sleep(1.5)
    if data is None:
        return None
    tweet = data.get("tweet") if isinstance(data, dict) and data.get("code") == 200 else None
    if not isinstance(tweet, dict):
        return None
    author = tweet.get("author") or {}
    return {
        "platform": "x",
        "source_id": status_id,
        "author": str(author.get("screen_name") or ""),
        "followers": int(author.get("followers") or 0),
        "created_ms": int(tweet.get("created_timestamp") or 0) * 1000,
        "likes": int(tweet.get("likes") or 0),
        "views": int(tweet.get("views") or 0),
        "text": str(tweet.get("text") or ""),
        "image_urls": x_media_image_urls(tweet),
    }


def _reddit_json_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    path = parsed.path.rstrip("/")
    if not path.endswith(".json"):
        path = path + ".json"
    return parsed._replace(path=path, query="raw_json=1", fragment="").geturl()


def _find_reddit_comment(children: Any, comment_id: str) -> Optional[Dict[str, Any]]:
    if not comment_id or not isinstance(children, list):
        return None
    for child in children:
        if not isinstance(child, dict) or child.get("kind") != "t1":
            continue
        data = child.get("data") or {}
        if data.get("id") == comment_id:
            return data
        replies = data.get("replies")
        if isinstance(replies, dict):
            found = _find_reddit_comment(((replies.get("data") or {}).get("children") or []), comment_id)
            if found:
                return found
    return None


_REDDIT_IMAGE_HOSTS = {"i.redd.it", "preview.redd.it", "external-preview.redd.it", "i.reddituploads.com"}


def _clean_reddit_image_url(value: Any) -> str:
    text = html.unescape(str(value or "")).strip()
    if not text.startswith(("http://", "https://")):
        return ""
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if host in _REDDIT_IMAGE_HOSTS:
        return text
    if re.search(r"\.(?:png|jpe?g|webp|gif)$", path):
        return text
    return ""


def reddit_image_urls(post: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    direct = _clean_reddit_image_url(post.get("url_overridden_by_dest") or post.get("url"))
    if direct:
        urls.append(direct)

    preview = post.get("preview") or {}
    for image in preview.get("images") or []:
        if not isinstance(image, dict):
            continue
        source = image.get("source") or {}
        cleaned = _clean_reddit_image_url(source.get("url"))
        if cleaned:
            urls.append(cleaned)
        for variant in (image.get("variants") or {}).values():
            if not isinstance(variant, dict):
                continue
            cleaned = _clean_reddit_image_url((variant.get("source") or {}).get("url"))
            if cleaned:
                urls.append(cleaned)

    media_metadata = post.get("media_metadata") or {}
    gallery_items = ((post.get("gallery_data") or {}).get("items") or [])
    gallery_ids = [
        str(item.get("media_id") or "")
        for item in gallery_items
        if isinstance(item, dict) and item.get("media_id")
    ]
    for media_id in gallery_ids or list(media_metadata):
        media = media_metadata.get(media_id) or {}
        if not isinstance(media, dict) or media.get("status") not in {None, "valid"}:
            continue
        for candidate in (media.get("s") or {}, *((media.get("p") or [])[-1:])):
            if not isinstance(candidate, dict):
                continue
            cleaned = _clean_reddit_image_url(candidate.get("u") or candidate.get("gif") or candidate.get("mp4"))
            if cleaned:
                urls.append(cleaned)

    thumb = _clean_reddit_image_url(post.get("thumbnail"))
    if thumb:
        urls.append(thumb)
    return list(dict.fromkeys(urls))[:8]


def reddit_lookup(url: str) -> Optional[Dict[str, Any]]:
    parsed = canonical_reddit(url)
    if not parsed:
        return None
    source_id, subreddit, comment_id = parsed
    try:
        resp = requests.get(
            _reddit_json_url(url),
            timeout=REDDIT_TIMEOUT_S,
            headers={"User-Agent": "rss-ingest-grok-watch/1.0"},
        )
    except Exception as exc:
        log(f"reddit lookup failed url={url}: {exc}")
        return None
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    if not isinstance(payload, list) or not payload:
        return None
    post_listing = payload[0] if isinstance(payload[0], dict) else {}
    post_children = ((post_listing.get("data") or {}).get("children") or [])
    if not post_children:
        return None
    post = (post_children[0].get("data") or {}) if isinstance(post_children[0], dict) else {}
    target = post
    if comment_id and len(payload) > 1 and isinstance(payload[1], dict):
        comment = _find_reddit_comment(((payload[1].get("data") or {}).get("children") or []), comment_id)
        if comment:
            target = comment
    title = str(post.get("title") or "").strip()
    body = str(target.get("body") or target.get("selftext") or "").strip()
    text = "\n\n".join(part for part in (title, body) if part)
    if not text:
        return None
    return {
        "platform": "reddit",
        "source_id": f"reddit:{source_id}",
        "author": str(target.get("author") or post.get("author") or ""),
        "subreddit": str(target.get("subreddit") or post.get("subreddit") or subreddit),
        "followers": 0,
        "created_ms": int(float(target.get("created_utc") or post.get("created_utc") or 0) * 1000),
        "likes": int(target.get("score") or 0),
        "views": 0,
        "text": text,
        "link": "https://www.reddit.com" + str(target.get("permalink") or post.get("permalink") or urlparse(url).path),
        "image_urls": reddit_image_urls(post),
    }


LOW_CRED_FOLLOWERS = 200
LOW_CRED_VIEWS = 50


def hard_filter(item: Dict[str, Any], tweet: Dict[str, Any], now_ms: int, window_hours: int) -> Optional[str]:
    created_ms = int(tweet.get("created_ms") or 0)
    if created_ms <= 0:
        return "no_timestamp"
    if now_ms - created_ms > int(window_hours) * 3600 * 1000:
        return "stale"
    if (
        str(tweet.get("platform") or "x") == "x"
        and
        str(item.get("category") or "") == "deal"
        and int(tweet.get("followers") or 0) < LOW_CRED_FOLLOWERS
        and int(tweet.get("views") or 0) < LOW_CRED_VIEWS
        and not str(item.get("official_url") or "").strip()
    ):
        return "low_cred_deal"
    return None


def prune_seen(state: Dict[str, Any], now_ms: int, max_age_days: int = SEEN_MAX_AGE_DAYS) -> None:
    cutoff = now_ms - max_age_days * 24 * 3600 * 1000
    for pool_key in ("seen_posts", "seen_text"):
        pool = state.get(pool_key) or {}
        state[pool_key] = {k: v for k, v in pool.items() if int(v or 0) >= cutoff}


EXTRA_DESC_KEYS = (
    "post_type", "official_url", "claim", "source_chain", "corroboration",
    "traction", "cold_start", "gap_signal", "why_hot", "content_angle",
    "heat_stage", "effective_date", "resource_type", "target_user",
    "repro_steps", "workflow_type", "pain_point", "fix_or_workaround",
)


def build_item_description(item: Dict[str, Any], tweet: Dict[str, Any]) -> str:
    parts: List[str] = []
    if tweet.get("text"):
        parts.append(str(tweet["text"]).strip())
    summary = str(item.get("summary") or "").strip()
    if summary:
        parts.append(f"[Grok摘要] {summary}")
    evidence = str(item.get("evidence") or "").strip()
    if evidence:
        parts.append(f"[证据] {evidence}")
    flags = [str(f).strip() for f in (item.get("red_flags") or []) if str(f).strip()]
    if flags:
        parts.append("[红旗] " + "、".join(flags))
    score = item.get("signal_score")
    if isinstance(score, (int, float)) and score:
        parts.append(f"[评分] {int(score)}/5")
    extras = [
        f"{key}: {str(item[key]).strip()}"
        for key in EXTRA_DESC_KEYS
        if str(item.get(key) or "").strip()
    ]
    if extras:
        parts.append("\n".join(extras))
    if str(tweet.get("platform") or "x") == "reddit":
        parts.append(
            f"[来源] r/{tweet.get('subreddit', '')} · u/{tweet.get('author', '')} · {int(tweet.get('likes') or 0)}分{_posted_suffix(tweet)}"
        )
    else:
        parts.append(
            f"[来源] @{tweet.get('author', '')} · {int(tweet.get('likes') or 0)}赞·{int(tweet.get('views') or 0)}阅{_posted_suffix(tweet)}"
        )
    return "\n\n".join(parts)


def _posted_suffix(tweet: Dict[str, Any]) -> str:
    cms = int(tweet.get("created_ms") or 0)
    if not cms:
        return ""
    return " · 发布 " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(cms / 1000))


def _image_mime_from_url(url: str) -> str:
    mime = mimetypes.guess_type(urlparse(str(url or "")).path)[0]
    return mime if mime and mime.startswith("image/") else "image/jpeg"


def build_feed_xml(topic: Dict[str, Any], items: List[Dict[str, Any]], feed_now_ms: int) -> str:
    # pubDate 用入库时刻（feed_ts_ms），不是推文真实发布时间。grok 搜的是历史推文，
    # 若 pubDate 用真实时间会早于 rss_ingest 的增量 cutoff（last_fetch）而被时间窗砍掉。
    # 真实发布时间保留在 description 的 [来源] 行；去重靠 item_key + seen-store。
    prefix = str(topic.get("title_prefix") or "")
    topic_platform = str(topic.get("platform") or "x").lower()
    channel_link = "https://www.reddit.com" if topic_platform == "reddit" else ("https://grok.com" if topic_platform == "mixed" else "https://x.com")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        f"<title>{escape('Grok搜索 - ' + str(topic.get('name') or topic['key']))}</title>",
        f"<link>{escape(channel_link)}</link>",
        f"<description>{escape('grok_watch 生成的本地 feed: ' + str(topic['key']))}</description>",
    ]
    for item in items:
        title = prefix + str(item.get("title") or "(无标题)")
        pub = formatdate(int(item.get("feed_ts_ms") or feed_now_ms) / 1000, usegmt=True)
        lines.extend(
            [
                "<item>",
                f"<title>{escape(title)}</title>",
                f"<link>{escape(item['link'])}</link>",
                f'<guid isPermaLink="true">{escape(item["link"])}</guid>',
                f"<pubDate>{pub}</pubDate>",
                f"<description>{escape(str(item.get('description') or ''))}</description>",
            ]
        )
        for image_url in item.get("image_urls") or []:
            if not str(image_url or "").strip():
                continue
            lines.append(
                f'<enclosure url="{escape(str(image_url))}" type="{escape(_image_mime_from_url(str(image_url)))}" length="0" />'
            )
        lines.append("</item>")
    lines.extend(["</channel>", "</rss>"])
    return "\n".join(lines)


def write_feed(topic: Dict[str, Any], items: List[Dict[str, Any]], feed_dir: Path, feed_now_ms: int) -> Path:
    feed_dir = Path(feed_dir)
    feed_dir.mkdir(parents=True, exist_ok=True)
    path = feed_dir / f"{topic['key']}.xml"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(build_feed_xml(topic, items, feed_now_ms), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _grok_watch_transport() -> str:
    return (os.environ.get("GROK_WATCH_TRANSPORT", "web").strip().lower() or "web")


def _grok_model() -> str:
    return (
        os.environ.get("GROK_WATCH_MODEL", "").strip()
        or os.environ.get("GROK_MODEL", "").strip()
        or "grok-4-1-fast"
    )


def _grok_api_key() -> str:
    return os.environ.get("XAI_API_KEY", "").strip() or os.environ.get("GROK_API_KEY", "").strip()


def build_grok_api_request(prompt_text: str) -> Dict[str, Any]:
    return {
        "model": _grok_model(),
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt_text}],
            }
        ],
        "tools": [{"type": "x_search", "x_search": {}}],
        "store": False,
        "temperature": 0,
    }


def extract_grok_api_output_text(payload: Dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    for output in payload.get("output") or []:
        if not isinstance(output, dict):
            continue
        for content in output.get("content") or []:
            if (
                isinstance(content, dict)
                and content.get("type") == "output_text"
                and isinstance(content.get("text"), str)
                and content["text"].strip()
            ):
                return content["text"]
    raise RuntimeError("Grok API response did not include output text.")


def run_grok_api(prompt_text: str, timeout_s: int = 0) -> str:
    api_key = _grok_api_key()
    if not api_key:
        raise RuntimeError("Grok API key missing; set XAI_API_KEY or GROK_API_KEY for grok_watch.")
    resp = requests.post(
        GROK_API_URL,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        json=build_grok_api_request(prompt_text),
        timeout=timeout_s or GROK_API_TIMEOUT_S,
    )
    if resp.status_code < 200 or resp.status_code >= 300:
        raise RuntimeError(f"Grok API HTTP {resp.status_code}: {(resp.text or '')[-400:]}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"Grok API returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Grok API returned a non-object response.")
    return extract_grok_api_output_text(payload)


def _grok_browser_cli_path() -> Path:
    cli = Path(GROK_BROWSER_CLI)
    if not cli.is_absolute():
        cli = PROJECT_ROOT / cli
    return cli


def _legacy_grok_web_runner_path() -> Path:
    runner = Path(GROK_LEGACY_WEB_RUNNER)
    if not runner.is_absolute():
        runner = PROJECT_ROOT / runner
    return runner


def _grok_web_command_exists(command: str) -> bool:
    cmd_path = Path(command)
    if cmd_path.exists():
        return True
    return shutil.which(command) is not None


def _grok_web_endpoint_status(endpoint_file: Path) -> Tuple[bool, str]:
    endpoint_file = Path(endpoint_file)
    if not endpoint_file.exists():
        return False, f"endpoint file missing: {endpoint_file}"
    try:
        endpoint = endpoint_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return False, f"endpoint file unreadable: {exc}"
    parsed = urlparse(endpoint)
    try:
        endpoint_host = parsed.hostname
        endpoint_port = parsed.port
    except ValueError as exc:
        return False, f"endpoint file does not contain a valid WebSocket URL: {exc}"
    if parsed.scheme not in {"ws", "wss"} or not endpoint_host or not endpoint_port:
        return False, "endpoint file does not contain a valid WebSocket URL"
    if endpoint_host not in {"127.0.0.1", "localhost", "::1"}:
        return False, f"endpoint host is not local: {endpoint_host}"
    try:
        with socket.create_connection((endpoint_host, endpoint_port), timeout=2):
            return True, ""
    except OSError as exc:
        return False, f"{endpoint_host}:{endpoint_port} unreachable: {exc}"


def ensure_grok_web_endpoint() -> None:
    endpoint_file = Path(GROK_WEB_ENDPOINT_FILE)
    healthy, detail = _grok_web_endpoint_status(endpoint_file)
    if healthy:
        return

    launcher = Path(GROK_GPT_BROWSER_COMMAND).expanduser()
    launcher_path = str(launcher) if launcher.exists() else shutil.which(GROK_GPT_BROWSER_COMMAND)
    if not launcher_path:
        raise RuntimeError(
            f"Grok web endpoint unavailable ({detail}); "
            f"gpt-browser launcher not found: {GROK_GPT_BROWSER_COMMAND}"
        )
    log(f"gpt-browser endpoint unavailable ({detail}); launching Chrome")
    try:
        completed = subprocess.run(
            [launcher_path, "launch"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(10, GROK_GPT_BROWSER_LAUNCH_TIMEOUT_S),
            check=False,
            creationflags=_win_flag("CREATE_NO_WINDOW"),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"gpt-browser launch failed: {exc}") from exc
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout or "").strip()[-500:]
        raise RuntimeError(f"gpt-browser launch exited {completed.returncode}: {error}")

    healthy, detail = _grok_web_endpoint_status(endpoint_file)
    if not healthy:
        raise RuntimeError(f"gpt-browser launch completed but endpoint is still unavailable: {detail}")
    log("gpt-browser endpoint recovered")


def validate_grok_web_config() -> None:
    endpoint_file = Path(GROK_WEB_ENDPOINT_FILE)
    if not endpoint_file.exists():
        raise RuntimeError(f"Grok web endpoint file missing: {endpoint_file}")
    cli = _grok_browser_cli_path()
    if not cli.exists():
        if GROK_ALLOW_LEGACY_WEB_RUNNER and _legacy_grok_web_runner_path().exists():
            return
        raise RuntimeError(f"grok-browser CLI missing: {cli}")
    if not _grok_web_command_exists(GROK_WEB_COMMAND):
        raise RuntimeError(f"Grok web command not found: {GROK_WEB_COMMAND}")


def run_grok_web(prompt_text: str, timeout_s: int = 0) -> str:
    validate_grok_web_config()
    timeout_s = timeout_s or GROK_WEB_TIMEOUT_S
    fd, prompt_path = tempfile.mkstemp(prefix="grok-web-watch-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(prompt_text.rstrip() + "\n")
        env = os.environ.copy()
        node_modules = GROK_WEB_NODE_MODULES
        if node_modules:
            existing = env.get("NODE_PATH", "")
            env["NODE_PATH"] = node_modules if not existing else f"{node_modules}{os.pathsep}{existing}"
        env["GROK_BROWSER_ENDPOINT_FILE"] = GROK_WEB_ENDPOINT_FILE
        env["GROK_BROWSER_MODEL"] = GROK_WEB_MODEL
        env["GROK_BROWSER_URL"] = GROK_WEB_URL
        env["GROK_BROWSER_TIMEOUT_S"] = str(max(1, int(timeout_s)))
        if GROK_WEB_KEEP_PAGE:
            env["GROK_BROWSER_KEEP_PAGE"] = GROK_WEB_KEEP_PAGE
        args = [
            GROK_WEB_COMMAND,
            str(_grok_browser_cli_path()),
            "send",
            "--file",
            prompt_path,
            "--model",
            GROK_WEB_MODEL,
            "--endpoint-file",
            GROK_WEB_ENDPOINT_FILE,
            "--url",
            GROK_WEB_URL,
            "--timeout",
            str(max(1, int(timeout_s))),
            "--json",
        ]
        if GROK_WEB_KEEP_PAGE:
            args.append("--keep-page")
        if not _grok_browser_cli_path().exists() and GROK_ALLOW_LEGACY_WEB_RUNNER:
            args = [GROK_WEB_COMMAND, str(_legacy_grok_web_runner_path())]
            env["GROK_WEB_ENDPOINT_FILE"] = GROK_WEB_ENDPOINT_FILE
            env["GROK_WEB_PROMPT_FILE"] = prompt_path
            env["GROK_WEB_URL"] = GROK_WEB_URL
            env["GROK_WEB_MODEL"] = GROK_WEB_MODEL
            env["GROK_WEB_TIMEOUT_MS"] = str(max(1, int(timeout_s)) * 1000)
            env["GROK_WEB_KEEP_PAGE"] = GROK_WEB_KEEP_PAGE
        completed = subprocess.run(
            args,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=max(10, int(timeout_s) + 30),
            check=False,
            creationflags=_win_flag("CREATE_NO_WINDOW"),
        )
    except subprocess.TimeoutExpired as exc:
        raise GrokTimeout(f"grok web timeout after {timeout_s}s") from exc
    finally:
        try:
            os.unlink(prompt_path)
        except OSError:
            pass
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "grok web runner failed").strip()
        raise RuntimeError(detail[-1000:])
    try:
        payload = json.loads((completed.stdout or "").strip())
    except ValueError as exc:
        raise RuntimeError(f"grok web runner returned invalid JSON: {(completed.stdout or '')[:500]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("grok web runner returned a non-object payload.")
    text = str(payload.get("text") or "").strip()
    if not text:
        raise RuntimeError("grok web runner returned empty text.")
    return text


def run_grok_cli(prompt_text: str, command: str = "", timeout_s: int = 0) -> str:
    command = command or GROK_COMMAND
    timeout_s = timeout_s or GROK_TIMEOUT_S
    fd, prompt_path = tempfile.mkstemp(prefix="grok-watch-", suffix=".md")
    proc: Optional[subprocess.Popen[str]] = None
    stdout = ""
    stderr = ""
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(prompt_text.rstrip() + "\n")
        GROK_CWD.mkdir(parents=True, exist_ok=True)
        args = [
            command,
            "--leader-socket", GROK_LEADER_SOCKET,
            "--cwd", str(GROK_CWD),
            "--verbatim",
            "--prompt-file", prompt_path,
            "--output-format", "json",
            "--tools", "",
            "--deny", "Bash",
            "--deny", "MCPTool",
            "--disallowed-tools", GROK_DISALLOWED_TOOLS,
            "--system-prompt-override", GROK_SYSTEM_PROMPT,
            "--rules", "Do not invoke skills, plugins, hooks, subagents, shell commands, browser automation, or external scripts. Use only Grok's native response capability and return the requested JSON.",
            "--max-turns", str(GROK_MAX_TURNS),
            "--no-subagents",
            "--no-memory",
            "--no-plan",
        ]
        proc = subprocess.Popen(
            args,
            cwd=str(GROK_CWD),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            close_fds=True,
            env=_grok_cli_env(),
            # 隐藏 grok.exe 的控制台窗口，并让超时路径可以按进程组/进程树清理。
            creationflags=_win_flag("CREATE_NO_WINDOW") | _win_flag("CREATE_NEW_PROCESS_GROUP"),
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            log(f"grok timeout after {timeout_s}s; killing process tree pid={proc.pid}")
            if os.name == "nt":
                _kill_tree_win(proc.pid)
            else:
                proc.kill()
            for pipe in (proc.stdout, proc.stderr):
                if pipe:
                    try:
                        pipe.close()
                    except OSError:
                        pass
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                log(f"grok process still alive after taskkill pid={proc.pid}")
            raise GrokTimeout(f"grok timeout after {timeout_s}s")
    finally:
        try:
            os.unlink(prompt_path)
        except OSError:
            pass
    if proc.returncode != 0:
        raise RuntimeError(f"grok exit {proc.returncode}: {(stderr or '')[-400:]}")
    stdout = (stdout or "").strip()
    if not stdout:
        return ""
    try:
        outer = json.loads(stdout)
        if isinstance(outer, dict) and isinstance(outer.get("text"), str):
            return outer["text"]
    except ValueError:
        pass
    return stdout


def run_grok(prompt_text: str, command: str = "", timeout_s: int = 0) -> str:
    transport = _grok_watch_transport()
    if transport == "api":
        return run_grok_api(prompt_text, timeout_s=timeout_s)
    if transport == "web":
        return run_grok_web(prompt_text, timeout_s=timeout_s)
    if transport == "cli":
        return run_grok_cli(prompt_text, command=command, timeout_s=timeout_s)
    if transport in {"off", "disabled", "none"}:
        raise RuntimeError("grok_watch transport is disabled.")
    raise RuntimeError(f"unsupported GROK_WATCH_TRANSPORT={transport!r}")


def validate_grok_transport() -> None:
    transport = _grok_watch_transport()
    if transport == "api" and not _grok_api_key():
        raise RuntimeError("Grok API key missing; set XAI_API_KEY or GROK_API_KEY before enabling grok_watch.")
    if transport == "web":
        ensure_grok_web_endpoint()
        validate_grok_web_config()
    if transport not in {"api", "cli", "web"}:
        raise RuntimeError(f"unsupported GROK_WATCH_TRANSPORT={transport!r}")


def _read_prompt(topic: Dict[str, Any]) -> str:
    path = Path(topic["prompt_file"])
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.read_text(encoding="utf-8")


def process_topic(
    topic: Dict[str, Any],
    state: Dict[str, Any],
    now_ms: int,
    run_grok_fn: Callable[[str], str],
    lookup_fn: Callable[[str, str], Optional[Dict[str, Any]]],
    feed_dir: Path,
    reddit_lookup_fn: Callable[[str], Optional[Dict[str, Any]]] = reddit_lookup,
) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    prompt = _read_prompt(topic)
    provider_succeeded = False
    provider_errors: List[str] = []
    for attempt in range(2):
        try:
            items = extract_items(run_grok_fn(prompt))
            provider_succeeded = True
        except GrokTimeout as exc:
            log(f"topic={topic['key']} grok timed out: {exc}")
            provider_errors.append(str(exc))
            items = []
            break
        except Exception as exc:
            log(f"topic={topic['key']} grok failed: {exc}")
            provider_errors.append(str(exc))
            items = []
        if items:
            break
        if attempt == 0:
            log(f"topic={topic['key']} no items parsed (may just be no new posts; real failures log 'grok failed' above); retry once")
    if not provider_succeeded:
        detail = provider_errors[-1] if provider_errors else "no successful response"
        raise RuntimeError(f"grok provider failed for topic={topic['key']}: {detail}")

    dropped: Dict[str, int] = {}

    def _drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    accepted: List[Dict[str, Any]] = []
    pending_seen_posts: Dict[str, int] = {}
    pending_seen_text: Dict[str, int] = {}
    for item in items:
        url = str(item.get("url") or "")
        reddit_parsed = canonical_reddit(url)
        x_parsed = None if reddit_parsed else canonical_status(url)
        if reddit_parsed:
            source = reddit_lookup_fn(url)
            source_key = reddit_parsed[0]
        elif x_parsed:
            status_id, handle = x_parsed
            source = lookup_fn(status_id, handle)
            source_key = status_id
        else:
            _drop("bad_url")
            continue
        seen_key = str(source.get("source_id") or source_key) if source else ("reddit:" + source_key if reddit_parsed else source_key)
        if seen_key in state["seen_posts"] or seen_key in pending_seen_posts:
            _drop("dup_post")
            continue
        if not source:
            _drop("unverified")
            continue
        reason = hard_filter(item, source, now_ms, int(topic["window_hours"]))
        if reason:
            _drop(reason)
            continue
        thash = text_hash(source["text"])
        if thash in state["seen_text"] or thash in pending_seen_text:
            _drop("dup_text")
            continue
        pending_seen_posts[seen_key] = now_ms
        pending_seen_text[thash] = now_ms
        if str(source.get("platform") or "x") == "reddit":
            link = str(source.get("link") or url)
        else:
            link = f"https://x.com/{source['author']}/status/{source_key}"
        accepted.append(
            {
                "source_id": seen_key,
                "platform": str(source.get("platform") or "x"),
                "link": link,
                "title": str(item.get("title") or "(无标题)"),
                "created_ms": int(source["created_ms"]),
                # feed_ts_ms = 入库时刻（每条递减 1 秒保序），用作 RSS pubDate，
                # 让历史推文不被 rss_ingest 的增量时间窗砍掉。created_ms 仅用于显示/保鲜。
                "feed_ts_ms": now_ms - len(accepted) * 1000,
                "author": source["author"],
                "description": build_item_description(item, source),
                "image_urls": list(source.get("image_urls") or []),
            }
        )

    topic_state = dict(state["topics"].get(topic["key"]) or {})
    cutoff = now_ms - FEED_ITEM_MAX_AGE_H * 3600 * 1000
    recent = [
        entry
        for entry in (topic_state.get("recent_items") or [])
        if int(entry.get("created_ms") or 0) >= cutoff
    ]
    recent.extend(accepted)
    recent.sort(key=lambda e: int(e.get("feed_ts_ms") or e.get("created_ms") or 0), reverse=True)
    next_recent_items = recent[:FEED_MAX_ITEMS]
    topic_state["recent_items"] = next_recent_items
    topic_state["last_run_ms"] = now_ms

    write_feed(topic, next_recent_items, feed_dir, now_ms)
    state["seen_posts"].update(pending_seen_posts)
    state["seen_text"].update(pending_seen_text)
    state["topics"][topic["key"]] = topic_state
    stats = {"topic": topic["key"], "returned": len(items), "accepted": len(accepted), "dropped": dropped}
    log(f"topic={topic['key']} returned={len(items)} accepted={len(accepted)} dropped={dropped}")
    return stats


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Grok X-search → local RSS feeds")
    parser.add_argument("--topics-path", default=str(DEFAULT_TOPICS_PATH))
    parser.add_argument("--state-path", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--feed-dir", default=str(DEFAULT_FEED_DIR))
    parser.add_argument("--topic", action="append", default=None, help="只跑指定话题 key，可重复")
    parser.add_argument("--force", action="store_true", help="忽略 interval 到期判断")
    parser.add_argument(
        "--max-topics-per-run",
        type=int,
        default=MAX_TOPICS_PER_RUN,
        help="每轮最多处理的到期话题数；0 表示不限制，--force 时忽略",
    )
    args = parser.parse_args(argv)

    topics = load_topics(Path(args.topics_path))
    if args.topic:
        wanted = set(args.topic)
        topics = [t for t in topics if t["key"] in wanted]
    state_path = Path(args.state_path)
    state = load_state(state_path)
    now_ms = int(time.time() * 1000)

    due = select_due_topics(
        topics,
        state,
        now_ms,
        force=args.force,
        max_topics=args.max_topics_per_run,
    )
    log(f"topics total={len(topics)} due={[t['key'] for t in due]}")
    if due:
        try:
            validate_grok_transport()
        except RuntimeError as exc:
            log(str(exc))
            return 2
    failed_topics = 0
    for topic in due:
        try:
            process_topic(topic, state, now_ms, run_grok, fxtwitter_lookup, Path(args.feed_dir))
        except Exception as exc:
            log(f"topic={topic['key']} failed: {exc}")
            failed_topics += 1
            continue
        save_state(state_path, state)
    prune_seen(state, now_ms)
    save_state(state_path, state)
    return 1 if failed_topics else 0


if __name__ == "__main__":
    from rss_ingest import SingleInstanceLock  # 复用主流程的锁（带 stale 检测）

    _lock = SingleInstanceLock(DEFAULT_LOCK_PATH)
    if not _lock.acquire():
        log(f"another grok_watch is running; skip. lock={_lock.path}")
        raise SystemExit(0)
    try:
        raise SystemExit(main())
    finally:
        _lock.release()
