import pytest
from source_runtime import select_ingest_lane, SourceRuntimeConfigError
from source_runtime import grok_source_ids
import json


def test_rss_and_grok_select_disjoint_sources_before_processing():
    sources = [
        {"record_id": "rss", "feed_url": "https://example.test/feed.xml"},
        {"record_id": "grok-local", "feed_url": "file:///local/data/grok-feeds/tips.xml"},
        {"record_id": "grok-snapshot", "feed_url": "/runtime-data/feeds/grok/cases.xml"},
    ]
    assert [s["record_id"] for s in select_ingest_lane(sources, "rss")] == ["rss"]
    assert [s["record_id"] for s in select_ingest_lane(sources, "grok")] == ["grok-local", "grok-snapshot"]
    with pytest.raises(SourceRuntimeConfigError):
        select_ingest_lane(sources, "all")


def test_per_item_transient_failure_cannot_bypass_task_alert_owner(monkeypatch):
    import rss_ingest
    calls = []
    monkeypatch.setenv("RSS_TASK_ALERTS_ONLY", "true")
    monkeypatch.setattr(rss_ingest, "create_bitable_record", lambda *a, **k: calls.append(a))
    rss_ingest.notify_root_cause("temporary failure", "HTTP 502", "server_error")
    assert calls == []


def test_producer_identity_survives_an_unavailable_mapped_snapshot(tmp_path):
    mapping = tmp_path/"source-map.json"
    mapping.write_text(json.dumps({"sources":{"recGrok":"feeds/grok/tips.xml"}}))
    sources = [{"record_id":"recGrok","feed_url":"http://private-host/local/tips"},
               {"record_id":"recRSS","feed_url":"https://example.test/rss.xml"}]
    ids = grok_source_ids(sources, str(mapping))
    assert ids == {"recGrok"}
    assert [source["record_id"] for source in select_ingest_lane(sources,"rss",ids)] == ["recRSS"]
