# -*- coding: utf-8 -*-
"""Publish local RSS snapshots through the authenticated WSL GitHub CLI."""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Sequence, Tuple
from urllib.parse import unquote, urljoin, urlparse


LOGGER = logging.getLogger("local-feed-publisher")

GROK_FEED_KEYS = (
    "deals",
    "rumors",
    "cases",
    "burst",
    "tips",
    "peers",
    "resources",
    "codex",
    "claude",
)

SUBSTACK_MIRROR_FEEDS = (
    ("prompthub-blog", "https://www.prompthub.us/blog", "feeds/public-mirror/prompthub-blog.xml"),
    ("substack-a16z", "https://a16z.substack.com/feed", "feeds/public-mirror/a16z-substack.xml"),
    (
        "substack-department-of-product",
        "https://departmentofproduct.substack.com/feed",
        "feeds/public-mirror/departmentofproduct-substack.xml",
    ),
    ("substack-ai-models", "https://aimodels.substack.com/feed", "feeds/public-mirror/aimodels-substack.xml"),
    ("substack-the-sequence", "https://thesequence.substack.com/feed", "feeds/public-mirror/thesequence-substack.xml"),
    ("substack-gary-marcus", "https://garymarcus.substack.com/feed", "feeds/public-mirror/garymarcus-substack.xml"),
    (
        "substack-micro-saas-idea",
        "https://microsaasidea.substack.com/feed",
        "feeds/public-mirror/microsaasidea-substack.xml",
    ),
)

REDDIT_MIRROR_FEEDS = (
    (
        "reddit-indiehackers-ai-revenue",
        "https://www.reddit.com/r/indiehackers/search.rss?q=AI%20revenue&restrict_sr=1&sort=new",
        "feeds/public-mirror/reddit-indiehackers-ai-revenue.xml",
    ),
    (
        "reddit-sideproject-ai-revenue",
        "https://www.reddit.com/r/SideProject/search.rss?q=AI%20revenue&restrict_sr=1&sort=new",
        "feeds/public-mirror/reddit-sideproject-ai-revenue.xml",
    ),
)

SUBSTACK_FEED_TARGETS = {
    source_url: target
    for _, source_url, target in SUBSTACK_MIRROR_FEEDS
}
# The Feishu source record still carries the retired Substack URL.  Runtime
# routing keeps that stable record id while the publisher now mirrors the
# official PromptHub blog.
SUBSTACK_FEED_TARGETS.pop("https://www.prompthub.us/blog", None)
SUBSTACK_FEED_TARGETS["https://prompthub.substack.com/feed"] = (
    "feeds/public-mirror/prompthub-blog.xml"
)
REDDIT_FEED_TARGETS = {
    source_url: target
    for _, source_url, target in REDDIT_MIRROR_FEEDS
}


class FeedValidationError(ValueError):
    """Raised when a candidate feed must not replace the last good snapshot."""


class FeedNotReadyError(FeedValidationError):
    """Raised when a valid feed is still being populated by its producer."""


@dataclass(frozen=True)
class SourceSpec:
    name: str
    source: str
    target: str
    soft_fail: bool = False
    kind: str = "feed"


def _path_as_file_uri(path: Path) -> str:
    try:
        return path.as_uri()
    except ValueError:
        # Windows pathlib treats a WSL path such as /mnt/f/... as drive-relative.
        text = path.as_posix()
        if text.startswith("/"):
            return f"file://{text}"
        raise


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
    dispatch_enabled: bool = True
    squash_snapshots: bool = True

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
        grok_feed_dir = Path(
            os.getenv(
                "GROK_RSS_SNAPSHOT_DIR",
                "/mnt/f/coding/rss-ingest-local/data/grok-feeds",
            )
        )
        grok_feed_paths = tuple(grok_feed_dir / f"{key}.xml" for key in GROK_FEED_KEYS)
        keyword_snapshot = Path(
            os.getenv(
                "KEYWORD_SNAPSHOT_PUBLISH_PATH",
                "/mnt/f/coding/rss-ingest-local/data/keyword_snapshot.json",
            )
        )
        grok_sources = tuple(
            SourceSpec(
                f"grok-{key}",
                _path_as_file_uri(path),
                f"feeds/grok/{key}.xml",
            )
            for key, path in zip(GROK_FEED_KEYS, grok_feed_paths)
        )
        substack_sources = tuple(
            SourceSpec(name, source_url, target, soft_fail=True)
            for name, source_url, target in SUBSTACK_MIRROR_FEEDS
        )
        reddit_sources = tuple(
            SourceSpec(name, source_url, target, soft_fail=True)
            for name, source_url, target in REDDIT_MIRROR_FEEDS
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
                SourceSpec("private-rss", _path_as_file_uri(private_feed), "feeds/private-rss.xml"),
                *grok_sources,
                *substack_sources,
                *reddit_sources,
                SourceSpec(
                    "keyword-snapshot",
                    _path_as_file_uri(keyword_snapshot),
                    "keyword_snapshot.json",
                    kind="json",
                ),
            ),
            watch_paths=(
                private_feed,
                we_mp_db,
                Path(f"{we_mp_db}-wal"),
                Path(f"{we_mp_db}-shm"),
                *grok_feed_paths,
                keyword_snapshot,
            ),
            poll_seconds=max(1.0, float(os.getenv("LOCAL_FEED_PUBLISHER_POLL_SECONDS", "5"))),
            settle_seconds=max(0.0, float(os.getenv("LOCAL_FEED_PUBLISHER_SETTLE_SECONDS", "90"))),
            gh_path=os.getenv("GH_PATH", str(home / ".local" / "bin" / "gh")).strip() or "gh",
            lock_path=state_dir / "publisher.lock",
            dispatch_enabled=os.getenv("RSS_ACTION_DISPATCH_ENABLED", "true").lower()
            in {"1", "true", "yes", "y"},
            squash_snapshots=os.getenv("RSS_DATA_SQUASH_SNAPSHOTS", "true").lower()
            in {"1", "true", "yes", "y"},
        )


