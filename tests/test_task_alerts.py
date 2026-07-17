# -*- coding: utf-8 -*-
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import task_alerts


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"code": 0}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def test_build_message_contains_task_exit_code_and_log_tail():
    text = task_alerts.build_message("keyword-alias-daily", 1, log_tail="boom line")

    assert "keyword-alias-daily" in text
    assert "退出码 1" in text
    assert "boom line" in text


def test_build_message_explains_ark_quota_failure_in_plain_language():
    log_text = """
[runner] rss-ingest started 2026-07-16T19:20:32
[LLM] analysis failed source=Techmeme reason=rate_limit: HTTP 429: {"error":{"code":"AccountQuotaExceeded","message":"You have exceeded the 5-hour usage quota. It will reset at 2026-07-16 20:07:48 +0800 CST."}}
[Summary] sources_done=127 sources_failed=2 queue_total=73 llm_failed=73 written=0
[LLM] fatal: every queued item failed; marking the run unsuccessful
"""

    text = task_alerts.build_message(
        "rss-ingest-fetch", 1, when="2026-07-16 19:57:09", log_text=log_text
    )

    assert "Ark 的 5 小时调用额度用完了" in text
    assert text.startswith("[自动恢复中] 资讯抓取")
    assert "73 条候选资讯暂未处理" in text
    assert "2026-07-16 20:07:48 自动重置" in text
    assert "entries_fetched" not in text


def test_build_message_explains_keyword_audit_failure_in_plain_language():
    log_text = """
{
  "healthy": false,
  "compact_duplicate_groups": 1,
  "zero_link_keyword_count": 24
}
"""

    text = task_alerts.build_message(
        "keyword-audit-repair-daily", 1, log_text=log_text
    )

    assert "数据巡检没有通过" in text
    assert text.startswith("[待清理] 关键词巡检修复")
    assert "1 组重复关键词" in text
    assert "24 个零关联关键词" in text


def test_build_message_explains_feishu_502_without_dumping_html():
    log_text = """
[RSS] TechTalks new=0
[RSS] fatal error: RuntimeError: [Feishu] non-JSON response: HTTP 502: <!DOCTYPE HTML>
<html><head><title>502 Bad Gateway</title></head><body>Powered by volc-dcdn</body></html>
[runner] rss-ingest finished exit=1
"""

    text = task_alerts.build_message(
        "rss-ingest-fetch", 1, when="2026-07-17 13:37:52", log_text=log_text
    )

    assert text.startswith("[自动重试] 资讯抓取")
    assert "飞书前面的网关连续返回 HTTP 502" in text
    assert "已经写入的记录保留" in text
    assert "连续两班仍失败" in text
    assert "<!DOCTYPE" not in text
    assert "<html>" not in text


@pytest.mark.parametrize(
    ("task", "log_text", "status", "expected"),
    [
        (
            "rss-ingest-fetch",
            "[RSS] fatal error: [Feishu] HTTP 403 permission denied",
            "需要处理",
            "应用权限或数据表权限不足",
        ),
        (
            "rss-ingest-fetch",
            "[LLM] analysis failed: HTTP 401 invalid api key",
            "需要处理",
            "API Key 无效",
        ),
        (
            "rss-ingest-fetch",
            "[LLM] analysis failed: HTTP 429 rate_limit\n[Summary] queue_total=8 llm_failed=8",
            "自动重试",
            "临时限流",
        ),
        (
            "grok-watch-hourly",
            "[GrokWatch] topic=cases grok failed: GROK_TEXTBOX_NOT_FOUND",
            "需要处理",
            "找不到输入框",
        ),
        (
            "grok-watch-hourly",
            "[GrokWatch] topic=cases failed: ECONNREFUSED 127.0.0.1:55386",
            "需要处理",
            "浏览器服务没连上",
        ),
        (
            "rss-ingest-fetch",
            "ModuleNotFoundError: No module named 'feedparser'",
            "需要处理",
            "缺少 Python 依赖",
        ),
        (
            "rss-ingest-fetch",
            "OSError: No space left on device",
            "需要处理",
            "磁盘空间不足",
        ),
        (
            "rss-ingest-fetch",
            "[Summary] sources_done=80 sources_failed=20 queue_total=0\n[RSS] fatal source failures",
            "需要排查",
            "20 个 RSS 源失败",
        ),
    ],
)
def test_diagnosis_matrix_covers_common_task_failures(task, log_text, status, expected):
    diagnosis = task_alerts.diagnose_failure(task, log_text)

    assert diagnosis.status == status
    assert expected in diagnosis.cause


def test_unknown_failure_keeps_one_clean_clue_without_html():
    text = task_alerts.build_message(
        "rss-ingest-fetch",
        1,
        log_text="fatal: strange failure <html><body>noisy page</body></html>",
    )

    assert text.startswith("[需要排查] 资讯抓取")
    assert "线索：fatal: strange failure noisy page" in text
    assert "<html>" not in text


