# -*- coding: utf-8 -*-
import os
from pathlib import Path


def load_env_file(path: Path) -> None:
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                name = name.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(name, value)
    except FileNotFoundError:
        pass


def load_project_env(base_dir: Path) -> None:
    """Load only this repository's explicit local environment file."""
    load_env_file(Path(base_dir) / "rss-ingest-local.env")

BASE_DIR = Path(__file__).resolve().parent

# Local env file support (optional)
if os.getenv("RSS_INGEST_SKIP_LOCAL_ENV", "").lower() not in {"1", "true", "yes", "y"}:
    load_project_env(BASE_DIR)

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

# 飞书 Bitable App（同一应用可包含多个表）
FEISHU_APP_TOKEN = os.getenv("FEISHU_APP_TOKEN", "")
FEISHU_NEWS_TABLE_ID = os.getenv("FEISHU_NEWS_TABLE_ID", "")
FEISHU_RSS_TABLE_ID = os.getenv("FEISHU_RSS_TABLE_ID", "")
FEISHU_FILTERED_TABLE_ID = os.getenv("FEISHU_FILTERED_TABLE_ID", "")
FEISHU_KEYWORD_TABLE_ID = os.getenv("FEISHU_KEYWORD_TABLE_ID", "")
FEISHU_NOTIFY_TABLE_ID = os.getenv("FEISHU_NOTIFY_TABLE_ID", "")
FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")
FEISHU_SYNC_TABLE_ID = os.getenv("FEISHU_SYNC_TABLE_ID", "")
FEISHU_SYNC_FIELD_SUMMARY = os.getenv("FEISHU_SYNC_FIELD_SUMMARY", "总结")
ENABLE_SECONDARY_SYNC = os.getenv("ENABLE_SECONDARY_SYNC", "false").lower() in {"1", "true", "yes", "y"}
FEISHU_SYNC_APP_TOKEN = os.getenv("FEISHU_SYNC_APP_TOKEN", "")
LOCAL_KEYWORD_BLOCKLIST_PATH = os.getenv("LOCAL_KEYWORD_BLOCKLIST_PATH", str(BASE_DIR / "docs" / "local-keyword-blocklist.txt"))
LOCAL_KEYWORD_NAME_BLOCKLIST_PATH = os.getenv("LOCAL_KEYWORD_NAME_BLOCKLIST_PATH", str(BASE_DIR / "docs" / "local-keyword-name-blocklist.txt"))
LOCAL_TRIAGE_PROMPT_PATH = os.getenv("LOCAL_TRIAGE_PROMPT_PATH", str(BASE_DIR / "docs" / "local-screen-triage-prompt.md"))
LOCAL_SCREEN_PROMPT_PATH = os.getenv("LOCAL_SCREEN_PROMPT_PATH", str(BASE_DIR / "docs" / "local-screen-prompt.md"))
LOCAL_SUMMARIZE_PROMPT_PATH = os.getenv("LOCAL_SUMMARIZE_PROMPT_PATH", str(BASE_DIR / "docs" / "local-summarize-prompt.md"))
LOCAL_PROMPT_RULES_PATH = os.getenv("LOCAL_PROMPT_RULES_PATH", str(BASE_DIR / "docs" / "local-prompt-rules.md"))
LOCAL_SCREEN_KEYWORDS_ADDENDUM_PATH = os.getenv(
    "LOCAL_SCREEN_KEYWORDS_ADDENDUM_PATH",
    "",
).strip()
LOCAL_DEDUP_ALIAS_GROUPS_PATH = os.getenv(
    "LOCAL_DEDUP_ALIAS_GROUPS_PATH",
    str(BASE_DIR / "docs" / "local-merge-alias-groups.json"),
)
ENABLE_KEYWORD_SNAPSHOT_INDEX = os.getenv("ENABLE_KEYWORD_SNAPSHOT_INDEX", "true").lower() in {"1", "true", "yes", "y"}
KEYWORD_SNAPSHOT_PATH = os.getenv("KEYWORD_SNAPSHOT_PATH", str(BASE_DIR / "data" / "keyword_snapshot.json"))
KEYWORD_RUNTIME_SNAPSHOT_PATH = os.getenv(
    "KEYWORD_RUNTIME_SNAPSHOT_PATH",
    str(BASE_DIR / ".cache" / "keyword_snapshot_runtime.json"),
)
KEYWORD_SNAPSHOT_GIT_REF = os.getenv("KEYWORD_SNAPSHOT_GIT_REF", "origin/main")
KEYWORD_SNAPSHOT_GIT_PATH = os.getenv("KEYWORD_SNAPSHOT_GIT_PATH", "data/keyword_snapshot.json")
KEYWORD_SNAPSHOT_GIT_FETCH = os.getenv("KEYWORD_SNAPSHOT_GIT_FETCH", "true").lower() in {"1", "true", "yes", "y"}
KEYWORD_SNAPSHOT_GIT_TIMEOUT = int(os.getenv("KEYWORD_SNAPSHOT_GIT_TIMEOUT", "15"))
KEYWORD_SNAPSHOT_GIT_FETCH_INTERVAL_MIN = int(os.getenv("KEYWORD_SNAPSHOT_GIT_FETCH_INTERVAL_MIN", "60"))
KEYWORD_SNAPSHOT_GIT_FETCH_STAMP_PATH = os.getenv(
    "KEYWORD_SNAPSHOT_GIT_FETCH_STAMP_PATH",
    str(BASE_DIR / ".cache" / "keyword_snapshot_git_fetch.stamp"),
)
KEYWORD_SNAPSHOT_URL = os.getenv(
    "KEYWORD_SNAPSHOT_URL",
    "",
)
KEYWORD_SNAPSHOT_TIMEOUT = int(os.getenv("KEYWORD_SNAPSHOT_TIMEOUT", "8"))
KEYWORD_SNAPSHOT_MIN_ENTRIES = int(os.getenv("KEYWORD_SNAPSHOT_MIN_ENTRIES", "1000"))
KEYWORD_SNAPSHOT_MAX_AGE_HOURS = float(os.getenv("KEYWORD_SNAPSHOT_MAX_AGE_HOURS", "6"))

# Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL_NAME", "gemini-3-flash-preview")
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL_NAME}:generateContent"
GEMINI_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "65536"))
GEMINI_THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "minimal").strip()
GEMINI_BACKEND = os.getenv("GEMINI_BACKEND", "developer").strip().lower()
GOOGLE_CLOUD_PROJECT = (
    os.getenv("GOOGLE_CLOUD_PROJECT")
    or os.getenv("GCP_PROJECT_ID")
    or os.getenv("GCLOUD_PROJECT")
    or ""
).strip()
GOOGLE_CLOUD_LOCATION = (
    os.getenv("GOOGLE_CLOUD_LOCATION")
    or os.getenv("GOOGLE_VERTEX_LOCATION")
    or "global"
).strip()
GOOGLE_VERTEX_MODEL = (
    os.getenv("GOOGLE_VERTEX_MODEL")
    or os.getenv("VERTEX_MODEL")
    or GEMINI_MODEL_NAME
).strip()

# 新闻表字段
NEWS_FIELD_TITLE = "标题"
NEWS_FIELD_SCORE = "AI打分"
NEWS_FIELD_CATEGORIES = "分类"
NEWS_FIELD_SUMMARY = "QA总结"
NEWS_FIELD_PUBLISHED_MS = "发布时间"
NEWS_FIELD_SOURCE = "来源"
NEWS_FIELD_FULL_CONTENT = "全文"
NEWS_FIELD_ITEM_KEY = "item_key"
NEWS_FIELD_CREATED_TIME = "创建时间"
NEWS_FIELD_READ = "已读"
NEWS_FIELD_BRIEF_SUMMARY = "摘要"
NEWS_FIELD_KEYWORDS = "关键词"
NEWS_FIELD_KEYWORD_RECORDS = "关键词记录"
NEWS_FIELD_IMAGES = os.getenv("NEWS_FIELD_IMAGES", "图片")
NEWS_CATEGORY_OPTIONS = (
    "AI前沿资讯",
    "AI工具与自动化",
    "AI实战教程",
    "商业与变现",
    "创作者经济",
    "产品与增长",
    "科技与产业趋势",
    "宏观与国际局势",
    "深度思考与认知",
)
FILTERED_FIELD_TITLE = "标题"
FILTERED_FIELD_FILTER_METHOD = "过滤方式"
FILTERED_FIELD_FILTER_REASON = "过滤原因"
FILTERED_FIELD_PUBLISHED_MS = "发布时间"
FILTERED_FIELD_SOURCE = "来源"
FILTERED_FIELD_FULL_CONTENT = "全文"
FILTERED_FIELD_ITEM_KEY = "item_key"
FILTERED_FIELD_CREATED_TIME = "创建时间"
FILTERED_FIELD_KEYWORDS = "关键词"
FILTERED_FIELD_SUMMARY = "摘要"
FILTERED_FIELD_KEYWORD_RECORDS = "关键词记录"
FILTERED_FIELD_IMAGES = os.getenv("FILTERED_FIELD_IMAGES", "图片")
KEYWORD_FIELD_CANONICAL_NAME = "规范名"
KEYWORD_FIELD_TYPE = "类型"
KEYWORD_FIELD_ALIASES = "归一项"
KEYWORD_FIELD_FIRST_SEEN = "首次出现"
KEYWORD_FIELD_NOTE = "备注"
KEYWORD_FIELD_NEWS_COUNT = "NEWS次数"
KEYWORD_FIELD_FILTERED_COUNT = "FILTERED次数"
KEYWORD_FIELD_NEWS_24H = "NEWS24h"
KEYWORD_FIELD_LAST_SEEN = "最后出现"
KEYWORD_FIELD_HEAT_SAMPLE = "热度样本"
KEYWORD_FIELD_PARENT = "父关键词"
KEYWORD_FIELD_OWNERS = "归属关键词"

