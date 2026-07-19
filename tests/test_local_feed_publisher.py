import json
import subprocess
import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest

from tools.local_feed_publisher import (
    FeedNotReadyError,
    FeedValidationError,
    LocalFeedPublisher,
    PublisherConfig,
    SourceSpec,
    build_prompthub_blog_feed,
    feed_fingerprint,
    sanitize_we_mp_feed_content,
    validate_feed_bytes,
    validate_keyword_snapshot_bytes,
    validate_we_mp_feed_content,
    watch_signature,
)


RSS_ONE = b"""<?xml version="1.0"?><rss><channel><item><guid>1</guid></item></channel></rss>"""
RSS_TWO = b"""<?xml version="1.0"?><rss><channel><item><guid>2</guid></item></channel></rss>"""
RSS_META_ONE = b"""<?xml version="1.0"?><rss><channel><lastBuildDate>one</lastBuildDate><item><guid>1</guid></item></channel></rss>"""
RSS_META_TWO = b"""<?xml version="1.0"?><rss><channel><lastBuildDate>two</lastBuildDate><item><guid>1</guid></item></channel></rss>"""
WE_MP_READY = b"""<?xml version="1.0"?><rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item><guid>1</guid><content:encoded>&lt;p&gt;full body&lt;/p&gt;</content:encoded></item></channel></rss>"""
WE_MP_PENDING = b"""<?xml version="1.0"?><rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item><guid>1</guid><content:encoded></content:encoded></item></channel></rss>"""


class FakeRunner:
    def __init__(self, *, fail_dispatch_once: bool = False, head_subject: str = "") -> None:
        self.calls = []
        self.fail_dispatch_once = fail_dispatch_once
        self.head_subject = head_subject

    def __call__(self, args, *, cwd=None, check=True):
        call = (list(args), Path(cwd) if cwd else None)
        self.calls.append(call)
        if self.fail_dispatch_once and "workflow" in args:
            self.fail_dispatch_once = False
            raise subprocess.CalledProcessError(1, args)
        stdout = self.head_subject if list(args[:3]) == ["git", "log", "-1"] else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")


def _config(tmp_path: Path) -> PublisherConfig:
    repo_dir = tmp_path / "runtime-data"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    return PublisherConfig(
        data_repo="acaiaishizhan/rss-runtime-data",
        data_repo_dir=repo_dir,
        action_repo="acaiaishizhan/rss-ingest-action",
        workflow_file="rss-ingest.yml",
        action_ref="main",
        state_path=tmp_path / "publisher-state.json",
        log_path=tmp_path / "publisher.log",
        sources=(
            SourceSpec("private-rss", "file:///private.xml", "feeds/private-rss.xml"),
        ),
        watch_paths=(tmp_path / "watched.xml",),
        poll_seconds=1.0,
        settle_seconds=1.0,
        gh_path="gh",
    )


def test_from_env_includes_grok_and_substack_snapshots(monkeypatch, tmp_path: Path) -> None:
    grok_dir = tmp_path / "grok-feeds"
    monkeypatch.setenv("GROK_RSS_SNAPSHOT_DIR", str(grok_dir))

    config = PublisherConfig.from_env()

    grok_sources = [source for source in config.sources if source.name.startswith("grok-")]
    substack_sources = [source for source in config.sources if source.name.startswith("substack-")]
    assert len(config.sources) == 19
    assert len(grok_sources) == 9
    assert len(substack_sources) == 6
    assert all(source.soft_fail for source in substack_sources)
    assert any(source.name == "prompthub-blog" and source.soft_fail for source in config.sources)
    assert {source.target for source in grok_sources} == {
        f"feeds/grok/{key}.xml"
        for key in ("deals", "rumors", "cases", "burst", "tips", "peers", "resources", "codex", "claude")
    }
    assert {grok_dir / f"{key}.xml" for key in ("deals", "rumors", "cases", "burst", "tips", "peers", "resources", "codex", "claude")} <= set(config.watch_paths)
    assert any(source.name == "keyword-snapshot" and source.kind == "json" for source in config.sources)
    assert any(source.name == "prompthub-blog" and source.source == "https://www.prompthub.us/blog" for source in config.sources)


def test_validate_feed_bytes_accepts_rss_and_atom() -> None:
    assert validate_feed_bytes(RSS_ONE) == 1
    atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>1</id></entry></feed>'
    assert validate_feed_bytes(atom) == 1


def test_validate_we_mp_feed_content_rejects_transient_empty_bodies() -> None:
    assert validate_we_mp_feed_content(WE_MP_READY) == 1
    with pytest.raises(FeedNotReadyError, match="not ready for 1/1 items"):
        validate_we_mp_feed_content(WE_MP_PENDING)