@dataclass
class SyncResult:
    changed: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    deferred: Dict[str, str] = field(default_factory=dict)
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


def validate_we_mp_feed_content(payload: bytes) -> int:
    """Reject the transient state where WeChat items exist before their body is ready."""

    item_count = validate_feed_bytes(payload)
    root = ET.fromstring(payload)
    incomplete = 0
    for element in root.iter():
        if _local_name(element.tag) != "item":
            continue
        encoded = next(
            (child for child in list(element) if _local_name(child.tag) == "encoded"),
            None,
        )
        body = "" if encoded is None else "".join(encoded.itertext()).strip()
        if not body:
            incomplete += 1
    if incomplete:
        raise FeedNotReadyError(
            f"feed body is not ready for {incomplete}/{item_count} items"
        )
    return item_count


def sanitize_we_mp_feed_content(
    payload: bytes,
    *,
    now: dt.datetime | None = None,
    grace_seconds: int = 3600,
) -> tuple[bytes, int]:
    """Wait for fresh bodies, but stop one permanently empty item blocking the feed."""

    validate_feed_bytes(payload)
    root = ET.fromstring(payload)
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    deferred = 0
    dropped = 0
    for parent in root.iter():
        for element in list(parent):
            if _local_name(element.tag) != "item":
                continue
            encoded = next(
                (child for child in list(element) if _local_name(child.tag) == "encoded"),
                None,
            )
            body = "" if encoded is None else "".join(encoded.itertext()).strip()
            if body:
                continue
            raw_date = ""
            for child in list(element):
                if _local_name(child.tag) == "pubdate":
                    raw_date = "".join(child.itertext()).strip()
                    break
            try:
                published = email.utils.parsedate_to_datetime(raw_date)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=dt.timezone.utc)
                age_seconds = (current - published).total_seconds()
            except (TypeError, ValueError, OverflowError):
                age_seconds = 0
            if age_seconds < max(0, grace_seconds):
                deferred += 1
                continue
            parent.remove(element)
            dropped += 1
    if deferred:
        item_count = validate_feed_bytes(payload)
        raise FeedNotReadyError(f"feed body is not ready for {deferred}/{item_count} fresh items")
    if dropped:
        sanitized = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        validate_feed_bytes(sanitized)
        return sanitized, dropped
    return payload, 0


def validate_keyword_snapshot_bytes(payload: bytes, min_entries: int = 1000) -> int:
    data = json.loads(payload.decode("utf-8-sig"))
    entries = data.get("entries") if isinstance(data, dict) else None
    if int((data or {}).get("schema_version") or 0) < 2 or not isinstance(entries, list):
        raise FeedValidationError("keyword snapshot must use schema v2 with entries")
    if len(entries) < min_entries:
        raise FeedValidationError(f"keyword snapshot contains only {len(entries)} entries")
    return len(entries)


class _PromptHubBlogParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: List[Dict[str, str]] = []
        self.current: Dict[str, str] | None = None
        self.capture = ""
        self.capture_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        classes = set(str(values.get("class") or "").split())
        if tag == "a" and "blog-post-preview-content" in classes:
            self.current = {"href": str(values.get("href") or ""), "title": "", "date": ""}
        elif self.current is not None and tag == "h2" and "blog-title-list-page" in classes:
            self.capture = "title"
            self.capture_parts = []
        elif self.current is not None and tag == "p" and "blog-date" in classes:
            self.capture = "date"
            self.capture_parts = []

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.capture_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.current is not None and self.capture and tag in {"h2", "p"}:
            self.current[self.capture] = " ".join("".join(self.capture_parts).split())
            self.capture = ""
            self.capture_parts = []
        if tag == "a" and self.current is not None:
            if self.current.get("href") and self.current.get("title"):
                self.posts.append(self.current)
            self.current = None
            self.capture = ""
            self.capture_parts = []