# RSS 源表字段
RSS_FIELD_NAME = "name"
RSS_FIELD_FEED_URL = "feed_url"
RSS_FIELD_TYPE = "type"
RSS_FIELD_DESCRIPTION = "description"
RSS_FIELD_ENABLED = "enabled"
RSS_FIELD_STATUS = "status"
RSS_FIELD_LAST_FETCH_TIME = "last_fetch_time"
RSS_FIELD_LAST_FETCH_STATUS = "last_fetch_status"
RSS_FIELD_CONSECUTIVE_FAIL_COUNT = "consecutive_fail_count"
RSS_FIELD_LAST_ITEM_GUID = "last_item_guid"
RSS_FIELD_LAST_ITEM_PUB_TIME = "last_item_pub_time"
RSS_FIELD_ITEM_ID_STRATEGY = "item_id_strategy"
RSS_FIELD_CONTENT_LANGUAGE = "content_language"
RSS_FIELD_FAILED_ITEMS = "failed_items"
RSS_FIELD_WATCH_STATE = "watch_state"

DEFAULT_ITEM_ID_STRATEGY = "guid"
DEFAULT_CONTENT_HASH_ALGO = "md5"
DEFAULT_FETCH_INTERVAL_MIN = int(os.getenv("DEFAULT_FETCH_INTERVAL_MIN", "180"))
RSS_SOURCE_MODE = os.getenv("RSS_SOURCE_MODE", "all").strip().lower() or "all"
RSS_SOURCE_OVERRIDE_FILE = os.getenv("RSS_SOURCE_OVERRIDE_FILE", "").strip()
HTML_WATCH_FETCH_INTERVAL_MIN = int(os.getenv("HTML_WATCH_FETCH_INTERVAL_MIN", "10"))
RSS_FETCH_CONCURRENCY = int(os.getenv("RSS_FETCH_CONCURRENCY", "20"))
RSS_FETCH_PER_HOST_CONCURRENCY = int(os.getenv("RSS_FETCH_PER_HOST_CONCURRENCY", "4"))
RSS_FETCH_RETRY_CONCURRENCY = int(os.getenv("RSS_FETCH_RETRY_CONCURRENCY", "4"))
RSS_FETCH_LOOKBACK_MINUTES = int(os.getenv("RSS_FETCH_LOOKBACK_MINUTES", "180"))
RSS_SOURCE_FAILURE_EXIT_COUNT = int(os.getenv("RSS_SOURCE_FAILURE_EXIT_COUNT", "10"))
RSS_SOURCE_FAILURE_EXIT_RATIO = float(os.getenv("RSS_SOURCE_FAILURE_EXIT_RATIO", "0.25"))
MAX_ENTRIES_PER_FEED = 200
NEWS_ITEM_KEY_PREFETCH_LIMIT = 500
NEWS_ITEM_KEY_PREFETCH_MAX_PAGES = int(os.getenv("NEWS_ITEM_KEY_PREFETCH_MAX_PAGES", "50"))
ITEM_KEY_PREFETCH_DEFAULT_DAYS = int(os.getenv("ITEM_KEY_PREFETCH_DEFAULT_DAYS", "30"))
RSS_INGEST_ITEM_KEY_PREFETCH_ATTEMPTS = int(os.getenv("RSS_INGEST_ITEM_KEY_PREFETCH_ATTEMPTS", "2"))
ENABLE_BROWSER_ARTICLE_FETCH = os.getenv("ENABLE_BROWSER_ARTICLE_FETCH", "false").lower() in {"1", "true", "yes", "y"}
ENABLE_X_BROWSER_FALLBACK = os.getenv("ENABLE_X_BROWSER_FALLBACK", "false").lower() in {"1", "true", "yes", "y"}
BROWSER_ARTICLE_FETCH_COMMAND = os.getenv("BROWSER_ARTICLE_FETCH_COMMAND", "node").strip() or "node"
BROWSER_ARTICLE_FETCH_NODE_MODULES = os.getenv("BROWSER_ARTICLE_FETCH_NODE_MODULES", "").strip()
BROWSER_ARTICLE_FETCH_ENDPOINT_FILE = os.getenv("BROWSER_ARTICLE_FETCH_ENDPOINT_FILE", "").strip()