def test_sanitize_we_mp_feed_drops_stale_empty_body() -> None:
    stale = b"""<?xml version="1.0"?><rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item><guid>old</guid><pubDate>Sat, 18 Jul 2026 22:26:17 +0800</pubDate><content:encoded></content:encoded></item><item><guid>ready</guid><content:encoded>&lt;p&gt;body&lt;/p&gt;</content:encoded></item></channel></rss>"""

    sanitized, dropped = sanitize_we_mp_feed_content(
        stale,
        now=dt.datetime(2026, 7, 19, 11, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
        grace_seconds=3600,
    )

    assert dropped == 1
    assert b"old" not in sanitized
    assert b"ready" in sanitized


def test_sanitize_we_mp_feed_waits_for_fresh_empty_body() -> None:
    fresh = b"""<?xml version="1.0"?><rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item><guid>fresh</guid><pubDate>Sun, 19 Jul 2026 10:45:00 +0800</pubDate><content:encoded></content:encoded></item></channel></rss>"""

    with pytest.raises(FeedNotReadyError, match="fresh items"):
        sanitize_we_mp_feed_content(
            fresh,
            now=dt.datetime(2026, 7, 19, 11, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
            grace_seconds=3600,
        )


def test_validate_keyword_snapshot_bytes_requires_v2_and_enough_entries() -> None:
    payload = json.dumps(
        {"schema_version": 2, "entries": [{"record_id": str(i)} for i in range(3)]}
    ).encode()

    assert validate_keyword_snapshot_bytes(payload, min_entries=3) == 3


def test_build_prompthub_blog_feed_converts_official_blog_cards() -> None:
    page = b"""<html><body><a href="/blog/one" class="blog-post-preview-content"><h2 class="blog-title-list-page">First &amp; Best</h2><p class="blog-date">October 23, 2025</p></a></body></html>"""

    feed = build_prompthub_blog_feed(page, "https://www.prompthub.us/blog")

    assert validate_feed_bytes(feed) == 1
    assert b"https://www.prompthub.us/blog/one" in feed
    assert b"First &amp; Best" in feed


def test_feed_fingerprint_ignores_feed_level_metadata() -> None:
    assert RSS_META_ONE != RSS_META_TWO
    assert feed_fingerprint(RSS_META_ONE) == feed_fingerprint(RSS_META_TWO)
    assert feed_fingerprint(RSS_META_ONE) != feed_fingerprint(RSS_TWO)


@pytest.mark.parametrize(
    "payload",
    [
        b"not xml",
        b"<html><body>login required</body></html>",
        b"<rss><channel /></rss>",
    ],
)
def test_validate_feed_bytes_rejects_invalid_or_empty_feeds(payload: bytes) -> None:
    with pytest.raises(FeedValidationError):
        validate_feed_bytes(payload)


def test_sync_once_writes_changed_feed_pushes_and_dispatches(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = config.data_repo_dir / "feeds/private-rss.xml"
    target.parent.mkdir()
    target.write_bytes(RSS_ONE)
    runner = FakeRunner()
    publisher = LocalFeedPublisher(
        config,
        runner=runner,
        source_reader=lambda source: RSS_TWO,
    )

    result = publisher.sync_once()

    assert result.changed == ["private-rss"]
    assert result.errors == {}
    assert result.dispatched is True
    assert target.read_bytes() == RSS_TWO
    flattened = [call[0] for call in runner.calls]
    assert any(args[:2] == ["git", "commit"] for args in flattened)
    assert any(args[:2] == ["git", "push"] for args in flattened)
    assert any(args[:3] == ["gh", "workflow", "run"] for args in flattened)
    assert json.loads(config.state_path.read_text(encoding="utf-8"))["dispatch_pending"] is False


def test_sync_once_does_not_commit_or_dispatch_unchanged_feed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = config.data_repo_dir / "feeds/private-rss.xml"
    target.parent.mkdir()
    target.write_bytes(RSS_ONE)
    runner = FakeRunner()
    publisher = LocalFeedPublisher(config, runner=runner, source_reader=lambda source: RSS_ONE)

    result = publisher.sync_once()

    assert result.changed == []
    assert result.dispatched is False
    flattened = [call[0] for call in runner.calls]
    assert not any(args[:2] == ["git", "commit"] for args in flattened)
    assert not any(args[:3] == ["gh", "workflow", "run"] for args in flattened)


def test_sync_once_ignores_metadata_only_feed_changes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = config.data_repo_dir / "feeds/private-rss.xml"
    target.parent.mkdir()
    target.write_bytes(RSS_META_ONE)
    runner = FakeRunner()
    publisher = LocalFeedPublisher(config, runner=runner, source_reader=lambda source: RSS_META_TWO)

    result = publisher.sync_once()

    assert result.changed == []
    assert result.dispatched is False
    assert target.read_bytes() == RSS_META_ONE
    flattened = [call[0] for call in runner.calls]
    assert not any(args[:2] == ["git", "commit"] for args in flattened)
    assert not any(args[:3] == ["gh", "workflow", "run"] for args in flattened)


def test_sync_once_can_push_without_dispatching_during_preflight(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), dispatch_enabled=False)
    target = config.data_repo_dir / "feeds/private-rss.xml"
    target.parent.mkdir()
    target.write_bytes(RSS_ONE)
    runner = FakeRunner()
    publisher = LocalFeedPublisher(config, runner=runner, source_reader=lambda source: RSS_TWO)

    result = publisher.sync_once()

    assert result.changed == ["private-rss"]
    assert result.errors == {}
    assert result.dispatched is False
    flattened = [call[0] for call in runner.calls]
    assert any(args[:2] == ["git", "push"] for args in flattened)
    assert not any(args[:3] == ["gh", "workflow", "run"] for args in flattened)
    assert json.loads(config.state_path.read_text(encoding="utf-8"))["dispatch_pending"] is True


def test_sync_once_amends_snapshot_tip_and_force_pushes_with_lease(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = config.data_repo_dir / "feeds/private-rss.xml"
    target.parent.mkdir()
    target.write_bytes(RSS_ONE)
    runner = FakeRunner(head_subject="data: update local RSS feeds (earlier)")
    publisher = LocalFeedPublisher(config, runner=runner, source_reader=lambda source: RSS_TWO)

    result = publisher.sync_once()

    assert result.errors == {}
    flattened = [call[0] for call in runner.calls]
    assert any(args[:3] == ["git", "commit", "--amend"] for args in flattened)
    assert any(args == ["git", "push", "--force-with-lease"] for args in flattened)
    state = json.loads(config.state_path.read_text(encoding="utf-8"))
    assert state["push_pending"] is False
    assert state["push_force"] is False


def test_sync_once_keeps_last_good_feed_when_new_payload_is_invalid(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = config.data_repo_dir / "feeds/private-rss.xml"
    target.parent.mkdir()
    target.write_bytes(RSS_ONE)
    runner = FakeRunner()
    publisher = LocalFeedPublisher(config, runner=runner, source_reader=lambda source: b"<rss />")

    result = publisher.sync_once()

    assert result.changed == []
    assert "private-rss" in result.errors
    assert target.read_bytes() == RSS_ONE
    assert not any(call[0][:2] == ["git", "commit"] for call in runner.calls)


def test_sync_once_defers_incomplete_we_mp_feed_without_failing_task(tmp_path: Path) -> None:
    config = replace(
        _config(tmp_path),
        sources=(SourceSpec("we-mp-rss", "http://127.0.0.1/feed", "feeds/we-mp-rss.xml"),),
    )
    target = config.data_repo_dir / "feeds/we-mp-rss.xml"
    target.parent.mkdir()
    target.write_bytes(WE_MP_READY)
    runner = FakeRunner()
    publisher = LocalFeedPublisher(config, runner=runner, source_reader=lambda source: WE_MP_PENDING)

    result = publisher.sync_once()

    assert result.changed == []
    assert result.errors == {}
    assert "we-mp-rss" in result.deferred
    assert target.read_bytes() == WE_MP_READY
    assert not any(call[0][:2] == ["git", "commit"] for call in runner.calls)


def test_sync_once_soft_fails_public_mirror_and_keeps_last_good_snapshot(tmp_path: Path) -> None:
    config = replace(
        _config(tmp_path),
        sources=(
            SourceSpec(
                "substack-test",
                "https://example.com/feed",
                "feeds/test.xml",
                soft_fail=True,
            ),
        ),
    )
    target = config.data_repo_dir / "feeds/test.xml"
    target.parent.mkdir()
    target.write_bytes(RSS_ONE)
    runner = FakeRunner()

    def fail_reader(source):
        raise RuntimeError("HTTP 403")

    result = LocalFeedPublisher(config, runner=runner, source_reader=fail_reader).sync_once()

    assert result.changed == []
    assert result.errors == {}
    assert result.deferred == {"substack-test": "HTTP 403"}
    assert target.read_bytes() == RSS_ONE


def test_failed_dispatch_is_persisted_and_retried_without_new_feed_change(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = config.data_repo_dir / "feeds/private-rss.xml"
    target.parent.mkdir()
    target.write_bytes(RSS_ONE)
    runner = FakeRunner(fail_dispatch_once=True)
    publisher = LocalFeedPublisher(config, runner=runner, source_reader=lambda source: RSS_TWO)

    first = publisher.sync_once()
    second = publisher.sync_once()

    assert first.dispatched is False
    assert json.loads(config.state_path.read_text(encoding="utf-8"))["dispatch_pending"] is False
    assert second.changed == []
    assert second.dispatched is True
    dispatch_calls = [call for call in runner.calls if call[0][:3] == ["gh", "workflow", "run"]]
    assert len(dispatch_calls) == 2


def test_watch_signature_changes_when_a_watched_file_appears(tmp_path: Path) -> None:
    watched = tmp_path / "feed.xml"
    before = watch_signature([watched])
    watched.write_bytes(RSS_ONE)
    after = watch_signature([watched])

    assert before != after
