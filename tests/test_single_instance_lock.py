import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

import pytest  # noqa: E402

import rss_ingest  # noqa: E402


def test_run_with_single_instance_lock_skips_when_existing_pid_is_alive(tmp_path, monkeypatch):
    lock_path = tmp_path / "rss_ingest.lock"
    lock_path.write_text('{"pid": 12345}', encoding="utf-8")
    calls = []

    monkeypatch.setattr(rss_ingest, "_pid_exists", lambda pid: pid == 12345)

    rc = rss_ingest.run_with_single_instance_lock(lambda: calls.append("ran"), lock_path=lock_path)

    assert rc == 0
    assert calls == []
    assert lock_path.exists()


def test_run_with_single_instance_lock_removes_stale_lock_and_runs(tmp_path, monkeypatch):
    lock_path = tmp_path / "rss_ingest.lock"
    lock_path.write_text('{"pid": 12345}', encoding="utf-8")
    calls = []

    monkeypatch.setattr(rss_ingest, "_pid_exists", lambda pid: False)

    rc = rss_ingest.run_with_single_instance_lock(lambda: calls.append("ran"), lock_path=lock_path)

    assert rc == 0
    assert calls == ["ran"]
    assert not lock_path.exists()


def test_run_with_single_instance_lock_releases_lock_on_exception(tmp_path):
    lock_path = tmp_path / "rss_ingest.lock"

    def fail():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        rss_ingest.run_with_single_instance_lock(fail, lock_path=lock_path)

    assert not lock_path.exists()