# 单选字段选项（需与你在表格中设置一致）
STATUS_IDLE = "idle"
STATUS_OK = "ok"
STATUS_UNSTABLE = "unstable"
STATUS_DEAD = "dead"
STATUS_OPTIONS = {STATUS_IDLE, STATUS_OK, STATUS_UNSTABLE, STATUS_DEAD}

FETCH_STATUS_SUCCESS = "success"
FETCH_STATUS_TIMEOUT = "timeout"
FETCH_STATUS_HTTP_ERROR = "http_error"
FETCH_STATUS_PARSE_ERROR = "parse_error"
FETCH_STATUS_OPTIONS = {FETCH_STATUS_SUCCESS, FETCH_STATUS_TIMEOUT, FETCH_STATUS_HTTP_ERROR, FETCH_STATUS_PARSE_ERROR}

ITEM_ID_STRATEGY_OPTIONS = {"guid", "link", "title_pubdate", "content_hash"}
CONTENT_LANGUAGE_OPTIONS = {"zh", "en", "jp", "mixed", "other"}
KEYWORD_TYPE_OPTIONS = {
    "org",
    "person",
    "product",
    "model",
    "technology",
    "hardware",
    "policy",
    "case",
    "topic",
}

HTTP_TIMEOUT = 20
HTTP_RETRIES = 3
# Feishu occasionally returns a blank 503 for tens of seconds.  Keep generic
# HTTP retries short, but give Feishu calls enough time to ride out that
# transient window without replaying the whole ingest job.
FEISHU_HTTP_RETRIES = int(os.getenv("FEISHU_HTTP_RETRIES", "7"))
FEISHU_RETRY_BASE_SECONDS = float(os.getenv("FEISHU_RETRY_BASE_SECONDS", "1.5"))
FEISHU_RETRY_MAX_SECONDS = float(os.getenv("FEISHU_RETRY_MAX_SECONDS", "30"))
USE_SYSTEM_PROXY = os.getenv("USE_SYSTEM_PROXY", "true").lower() in {"1", "true", "yes", "y"}
ENABLE_IMAGE_ATTACHMENTS = os.getenv("ENABLE_IMAGE_ATTACHMENTS", "true").lower() in {"1", "true", "yes", "y"}
IMAGE_ATTACHMENT_MAX_PER_RECORD = int(os.getenv("IMAGE_ATTACHMENT_MAX_PER_RECORD", "10"))
IMAGE_ATTACHMENT_MAX_BYTES = int(os.getenv("IMAGE_ATTACHMENT_MAX_BYTES", str(5 * 1024 * 1024)))
IMAGE_ATTACHMENT_TIMEOUT = int(os.getenv("IMAGE_ATTACHMENT_TIMEOUT", "12"))
IMAGE_ATTACHMENT_PROXY_FAKE_IP_HOSTS = frozenset(
    host.strip().rstrip(".").lower()
    for host in os.getenv(
        "IMAGE_ATTACHMENT_PROXY_FAKE_IP_HOSTS",
        "pbs.twimg.com,video.twimg.com,article-images.zsxq.com,breakout-1301344553.cos.ap-beijing.myqcloud.com,i.redd.it,preview.redd.it,external-preview.redd.it,mmbiz.qpic.cn",
    ).split(",")
    if host.strip()
)
ARTICLE_FETCH_MAX_BYTES = int(os.getenv("ARTICLE_FETCH_MAX_BYTES", str(2 * 1024 * 1024)))

