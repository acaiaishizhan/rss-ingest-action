# -*- coding: utf-8 -*-
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rss_ingest
import task_alerts


def test_cli_entrypoint_notifies_on_nonzero_exit(monkeypatch):
    calls = []
    monkeypatch.setattr(rss_ingest, "run_with_single_instance_lock", lambda: 3)
    monkeypatch.setenv("RSS_INGEST_ALERT_LOG_PATH", "out/rss-ingest/logs/current.log")
    monkeypatch.setattr(
        task_alerts,
        "notify_failure",
        lambda task, code, **kwargs: calls.append((task, code, kwargs)) or True,
    )

    assert rss_ingest.cli_entrypoint() == 3
    assert calls == [
        (
            "rss-ingest-fetch",
            3,
            {"log_path": str(rss_ingest.config.BASE_DIR / "out/rss-ingest/logs/current.log")},
        )
    ]


def test_cli_entrypoint_silent_on_success(monkeypatch):
    calls = []
    monkeypatch.setattr(rss_ingest, "run_with_single_instance_lock", lambda: 0)
    monkeypatch.setattr(task_alerts, "notify_failure", lambda task, code, **k: calls.append((task, code)) or True)

    assert rss_ingest.cli_entrypoint() == 0
    assert calls == []


def test_cli_entrypoint_notifies_then_reraises_on_crash(monkeypatch):
    calls = []

    def boom():
        raise RuntimeError("ingest crashed")

    monkeypatch.setattr(rss_ingest, "run_with_single_instance_lock", boom)
    monkeypatch.setattr(task_alerts, "notify_failure", lambda task, code, **k: calls.append((task, code)) or True)

    with pytest.raises(RuntimeError):
        rss_ingest.cli_entrypoint()
    assert calls == [("rss-ingest-fetch", 1)]


def test_cli_entrypoint_alert_error_does_not_mask_exit_code(monkeypatch):
    monkeypatch.setattr(rss_ingest, "run_with_single_instance_lock", lambda: 2)

    def alert_boom(task, code, **k):
        raise RuntimeError("webhook down")

    monkeypatch.setattr(task_alerts, "notify_failure", alert_boom)

    assert rss_ingest.cli_entrypoint() == 2


def test_cli_entrypoint_writes_run_log_and_passes_it_to_alert(tmp_path, monkeypatch):
    log_path = tmp_path / "rss-ingest.log"
    calls = []

    def failed_run():
        rss_ingest.log("[Summary] sources_failed=1")
        print("[Feishu] create record error: field missing")
        print("[stderr] provider detail", file=sys.stderr)
        return 1

    monkeypatch.setattr(rss_ingest, "run_with_single_instance_lock", failed_run)
    monkeypatch.setattr(
        task_alerts,
        "notify_failure",
        lambda task, code, **kwargs: calls.append((task, code, kwargs)) or True,
    )

    assert rss_ingest.cli_entrypoint(run_log_path=str(log_path)) == 1
    log_text = log_path.read_text(encoding="utf-8")
    assert "sources_failed=1" in log_text
    assert "create record error: field missing" in log_text
    assert "provider detail" in log_text
    assert calls == [
        ("rss-ingest-fetch", 1, {"log_path": str(log_path)})
    ]