def build_prompthub_blog_feed(payload: bytes, source_url: str) -> bytes:
    parser = _PromptHubBlogParser()
    parser.feed(payload.decode("utf-8", errors="replace"))
    seen = set()
    posts = []
    for post in parser.posts:
        link = urljoin(source_url, post["href"])
        if link in seen:
            continue
        seen.add(link)
        posts.append({**post, "link": link})
    if not posts:
        raise FeedValidationError("PromptHub blog page contains no posts")
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "PromptHub Blog"
    ET.SubElement(channel, "link").text = source_url
    ET.SubElement(channel, "description").text = "PromptHub product updates and prompt engineering guides"
    for post in posts[:50]:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = html.unescape(post["title"])
        ET.SubElement(item, "link").text = post["link"]
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = post["link"]
        try:
            published = dt.datetime.strptime(post.get("date", "").strip(), "%B %d, %Y").replace(
                tzinfo=dt.timezone.utc
            )
            ET.SubElement(item, "pubDate").text = email.utils.format_datetime(published)
        except ValueError:
            pass
    result = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    validate_feed_bytes(result)
    return result


def feed_fingerprint(payload: bytes) -> str:
    """Hash item/entry content while ignoring volatile feed-level metadata."""

    validate_feed_bytes(payload)
    root = ET.fromstring(payload)
    root_name = _local_name(root.tag)
    item_names = {"item"} if root_name in {"rss", "rdf"} else {"entry"}
    digest = hashlib.sha256()
    for element in root.iter():
        if _local_name(element.tag) not in item_names:
            continue
        digest.update(ET.tostring(element, encoding="utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


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
    request = urllib.request.Request(
        source.source,
        headers={"User-Agent": "Mozilla/5.0 (compatible; rss-local-feed-publisher/1.0)"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if getattr(response, "status", 200) != 200:
                    raise RuntimeError(f"HTTP {getattr(response, 'status', 'unknown')}")
                return response.read(50 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                raise
            if attempt >= 2:
                raise
            retry_after = str(exc.headers.get("Retry-After") or "").strip()
            delay = float(retry_after) if retry_after.isdigit() else float(10 * (attempt + 1))
            time.sleep(min(30.0, max(1.0, delay)))
    raise RuntimeError(f"remote source retries exhausted: {source.name}")


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
        return {"push_pending": False, "push_force": False, "dispatch_pending": False}

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
        args = ["git", "push"]
        if state.get("push_force"):
            args.append("--force-with-lease")
        self._run(args, cwd=self.config.data_repo_dir)
        state["push_pending"] = False
        state["push_force"] = False
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
                if source.name == "prompthub-blog":
                    payload = build_prompthub_blog_feed(payload, source.source)
                if source.kind == "json":
                    item_count = validate_keyword_snapshot_bytes(payload)
                else:
                    item_count = validate_feed_bytes(payload)
                if source.name == "we-mp-rss":
                    payload, dropped = sanitize_we_mp_feed_content(payload)
                    item_count = validate_feed_bytes(payload)
                    if dropped:
                        LOGGER.warning("source we-mp-rss dropped stale empty-body items=%s", dropped)
                if target.is_file():
                    existing = target.read_bytes()
                    if existing == payload:
                        continue
                    try:
                        if feed_fingerprint(existing) == feed_fingerprint(payload):
                            LOGGER.info("unchanged items %s; ignored feed metadata update", source.name)
                            continue
                    except FeedValidationError:
                        pass
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(f"{target.suffix}.tmp")
                temporary.write_bytes(payload)
                os.replace(temporary, target)
                changed_targets.append(target)
                result.changed.append(source.name)
                LOGGER.info("validated %s items=%s", source.name, item_count)
            except FeedNotReadyError as exc:
                result.deferred[source.name] = str(exc)
                LOGGER.warning("source %s deferred: %s", source.name, exc)
            except Exception as exc:
                if source.soft_fail:
                    result.deferred[source.name] = str(exc)
                    LOGGER.warning("source %s deferred; keeping last good snapshot: %s", source.name, exc)
                else:
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
                head_subject = self._run(
                    ["git", "log", "-1", "--format=%s"],
                    cwd=self.config.data_repo_dir,
                ).stdout.strip()
                amend_snapshot = self.config.squash_snapshots and head_subject.startswith(
                    "data: update local RSS feeds"
                )
                commit_args = ["git", "commit"]
                if amend_snapshot:
                    commit_args.append("--amend")
                commit_args.extend(["-m", f"data: update local RSS feeds ({timestamp})"])
                self._run(
                    commit_args,
                    cwd=self.config.data_repo_dir,
                )
                state["push_pending"] = True
                state["push_force"] = amend_snapshot
                self._save_state(state)
                self._push_pending(state)
            except Exception as exc:
                result.errors["git-push"] = str(exc)
                self._save_state(state)

        if state.get("dispatch_pending") and self.config.dispatch_enabled:
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
    parser.add_argument("--no-dispatch", action="store_true", help="push snapshots without triggering the Action")
    args = parser.parse_args(argv)
    config = PublisherConfig.from_env()
    if args.no_dispatch:
        config = replace(config, dispatch_enabled=False)
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
