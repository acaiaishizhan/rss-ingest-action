import json
from pathlib import Path

import pytest

from tools.build_runtime_source_map import REQUIRED_FEEDS, build_source_map, write_source_map


def _record(record_id: str, feed_url: str) -> dict:
    return {"record_id": record_id, "fields": {"feed_url": [{"text": feed_url}]}}


def _required_records() -> list[dict]:
    return [
        _record(f"rec-{index}", feed_url)
        for index, feed_url in enumerate(REQUIRED_FEEDS, start=1)
    ]


def test_build_source_map_matches_exact_local_feed_urls() -> None:
    records = _required_records()
    records.append(_record("rec-public", "https://example.com/feed.xml"))
    payload = build_source_map(records)

    assert payload == {
        "version": 1,
        "sources": {
            f"rec-{index}": target
            for index, target in enumerate(REQUIRED_FEEDS.values(), start=1)
        },
    }
    assert len(payload["sources"]) == 18


def test_build_source_map_fails_when_a_required_feed_is_missing_or_duplicated() -> None:
    with pytest.raises(RuntimeError, match="missing required local RSS source"):
        build_source_map(_required_records()[:-1])

    records = _required_records()
    duplicated_url = next(iter(REQUIRED_FEEDS))
    records.append(_record("rec-duplicate", duplicated_url))
    with pytest.raises(RuntimeError, match="multiple RSS source records"):
        build_source_map(records)


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
