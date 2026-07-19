import json
from pathlib import Path

import pytest

from tools.github_runtime_preflight import REQUIRED_ENV, validate_runtime


RSS = b"""<?xml version="1.0"?><rss><channel><item><guid>1</guid></item></channel></rss>"""


def _env() -> dict[str, str]:
    return {name: "configured" for name in REQUIRED_ENV}


def _source_map(tmp_path: Path, target: str = "feeds/private.xml") -> Path:
    mapping = tmp_path / "source-map.json"
    mapping.write_text(
        json.dumps({"version": 1, "sources": {"record": target}}),
        encoding="utf-8",
    )
    return mapping


def _keyword_snapshot(tmp_path: Path, count: int = 1000) -> Path:
    snapshot = tmp_path / "keyword_snapshot.json"
    snapshot.write_text(
        json.dumps({"schema_version": 2, "entries": [{} for _ in range(count)]}),
        encoding="utf-8",
    )
    return snapshot


def test_validate_runtime_accepts_valid_private_feed(tmp_path: Path) -> None:
    feed = tmp_path / "feeds/private.xml"
    feed.parent.mkdir()
    feed.write_bytes(RSS)

    assert validate_runtime(_source_map(tmp_path), _env(), _keyword_snapshot(tmp_path)) == (1, 1, 1000)


def test_validate_runtime_reports_missing_secret_names_only(tmp_path: Path) -> None:
    feed = tmp_path / "feeds/private.xml"
    feed.parent.mkdir()
    feed.write_bytes(RSS)
    env = _env()
    env["ARK_API_KEY"] = ""

    with pytest.raises(RuntimeError, match="ARK_API_KEY"):
        validate_runtime(_source_map(tmp_path), env, _keyword_snapshot(tmp_path))


def test_validate_runtime_rejects_path_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.xml"
    outside.write_bytes(RSS)

    with pytest.raises(RuntimeError, match="stay inside"):
        validate_runtime(_source_map(tmp_path, "../outside.xml"), _env(), _keyword_snapshot(tmp_path))


def test_validate_runtime_rejects_invalid_feed(tmp_path: Path) -> None:
    feed = tmp_path / "feeds/private.xml"
    feed.parent.mkdir()
    feed.write_text("<html>login</html>", encoding="utf-8")

    with pytest.raises(ValueError, match="unexpected feed root"):
        validate_runtime(_source_map(tmp_path), _env(), _keyword_snapshot(tmp_path))


def test_validate_runtime_rejects_missing_keyword_snapshot(tmp_path: Path) -> None:
    feed = tmp_path / "feeds/private.xml"
    feed.parent.mkdir()
    feed.write_bytes(RSS)

    with pytest.raises(RuntimeError, match="keyword snapshot is unavailable"):
        validate_runtime(_source_map(tmp_path), _env(), tmp_path / "missing.json")