def test_read_log_context_uses_latest_rss_run_only(tmp_path):
    log_path = tmp_path / "rss.log"
    log_path.write_text(
        "[runner] rss-ingest started old\nAccountQuotaExceeded\n"
        "[runner] rss-ingest started new\nnew failure\n",
        encoding="utf-8",
    )

    context = task_alerts.read_log_context(log_path)

    assert "new failure" in context
    assert "AccountQuotaExceeded" not in context


def test_should_alert_respects_cooldown(tmp_path):
    state_dir = tmp_path / "alerts"

    assert task_alerts.should_alert("t1", state_dir, now=1000.0, cooldown=100.0)
    task_alerts.record_alert("t1", state_dir, now=1000.0)
    assert not task_alerts.should_alert("t1", state_dir, now=1050.0, cooldown=100.0)
    assert task_alerts.should_alert("t1", state_dir, now=1101.0, cooldown=100.0)


def test_read_log_tail_handles_utf16_and_missing_file(tmp_path):
    utf16_log = tmp_path / "u16.log"
    utf16_log.write_text("line-a\nline-b\n", encoding="utf-16")

    tail = task_alerts.read_log_tail(utf16_log, max_lines=5)
    assert "line-b" in tail
    assert "\x00" not in tail

    assert task_alerts.read_log_tail(tmp_path / "missing.log") == ""


def test_notify_failure_skips_without_webhook(tmp_path, monkeypatch):
    posted = []
    monkeypatch.setattr(task_alerts, "http_post", lambda *a, **k: posted.append(a) or FakeResponse())

    ok = task_alerts.notify_failure(
        "rss-ingest-fetch", 1, webhook_url="", state_dir=tmp_path / "alerts"
    )

    assert ok is False
    assert posted == []


def test_notify_failure_posts_text_payload_and_records_state(tmp_path, monkeypatch):
    captured = {}

    def fake_http_post(url, headers, json_body, timeout, retries, params=None):
        captured["url"] = url
        captured["json_body"] = json_body
        return FakeResponse()

    monkeypatch.setattr(task_alerts, "http_post", fake_http_post)
    state_dir = tmp_path / "alerts"

    ok = task_alerts.notify_failure(
        "grok-watch-hourly",
        2,
        webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/xxx",
        state_dir=state_dir,
        now=5000.0,
    )

    assert ok is True
    assert captured["url"].startswith("https://open.feishu.cn")
    assert captured["json_body"]["msg_type"] == "text"
    assert "grok-watch-hourly" in captured["json_body"]["content"]["text"]
    # 同一任务冷却期内不重复告警
    assert not task_alerts.should_alert("grok-watch-hourly", state_dir, now=5100.0)


def test_webhook_http_200_with_business_error_is_not_success(monkeypatch):
    monkeypatch.setattr(
        task_alerts,
        "http_post",
        lambda *args, **kwargs: FakeResponse(
            status_code=200,
            payload={"code": 19002, "msg": "invalid webhook token"},
        ),
    )

    assert task_alerts.send_webhook("https://example.com/hook", "boom") is False


def test_notify_failure_within_cooldown_does_not_post(tmp_path, monkeypatch):
    posted = []
    monkeypatch.setattr(task_alerts, "http_post", lambda *a, **k: posted.append(a) or FakeResponse())
    state_dir = tmp_path / "alerts"
    task_alerts.record_alert("t2", state_dir, now=1000.0)

    ok = task_alerts.notify_failure(
        "t2", 1, webhook_url="https://example.com/hook", state_dir=state_dir, now=1100.0
    )

    assert ok is False
    assert posted == []


def test_notify_failure_cooldown_is_per_failure_type(tmp_path, monkeypatch):
    posted = []
    monkeypatch.setattr(task_alerts, "http_post", lambda *a, **k: posted.append(a) or FakeResponse())
    state_dir = tmp_path / "alerts"
    log_path = tmp_path / "run.log"
    log_path.write_text(
        "[RSS] fatal error: [Feishu] non-JSON response: HTTP 502 Bad Gateway",
        encoding="utf-8",
    )

    assert task_alerts.notify_failure(
        "rss-ingest-fetch",
        1,
        log_path=str(log_path),
        webhook_url="https://example.com/hook",
        state_dir=state_dir,
        now=1000.0,
    )
    assert not task_alerts.notify_failure(
        "rss-ingest-fetch",
        1,
        log_path=str(log_path),
        webhook_url="https://example.com/hook",
        state_dir=state_dir,
        now=1100.0,
    )

    log_path.write_text(
        "[RSS] fatal error: [Feishu] HTTP 403 permission denied",
        encoding="utf-8",
    )
    assert task_alerts.notify_failure(
        "rss-ingest-fetch",
        1,
        log_path=str(log_path),
        webhook_url="https://example.com/hook",
        state_dir=state_dir,
        now=1100.0,
    )
    assert len(posted) == 2


def test_main_never_fails_the_caller(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(task_alerts, "http_post", boom)

    rc = task_alerts.main(
        [
            "--task", "keyword-alias-daily",
            "--exit-code", "1",
            "--webhook-url", "https://example.com/hook",
            "--state-dir", str(tmp_path / "alerts"),
        ]
    )

    assert rc == 0
