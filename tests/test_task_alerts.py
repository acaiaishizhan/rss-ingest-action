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
    assert "exit=1" in text
    assert "boom line" in text


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
