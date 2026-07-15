# -*- coding: utf-8 -*-
"""Publish local RSS snapshots through the authenticated WSL GitHub CLI."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Sequence, Tuple
from urllib.parse import unquote, urlparse


LOGGER = logging.getLogger("local-feed-publisher")


class FeedValidationError(ValueError):
    """Raised when a candidate feed must not replace the last good snapshot."""


@dataclass(frozen=True)
class SourceSpec:
    name: str
    source: str
    target: str


@dataclass(frozen=True)
class PublisherConfig:
    data_repo: str
    data_repo_dir: Path
    action_repo: str
    workflow_file: str
    action_ref: str
    state_path: Path
    log_path: Path
    sources: Tuple[SourceSpec, ...]
    watch_paths: Tuple[Path, ...]
    poll_seconds: float
    settle_seconds: float
    gh_path: str
    lock_path: Path | None = None

    @classmethod
    def from_env(cls) -> "PublisherConfig":
        home = Path.home()
        private_feed = Path(
            os.getenv(
                "PRIVATE_RSS_SNAPSHOT_PATH",
                "/mnt/f/coding/solo-company/tools/private-rss/data/all.xml",
            )
        )
        we_mp_db = Path(
            os.getenv(
                "WE_MP_RSS_DB_PATH",
                "/mnt/f/coding/we-mp-rss/data/db.db",
            )
        )
        state_dir = Path(
            os.getenv(
                "LOCAL_FEED_PUBLISHER_STATE_DIR",
                str(home / ".local" / "state" / "rss-ingest"),
            )
        )
        data_repo_dir = Path(
            os.getenv(
                "RSS_DATA_REPO_DIR",
                str(home / ".local" / "share" / "rss-runtime-data"),
            )
        )
        return cls(
            data_repo=os.getenv("RSS_DATA_REPO", "acaiaishizhan/rss-runtime-data").strip(),
            data_repo_dir=data_repo_dir,
            action_repo=os.getenv("RSS_ACTION_REPO", "acaiaishizhan/rss-ingest-action").strip(),
            workflow_file=os.getenv("RSS_ACTION_WORKFLOW", "rss-ingest.yml").strip(),
            action_ref=os.getenv("RSS_ACTION_REF", "main").strip() or "main",
            state_path=state_dir / "publisher-state.json",
            log_path=state_dir / "publisher.log",
            sources=(
                SourceSpec(
                    "we-mp-rss",
                    os.getenv("WE_MP_RSS_FEED_URL", "http://127.0.0.1:8001/feed/all.rss").strip(),
                    "feeds/we-mp-rss.xml",
                ),
                SourceSpec("private-rss", private_feed.as_uri(), "feeds/private-rss.xml"),
            ),
            watch_paths=(
                private_feed,
                we_mp_db,
                Path(f"{we_mp_db}-wal"),
                Path(f"{we_mp_db}-shm"),
            ),
            poll_seconds=max(1.0, float(os.getenv("LOCAL_FEED_PUBLISHER_POLL_SECONDS", "5"))),
            settle_seconds=max(0.0, float(os.getenv("LOCAL_FEED_PUBLISHER_SETTLE_SECONDS", "90"))),
            gh_path=os.getenv("GH_PATH", str(home / ".local" / "bin" / "gh")).strip() or "gh",
            lock_path=state_dir / "publisher.lock",
        )


@dataclass
class SyncResult:
    changed: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    dispatched: bool = False


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def validate_feed_bytes(payload: bytes) -> int:
    if not payload or len(payload) > 50 * 1024 * 1024:
        raise FeedValidationError("feed is empty or exceeds 50 MiB")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise FeedValidationError(f"feed XML is invalid: {exc}") from exc
    root_name = _local_name(root.tag)
    if root_name not in {"rss", "feed", "rdf"}:
        raise FeedValidationError(f"unexpected feed root element: {root_name}")
    item_names = {"item"} if root_name in {"rss", "rdf"} else {"entry"}
    item_count = sum(1 for element in root.iter() if _local_name(element.tag) in item_names)
    if item_count <= 0:
        raise FeedValidationError("feed contains no items")
    return item_count


def watch_signature(paths: Iterable[Path]) -> Tuple[Tuple[str, int, int], ...]:
    signature = []
    for raw_path in paths:
        path = Path(raw_path)
        try:
            stat = path.stat()
            signature.append((str(path), stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            signature.append((str(path), -1, -1))
    return tuple(signature)


def _default_runner(args: Sequence[str], *, cwd=None, check=True):
    return subprocess.run(
        list(args),
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _default_source_reader(source: SourceSpec) -> bytes:
    parsed = urlparse(source.source)
    if parsed.scheme == "file":
        return Path(unquote(parsed.path)).read_bytes()
    request = urllib.request.Request(source.source, headers={"User-Agent": "rss-local-feed-publisher/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        if getattr(response, "status", 200) != 200:
            raise RuntimeError(f"HTTP {getattr(response, 'status', 'unknown')}")
        return response.read(50 * 1024 * 1024 + 1)


class LocalFeedPublisher:
    def __init__(
        self,
        config: PublisherConfig,
        *,
        runner: Callable = _default_runner,
        source_reader: Callable[[SourceSpec], bytes] = _default_source_reader,
    ) -> None:
        self.config = config
        self.runner = runner
        self.source_reader = source_reader

    def _run(self, args: Sequence[str], *, cwd=None):
        return self.runner(args, cwd=cwd, check=True)

    def _load_state(self) -> dict:
        try:
            payload = json.loads(self.config.state_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return {"push_pending": False, "dispatch_pending": False}

    def _save_state(self, state: dict) -> None:
        self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.config.state_path)

    def _ensure_checkout(self) -> None:
        if (self.config.data_repo_dir / ".git").is_dir():
            return
        self.config.data_repo_dir.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [self.config.gh_path, "repo", "clone", self.config.data_repo, str(self.config.data_repo_dir)]
        )

    def _push_pending(self, state: dict) -> None:
        self._run(["git", "push"], cwd=self.config.data_repo_dir)
        state["push_pending"] = False
        state["dispatch_pending"] = True
        self._save_state(state)

    def _dispatch_pending(self, state: dict) -> bool:
        self._run(
            [
                self.config.gh_path,
                "workflow",
                "run",
                self.config.workflow_file,
                "--repo",
                self.config.action_repo,
                "--ref",
                self.config.action_ref,
            ]
        )
        state["dispatch_pending"] = False
        self._save_state(state)
        return True

    def _target_path(self, source: SourceSpec) -> Path:
        relative = Path(source.target)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe data repository target: {source.target}")
        target = (self.config.data_repo_dir / relative).resolve()
        target.relative_to(self.config.data_repo_dir.resolve())
        return target

    def sync_once(self) -> SyncResult:
        result = SyncResult()
        self._ensure_checkout()
        state = self._load_state()

        if state.get("push_pending"):
            try:
                self._push_pending(state)
            except Exception as exc:
                result.errors["git-push"] = str(exc)
                self._save_state(state)
                return result
        else:
            try:
                self._run(["git", "pull", "--ff-only"], cwd=self.config.data_repo_dir)
            except Exception as exc:
                result.errors["git-pull"] = str(exc)
                return result

        changed_targets: List[Path] = []
        for source in self.config.sources:
            target = self._target_path(source)
            try:
                payload = self.source_reader(source)
                item_count = validate_feed_bytes(payload)
                if target.is_file() and target.read_bytes() == payload:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(f"{target.suffix}.tmp")
                temporary.write_bytes(payload)
                os.replace(temporary, target)
                changed_targets.append(target)
                result.changed.append(source.name)
                LOGGER.info("validated %s items=%s", source.name, item_count)
            except Exception as exc:
                result.errors[source.name] = str(exc)
                LOGGER.error("source %s rejected: %s", source.name, exc)

        if changed_targets:
            try:
                self._run(["git", "config", "user.name", "rss-local-feed-publisher"], cwd=self.config.data_repo_dir)
                self._run(
                    ["git", "config", "user.email", "rss-local-feed-publisher@users.noreply.github.com"],
                    cwd=self.config.data_repo_dir,
                )
                relative_targets = [str(path.relative_to(self.config.data_repo_dir)) for path in changed_targets]
                self._run(["git", "add", "--", *relative_targets], cwd=self.config.data_repo_dir)
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S %z")
                self._run(
                    ["git", "commit", "-m", f"data: update local RSS feeds ({timestamp})"],
                    cwd=self.config.data_repo_dir,
                )
                state["push_pending"] = True
                self._save_state(state)
                self._push_pending(state)
            except Exception as exc:
                result.errors["git-push"] = str(exc)
                self._save_state(state)

        if state.get("dispatch_pending"):
            try:
                result.dispatched = self._dispatch_pending(state)
            except Exception as exc:
                result.errors["workflow-dispatch"] = str(exc)
                self._save_state(state)
        return result

    def run_forever(self) -> int:
        retry_seconds = max(60.0, self.config.settle_seconds)
        result = self.sync_once()
        retry_at = time.monotonic() + retry_seconds if result.errors else None
        signature = watch_signature(self.config.watch_paths)
        publish_at = None
        while True:
            time.sleep(self.config.poll_seconds)
            current_signature = watch_signature(self.config.watch_paths)
            now = time.monotonic()
            if current_signature != signature:
                signature = current_signature
                publish_at = now + self.config.settle_seconds
            should_publish = publish_at is not None and now >= publish_at
            should_retry = retry_at is not None and now >= retry_at
            if not should_publish and not should_retry:
                continue
            result = self.sync_once()
            publish_at = None
            retry_at = now + retry_seconds if result.errors else None


def configure_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    LOGGER.handlers[:] = [file_handler, stream_handler]
    LOGGER.setLevel(logging.INFO)


def _acquire_lock(path: Path | None):
    if path is None:
        return None
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError("local feed publisher is already running")
    return handle


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="publish current snapshots and exit")
    args = parser.parse_args(argv)
    config = PublisherConfig.from_env()
    configure_logging(config.log_path)
    lock_handle = _acquire_lock(config.lock_path)
    try:
        publisher = LocalFeedPublisher(config)
        if args.once:
            result = publisher.sync_once()
            return 1 if result.errors else 0
        return publisher.run_forever()
    finally:
        if lock_handle is not None:
            lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
