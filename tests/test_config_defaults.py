import importlib
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def reload_config(env):
    for key in list(os.environ.keys()):
        if (
            key.startswith("LLM_")
            or key.startswith("LOCAL_")
            or key.startswith("QWEN_")
            or key.startswith("GEMINI_")
            or key.startswith("FEATURED_")
            or key.startswith("DEEP_ANALYSIS_")
            or key.startswith("DEEPSEEK_")
            or key.startswith("ARK_")
            or key.startswith("OLLAMA_")
            or key.startswith("KIMI_")
            or key.startswith("MOONSHOT_")

            or key.startswith("PROMPT_")
            or key.startswith("FEISHU_")
            or key.startswith("RSS_FETCH_")
            or key.startswith("RSS_INGEST_")
            or key.startswith("SCREEN_")
            or key.startswith("TEXT_DEDUP_")
            or key.startswith("IMAGE_")
            or key == "USE_SYSTEM_PROXY"
        ):
            os.environ.pop(key, None)
    os.environ["RSS_INGEST_SKIP_LOCAL_ENV"] = "true"
    os.environ.update(env)
    if "config" in sys.modules:
        del sys.modules["config"]
    import config
    importlib.reload(config)
    return config


def test_config_defaults_and_removes_featured_settings():
    cfg = reload_config(
        {
            "LLM_CONCURRENCY": "4",
        }
    )
    assert cfg.LLM_CONCURRENCY == 4
    assert cfg.LLM_PROVIDER == "ark"
    assert cfg.TEXT_DEDUP_PROVIDER == "ark"
    assert cfg.SCREEN_VALIDATE_RETRIES == 3
    assert cfg.NEWS_CATEGORY_OPTIONS == (
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
    assert cfg.RSS_FETCH_CONCURRENCY == 20
    assert cfg.RSS_FETCH_PER_HOST_CONCURRENCY == 4
    assert cfg.RSS_FETCH_RETRY_CONCURRENCY == 4
    assert cfg.RSS_INGEST_ITEM_KEY_PREFETCH_ATTEMPTS == 2
    assert cfg.IMAGE_ATTACHMENT_MAX_PER_RECORD == 10
    assert not hasattr(cfg, "FEATURED_LLM_PROVIDER")
    assert not hasattr(cfg, "FEATURED_RETRIES")
    assert not hasattr(cfg, "FEATURED_PROMPT")
    assert not hasattr(cfg, "DEEP_ANALYSIS_PROMPT_OVERRIDE")


def test_config_defaults_to_volcengine_deepseek_flash():
    cfg = reload_config({})
    assert cfg.LLM_PROVIDER == "ark"
    assert cfg.TEXT_DEDUP_PROVIDER == "ark"
    assert cfg.ARK_BASE_URL == "https://ark.cn-beijing.volces.com/api/coding/v3"
    assert cfg.ARK_MODEL == "deepseek-v4-flash"
    assert cfg.ARK_PARSE_RETRIES == 3
    assert cfg.ARK_DISABLE_THINKING is True
    assert cfg.DEEPSEEK_MODEL == "deepseek-v4-flash"
    assert cfg.DEEPSEEK_SCREEN_MODEL == ""


def test_config_allows_deepseek_screen_override():
    cfg = reload_config(
        {
            "SCREEN_PROVIDER": "deepseek",
            "DEEPSEEK_SCREEN_MODEL": "deepseek-v4-flash",
            "DEEPSEEK_MODEL": "deepseek-chat",
        }
    )
    assert cfg.SCREEN_PROVIDER == "deepseek"
    assert cfg.DEEPSEEK_SCREEN_MODEL == "deepseek-v4-flash"
    assert cfg.DEEPSEEK_MODEL == "deepseek-chat"


def test_config_allows_overriding_feishu_min_score():
    cfg = reload_config(
        {
            "FEISHU_MIN_SCORE": "7.5",
        }
    )
    assert cfg.FEISHU_MIN_SCORE == 7.5


def test_config_allows_overriding_triage_score_gate():
    cfg = reload_config(
        {
            "ENABLE_TRIAGE_SCORE_GATE": "false",
            "TRIAGE_MIN_SCORE": "4.2",
        }
    )
    assert cfg.ENABLE_TRIAGE_SCORE_GATE is False
    assert cfg.TRIAGE_MIN_SCORE == 4.2


def test_config_allows_overriding_rss_fetch_concurrency():
    cfg = reload_config(
        {
            "RSS_FETCH_CONCURRENCY": "12",
            "RSS_FETCH_PER_HOST_CONCURRENCY": "3",
            "RSS_FETCH_RETRY_CONCURRENCY": "2",
            "RSS_INGEST_ITEM_KEY_PREFETCH_ATTEMPTS": "4",
        }
    )
    assert cfg.RSS_FETCH_CONCURRENCY == 12
    assert cfg.RSS_FETCH_PER_HOST_CONCURRENCY == 3
    assert cfg.RSS_FETCH_RETRY_CONCURRENCY == 2
    assert cfg.RSS_INGEST_ITEM_KEY_PREFETCH_ATTEMPTS == 4


def test_config_allows_disabling_system_proxy():
    cfg = reload_config(
        {
            "USE_SYSTEM_PROXY": "false",
        }
    )
    assert cfg.USE_SYSTEM_PROXY is False


def test_config_no_longer_exposes_unused_gemini_pro_model():
    cfg = reload_config(
        {
            "GEMINI_MODEL_NAME": "gemini-3.1-pro-preview",
        }
    )
    assert cfg.GEMINI_MODEL_NAME == "gemini-3.1-pro-preview"
    assert not hasattr(cfg, "GEMINI_MODEL_NAME_PRO")


def test_config_no_longer_exposes_gemini_summary_model_override():
    cfg = reload_config(
        {
            "GEMINI_MODEL_NAME": "gemini-3.1-pro-preview",
            "GEMINI_MODEL_NAME_SUMMARY": "gemini-3-flash-preview",
        }
    )
    assert cfg.GEMINI_MODEL_NAME == "gemini-3.1-pro-preview"
    assert not hasattr(cfg, "GEMINI_MODEL_NAME_SUMMARY")


def test_config_supports_split_local_prompt_paths_stage_models_and_filtered_table():
    cfg = reload_config(
        {
            "LOCAL_KEYWORD_BLOCKLIST_PATH": "docs/custom-keywords.txt",
            "LOCAL_DEDUP_ALIAS_GROUPS_PATH": "docs/custom-dedup-alias-groups.json",
            "LOCAL_SCREEN_PROMPT_PATH": "docs/custom-screen.md",
            "LOCAL_SUMMARIZE_PROMPT_PATH": "docs/custom-summary.md",
            "FEISHU_FILTERED_TABLE_ID": "tbl_filtered",
            "FEISHU_KEYWORD_TABLE_ID": "tbl_keyword",
            "PROMPT_TITLE_MAX_CHARS": "256",
            "PROMPT_CONTENT_MAX_CHARS": "4096",
        }
    )
    assert cfg.LOCAL_KEYWORD_BLOCKLIST_PATH == "docs/custom-keywords.txt"
    assert cfg.LOCAL_DEDUP_ALIAS_GROUPS_PATH == "docs/custom-dedup-alias-groups.json"
    assert cfg.LOCAL_SCREEN_PROMPT_PATH == "docs/custom-screen.md"
    assert cfg.LOCAL_SUMMARIZE_PROMPT_PATH == "docs/custom-summary.md"
    assert cfg.FEISHU_FILTERED_TABLE_ID == "tbl_filtered"
    assert cfg.FEISHU_KEYWORD_TABLE_ID == "tbl_keyword"
    assert cfg.NEWS_FIELD_KEYWORD_RECORDS == "关键词记录"
    assert cfg.FILTERED_FIELD_KEYWORD_RECORDS == "关键词记录"
    assert cfg.FILTERED_FIELD_CREATED_TIME == "创建时间"
    assert cfg.KEYWORD_FIELD_CANONICAL_NAME == "规范名"
    assert cfg.KEYWORD_FIELD_TYPE == "类型"
    assert cfg.KEYWORD_FIELD_ALIASES == "归一项"
    assert cfg.KEYWORD_FIELD_FIRST_SEEN == "首次出现"
    assert cfg.KEYWORD_FIELD_NOTE == "备注"
    assert cfg.KEYWORD_FIELD_NEWS_COUNT == "NEWS次数"
    assert cfg.KEYWORD_FIELD_FILTERED_COUNT == "FILTERED次数"
    assert cfg.KEYWORD_FIELD_NEWS_24H == "NEWS24h"
    assert cfg.KEYWORD_FIELD_LAST_SEEN == "最后出现"
    assert cfg.KEYWORD_FIELD_HEAT_SAMPLE == "热度样本"
    assert cfg.KEYWORD_FIELD_PARENT == "父关键词"
    assert cfg.KEYWORD_FIELD_OWNERS == "归属关键词"
    assert "local-keyword-name-blocklist.txt" in cfg.LOCAL_KEYWORD_NAME_BLOCKLIST_PATH
    assert cfg.PROMPT_TITLE_MAX_CHARS == 256
    assert cfg.PROMPT_CONTENT_MAX_CHARS == 4096


def test_config_no_longer_exposes_system_prompt_override():
    cfg = reload_config(
        {
            "SYSTEM_PROMPT_OVERRIDE": "legacy prompt",
        }
    )
    assert not hasattr(cfg, "SYSTEM_PROMPT_OVERRIDE")
