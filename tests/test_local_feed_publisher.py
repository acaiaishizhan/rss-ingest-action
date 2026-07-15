import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from tools.local_feed_publisher import (
    FeedValidationError,
    LocalFeedPublisher,
    PublisherConfig,
    SourceSpec,
    feed_fingerprint,
    validate_feed_bytes,
    watch_signature,
)


RSS_ONE = b"""<?xml version="1.0"?><rss><channel><item><guid>1</guid></item></channel></rss>"""
RSS_TWO = b"""<?xml version="1.0"?><rss><channel><item><guid>2</guid></item></channel></rss>"""
RSS_META_ONE = b"""<?xml version="1.0"?><rss><channel><lastBuildDate>one</lastBuildDate><item><guid>1</guid></item></channel></rss>"""
RSS_META_TWO = b"""<?xml version="1.0"?><rss><channel><lastBuildDate>two</lastBuildDate><item><guid>1</guid></item></channel></rss>"""


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


def test_validate_feed_bytes_accepts_rss_and_atom() -> None:
    assert validate_feed_bytes(RSS_ONE) == 1
    atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>1</id></entry></feed>'
    assert validate_feed_bytes(atom) == 1


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