GEMINI_TIMEOUT = 180
GEMINI_RETRIES = 10
FEISHU_MIN_SCORE = float(os.getenv("FEISHU_MIN_SCORE", "6.0"))
FAILED_ITEMS_MAX = int(os.getenv("FAILED_ITEMS_MAX", "50"))
FAILED_ITEMS_RETRY_LIMIT = int(os.getenv("FAILED_ITEMS_RETRY_LIMIT", "5"))
FAILED_ITEMS_MAX_AGE_DAYS = int(os.getenv("FAILED_ITEMS_MAX_AGE_DAYS", "7"))
FAILED_ITEMS_MAX_MISS = int(os.getenv("FAILED_ITEMS_MAX_MISS", "3"))
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ark").strip().lower()
SCREEN_PROVIDER = os.getenv("SCREEN_PROVIDER", "").strip().lower()
TEXT_DEDUP_PROVIDER = os.getenv("TEXT_DEDUP_PROVIDER", "ark").strip().lower()
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1").rstrip("/")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "ollama")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "deepseek-v4-flash:cloud")
OLLAMA_SCREEN_MODEL = os.getenv("OLLAMA_SCREEN_MODEL", "").strip()
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "120"))
OLLAMA_RETRIES = int(os.getenv("OLLAMA_RETRIES", "5"))
OLLAMA_FALLBACK_MODEL = os.getenv("OLLAMA_FALLBACK_MODEL", "").strip()
OLLAMA_FALLBACK_PROVIDER = os.getenv("OLLAMA_FALLBACK_PROVIDER", "ark").strip().lower()
IFLOW_API_KEY = os.getenv("IFLOW_API_KEY", "")
IFLOW_BASE_URL = os.getenv("IFLOW_BASE_URL", "https://apis.iflow.cn/v1").rstrip("/")
IFLOW_MODEL = os.getenv("IFLOW_MODEL", "qwen3-max")
IFLOW_TIMEOUT = int(os.getenv("IFLOW_TIMEOUT", "60"))
IFLOW_RETRIES = int(os.getenv("IFLOW_RETRIES", "10"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "60"))
OPENAI_RETRIES = int(os.getenv("OPENAI_RETRIES", "10"))

ARK_API_KEY = os.getenv("ARK_API_KEY", "")
ARK_API_KEY_2 = os.getenv("ARK_API_KEY_2", "")
ARK_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3").rstrip("/")
ARK_MODEL = os.getenv("ARK_MODEL", "deepseek-v4-flash")
ARK_TIMEOUT = int(os.getenv("ARK_TIMEOUT", "60"))
ARK_RETRIES = int(os.getenv("ARK_RETRIES", "10"))
ARK_PARSE_RETRIES = int(os.getenv("ARK_PARSE_RETRIES", "3"))
ARK_DISABLE_THINKING = os.getenv("ARK_DISABLE_THINKING", "true").lower() in {"1", "true", "yes", "y"}

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_SCREEN_MODEL = os.getenv("DEEPSEEK_SCREEN_MODEL", "").strip()
DEEPSEEK_TIMEOUT = int(os.getenv("DEEPSEEK_TIMEOUT", "60"))
DEEPSEEK_RETRIES = int(os.getenv("DEEPSEEK_RETRIES", "10"))

ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY", "")
ZHIPU_BASE_URL = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
ZHIPU_MODEL = os.getenv("ZHIPU_MODEL", "glm-4.7")
ZHIPU_TIMEOUT = int(os.getenv("ZHIPU_TIMEOUT", "60"))
ZHIPU_RETRIES = int(os.getenv("ZHIPU_RETRIES", "10"))

SCREEN_VALIDATE_RETRIES = int(os.getenv("SCREEN_VALIDATE_RETRIES", "3"))

NOTIFY_FIELD_EVENT = os.getenv("NOTIFY_FIELD_EVENT", "事件")
NOTIFY_FIELD_DETAIL = os.getenv("NOTIFY_FIELD_DETAIL", "详情")
NOTIFY_FIELD_PLAIN = os.getenv("NOTIFY_FIELD_PLAIN", "说明")
NOTIFY_FIELD_TRIGGER_TIME = os.getenv("NOTIFY_FIELD_TRIGGER_TIME", "触发时间")
NOTIFY_FIELD_NOTIFIED = os.getenv("NOTIFY_FIELD_NOTIFIED", "已通知")

LLM_CONCURRENCY = int(os.getenv("LLM_CONCURRENCY", "4"))
PROGRESS_BAR_WIDTH = int(os.getenv("PROGRESS_BAR_WIDTH", "20"))
PROMPT_TITLE_MAX_CHARS = int(os.getenv("PROMPT_TITLE_MAX_CHARS", "300"))
PROMPT_CONTENT_MAX_CHARS = int(os.getenv("PROMPT_CONTENT_MAX_CHARS", "12000"))
