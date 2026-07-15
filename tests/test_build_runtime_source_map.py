import json
from pathlib import Path

import pytest

from tools.build_runtime_source_map import build_source_map, write_source_map


def _record(record_id: str, feed_url: str) -> dict:
    return {"record_id": record_id, "fields": {"feed_url": [{"text": feed_url}]}}


def test_build_source_map_matches_exact_local_feed_urls() -> None:
    payload = build_source_map(
        [
            _record("rec-we", "http://localhost:8001/feed/all.rss"),
            _record("rec-private", "http://127.0.0.1:8787/rss/all.xml"),
            _record("rec-public", "https://example.com/feed.xml"),
        ]
    )

    assert payload == {
        "version": 1,
        "sources": {
            "rec-we": "feeds/we-mp-rss.xml",
            "rec-private": "feeds/private-rss.xml",
        },
    }


def test_build_source_map_fails_when_a_required_feed_is_missing_or_duplicated() -> None:
    with pytest.raises(RuntimeError, match="missing required local RSS source"):
        build_source_map([_record("rec-we", "http://localhost:8001/feed/all.rss")])

    with pytest.raises(RuntimeError, match="multiple RSS source records"):
        build_source_map(
            [
                _record("rec-we-1", "http://localhost:8001/feed/all.rss"),
                _record("rec-we-2", "http://localhost:8001/feed/all.rss"),
                _record("rec-private", "http://127.0.0.1:8787/rss/all.xml"),
            ]
        )


def test_write_source_map_is_atomic_and_does_not_log_record_ids(tmp_path: Path, capsys) -> None:
    output = tmp_path / "source-map.json"
    payload = {
        "version": 1,
        "sources": {
            "rec-sensitive": "feeds/private-rss.xml",
            "rec-sensitive-2": "feeds/we-mp-rss.xml",
        },
    }

    write_source_map(output, payload)

    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert "rec-sensitive" not in capsys.readouterr().out
