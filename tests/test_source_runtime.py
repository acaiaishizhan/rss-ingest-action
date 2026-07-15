import json
from pathlib import Path

import pytest

from source_runtime import SourceRuntimeConfigError, prepare_sources_for_runtime


def _source(record_id: str, feed_url: str, *, enabled: bool = True) -> dict:
    return {
        "record_id": record_id,
        "name": record_id,
        "feed_url": feed_url,
        "enabled": enabled,
    }


def test_all_mode_preserves_every_source() -> None:
    sources = [
        _source("public", "https://example.com/feed.xml"),
        _source("local", "http://127.0.0.1:8787/rss/all.xml"),
        _source("file", r"F:\\feeds\\grok.xml"),
    ]

    selection = prepare_sources_for_runtime(sources, mode="all", override_file="")

    assert selection.sources == sources
    assert selection.skipped == []
    assert selection.overrides_applied == 0


def test_github_mode_keeps_public_sources_and_applies_private_overrides(tmp_path: Path) -> None:
    feeds_dir = tmp_path / "feeds"
    feeds_dir.mkdir()
    private_feed = feeds_dir / "private-rss.xml"
    private_feed.write_text("<rss><channel><item /></channel></rss>", encoding="utf-8")
    mapping_path = tmp_path / "source-map.json"
    mapping_path.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": {
                    "local-private": "feeds/private-rss.xml",
                },
            }
        ),
        encoding="utf-8",
    )
    sources = [
        _source("public", "https://example.com/feed.xml"),
        _source("local-private", "http://127.0.0.1:8787/rss/all.xml"),
        _source("local-unmapped", "http://localhost:8001/feed/all.rss"),
        _source("grok-file", r"F:\\coding\\rss-ingest-local\\data\\grok.xml"),
    ]

    selection = prepare_sources_for_runtime(
        sources,
        mode="github",
        override_file=str(mapping_path),
    )

    assert [source["record_id"] for source in selection.sources] == ["public", "local-private"]
    assert selection.sources[1]["feed_url"] == str(private_feed.resolve())
    assert selection.overrides_applied == 1
    assert {item.record_id for item in selection.skipped} == {"local-unmapped", "grok-file"}


def test_github_mode_skips_missing_override_target_but_keeps_public_sources(tmp_path: Path) -> None:
    mapping_path = tmp_path / "source-map.json"
    mapping_path.write_text(
        json.dumps({"version": 1, "sources": {"local": "feeds/missing.xml"}}),
        encoding="utf-8",
    )

    selection = prepare_sources_for_runtime(
        [
            _source("public", "https://example.com/feed.xml"),
            _source("local", "http://127.0.0.1:8787/rss/all.xml"),
        ],
        mode="github",
        override_file=str(mapping_path),
    )

    assert [source["record_id"] for source in selection.sources] == ["public"]
    assert selection.skipped[0].record_id == "local"
    assert selection.skipped[0].reason == "override target missing"


def test_github_mode_rejects_missing_or_invalid_mapping_file(tmp_path: Path) -> None:
    with pytest.raises(SourceRuntimeConfigError, match="does not exist"):
        prepare_sources_for_runtime(
            [],
            mode="github",
            override_file=str(tmp_path / "missing.json"),
        )

    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(SourceRuntimeConfigError, match="invalid JSON"):
        prepare_sources_for_runtime([], mode="github", override_file=str(invalid_path))


def test_github_mode_rejects_override_path_escape(tmp_path: Path) -> None:
    mapping_path = tmp_path / "source-map.json"
    mapping_path.write_text(
        json.dumps({"version": 1, "sources": {"local": "../secret.xml"}}),
        encoding="utf-8",
    )

    with pytest.raises(SourceRuntimeConfigError, match="must stay inside"):
        prepare_sources_for_runtime([], mode="github", override_file=str(mapping_path))


@pytest.mark.parametrize(
    "feed_url",
    [
        "http://127.0.0.1/feed.xml",
        "http://192.168.1.5/feed.xml",
        "http://host.docker.internal/feed.xml",
        "file:///tmp/feed.xml",
        "/tmp/feed.xml",
        r"F:\\feeds\\feed.xml",
    ],
)
def test_github_mode_skips_unmapped_non_public_sources(feed_url: str) -> None:
    selection = prepare_sources_for_runtime(
        [_source("local", feed_url)],
        mode="github",
        override_file="",
    )

    assert selection.sources == []
    assert selection.skipped[0].record_id == "local"


def test_unknown_source_mode_fails_fast() -> None:
    with pytest.raises(SourceRuntimeConfigError, match="unsupported RSS_SOURCE_MODE"):
        prepare_sources_for_runtime([], mode="cloudish", override_file="")
