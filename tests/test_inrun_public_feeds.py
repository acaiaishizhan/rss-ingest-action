import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest

from tools import inrun_public_feeds as feeds


NOW = dt.datetime(2026, 11, 2, 12, 14, tzinfo=dt.timezone.utc)
RSS = b"<rss><channel><item><guid>new</guid></item></channel></rss>"
HTML = b'<a class="blog-post-preview-content" href="/blog/a"><h2 class="blog-title-list-page">AI</h2></a>'


@pytest.fixture
def runtime(tmp_path):
    (tmp_path / "source-map.json").write_text('{"sources":{"private":"do-not-log"}}')
    for relative in feeds.TARGETS.values():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"old")
    return tmp_path


def test_empty_switch_is_zero_read_zero_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Disabled entry must do nothing")
    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(feeds, "load_producer", forbidden)
    assert feeds.refresh("unused", " , ") == []


def test_missing_checkout_map_never_fetches(tmp_path, monkeypatch):
    monkeypatch.setattr(feeds, "load_producer", Mock(side_effect=AssertionError("No checkout")))
    assert feeds.refresh(tmp_path, "prompthub-blog") == []
    assert list(tmp_path.iterdir()) == []


def test_allowlist_only_touches_selected_public_target(runtime):
    reader = Mock(return_value=HTML)
    results = feeds.refresh(runtime, " prompthub-blog,unknown,prompthub-blog ", now=NOW, mirror_reader=reader)
    assert results == [{"source": "prompthub-blog", "status": "success", "item_count": 1}]
    reader.assert_called_once()
    assert (runtime / feeds.TARGETS["prompthub-blog"]).read_bytes().startswith(b"<?xml")
    assert (runtime / feeds.TARGETS["sopilot"]).read_bytes() == b"old"
    assert (runtime / feeds.TARGETS["reddit-indiehackers-ai-revenue"]).read_bytes() == b"old"


@pytest.mark.parametrize("failure", [RuntimeError("private detail"), b"<rss>", b"<rss><channel/></rss>"])
def test_failure_preserves_old_source_and_other_source_continues(runtime, failure):
    def reader(source):
        if source.name == "prompthub-blog":
            if isinstance(failure, Exception):
                raise failure
            return failure
        return RSS
    results = feeds.refresh(runtime, ",".join(feeds.TARGETS), now=NOW.replace(minute=15), mirror_reader=reader)
    assert results[0]["status"] == "failed"
    assert results[1]["status"] == "success"
    assert (runtime / feeds.TARGETS["prompthub-blog"]).read_bytes() == b"old"
    assert (runtime / feeds.TARGETS["reddit-indiehackers-ai-revenue"]).read_bytes() == RSS
    feeds.refresh(runtime, "prompthub-blog", now=NOW, mirror_reader=lambda source: HTML)
    assert (runtime / feeds.TARGETS["prompthub-blog"]).read_bytes() != b"old"
    assert not list(runtime.rglob(".inrun-*"))


def test_replace_failure_preserves_old_and_cleans_temporary(runtime, monkeypatch):
    monkeypatch.setattr(feeds.os, "replace", Mock(side_effect=OSError("private path")))
    results = feeds.refresh(runtime, "prompthub-blog", now=NOW, mirror_reader=lambda source: HTML)
    assert results[0]["error_type"] == "OSError"
    assert (runtime / feeds.TARGETS["prompthub-blog"]).read_bytes() == b"old"
    assert not list(runtime.rglob(".inrun-*"))


def empty_sopilot(url):
    query = parse_qs(urlparse(url).query)
    props = {"range": "6h", "category": query["category"][0], "page": 1, "totalPages": 1,
             "risingTweets": [], "hotTweets": []}
    return "<script>self.__next_f.push(" + json.dumps([1, "1:" + json.dumps(props) + "\n"]) + ")</script>"


@pytest.mark.parametrize("minute, status", [(0, "success"), (14, "success"), (15, "skipped"), (59, "skipped")])
def test_sopilot_first_utc_quarter_and_valid_zero(runtime, minute, status):
    reader = Mock(side_effect=empty_sopilot)
    rows = feeds.refresh(runtime, "sopilot", now=NOW.replace(minute=minute), sopilot_reader=reader)
    assert rows[0]["status"] == status
    if status == "success":
        assert rows[0]["item_count"] == 0
        assert reader.call_count == 2
        assert (runtime / feeds.TARGETS["sopilot"]).read_bytes() != b"old"
    else:
        reader.assert_not_called()
        assert (runtime / feeds.TARGETS["sopilot"]).read_bytes() == b"old"


def test_sopilot_gate_converts_timezone_and_failure_retains_old(runtime):
    local = dt.datetime(2026, 11, 2, 12, 14, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    reader = Mock(side_effect=AssertionError("UTC minute is 44"))
    assert feeds.refresh(runtime, "sopilot", now=local, sopilot_reader=reader)[0]["status"] == "skipped"
    reader.assert_not_called()
    assert feeds.refresh(runtime, "sopilot", now=NOW, sopilot_reader=lambda url: "invalid")[0]["status"] == "failed"
    assert (runtime / feeds.TARGETS["sopilot"]).read_bytes() == b"old"


def test_cli_only_logs_public_summary(runtime, monkeypatch, capsys):
    mirrors = feeds.load_producer("public-mirrors-frozen.py")
    monkeypatch.setattr(mirrors, "_default_source_reader", Mock(side_effect=RuntimeError("SECRET private path")))
    monkeypatch.setenv("INRUN_FEEDS", "prompthub-blog")
    assert feeds.main(["--runtime-dir", str(runtime)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "source": "prompthub-blog", "status": "failed", "error_type": "RuntimeError"}


@pytest.mark.parametrize("lane,batch,event,preflight,outcome,enabled,expected", [
    ("rss", "", "workflow_dispatch", False, "success", "prompthub-blog", True),
    ("grok", "", "workflow_call", False, "success", "prompthub-blog", False),
    ("rss", "batch", "workflow_call", False, "success", "sopilot", False),
    ("rss", "", "workflow_dispatch", True, "success", "sopilot", False),
    ("rss", "", "workflow_dispatch", False, "failure", "sopilot", False),
    ("rss", "", "workflow_dispatch", False, "success", "", False),
])
def test_real_workflow_gate(lane, batch, event, preflight, outcome, enabled, expected):
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/rss-ingest.yml").read_text()
    step = workflow.split("      - name: Refresh public feeds in runner checkout\n")[1].split("      - name:")[0]
    expression = step.split("if: >-\n")[1].split("        continue-on-error:")[0].strip()
    for token, value in {"vars.INRUN_FEEDS": enabled, "steps.runtime_data.outcome": outcome,
                         "inputs.ingest_lane": lane, "inputs.sopilot_batch_id": batch,
                         "github.event_name": event, "inputs.preflight_only": preflight}.items():
        expression = expression.replace(token, repr(value))
    expression = re.sub(r"\btrue\b", "True", expression).replace("&&", "and").replace("||", "or")
    assert eval(" ".join(expression.split()), {"__builtins__": {}}) is expected
    assert workflow.index("Checkout private RSS snapshots") < workflow.index("Refresh public feeds") < workflow.index("Run RSS ingest")
    assert "continue-on-error: true" in step
    assert "timeout-minutes: 2" in step
    assert "upload-artifact" not in step


def run_cli(runtime, reader_code):
    root = Path(__file__).resolve().parents[1]
    code = f"""
import datetime as dt
import runpy
import socket
import sys
sys.path.insert(0, {str(root)!r})
def denied(*args, **kwargs):
    raise AssertionError('Network forbidden')
socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
from tools import inrun_public_feeds as feeds
mirrors = feeds.load_producer('public-mirrors-frozen.py')
sopilot = feeds.load_producer('sopilot_watch.py')
class Clock(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 2, 12, 14, tzinfo=dt.timezone.utc)
dt.datetime = Clock
{reader_code}
sys.argv = ['inrun_public_feeds.py', '--runtime-dir', {str(runtime)!r}]
runpy.run_path({str(root / 'tools/inrun_public_feeds.py')!r}, run_name='__main__')
"""
    env = dict(os.environ, INRUN_FEEDS=",".join(feeds.TARGETS), INRUN_DEADLINE_SECONDS="1",
               RSS_INGEST_SKIP_LOCAL_ENV="true")
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=2)


def test_cli_hard_deadline_never_writes_late_candidate(runtime):
    for parent in {runtime / Path(target).parent for target in feeds.TARGETS.values()}:
        (parent / ".inrun-leftover").write_bytes(b"unfinished")
    started = time.monotonic()
    process = run_cli(runtime, f"""
import time
def reader(source):
    if source.name == 'prompthub-blog':
        time.sleep(5)
        return {HTML!r}
    return {RSS!r}
mirrors._default_source_reader = reader
def empty(url):
    import json
    from urllib.parse import parse_qs, urlparse
    category = parse_qs(urlparse(url).query)['category'][0]
    props = {{'range':'6h','category':category,'page':1,'totalPages':1,'risingTweets':[],'hotTweets':[]}}
    return '<script>self.__next_f.push(' + json.dumps([1, '1:' + json.dumps(props) + '\\n']) + ')</script>'
sopilot._fetch_text = empty
""")
    elapsed = time.monotonic() - started
    assert elapsed < 2
    assert process.returncode == 0 and process.stderr == ""
    rows = {row["source"]: row for row in map(json.loads, process.stdout.splitlines())}
    assert rows["prompthub-blog"]["status"] == "timeout"
    assert rows["reddit-indiehackers-ai-revenue"]["status"] == "success"
    assert rows["sopilot"]["status"] == "success"
    assert (runtime / feeds.TARGETS["reddit-indiehackers-ai-revenue"]).read_bytes() == RSS
    assert (runtime / feeds.TARGETS["sopilot"]).read_bytes() != b"old"
    time.sleep(max(0, 5.2 - (time.monotonic() - started)))
    assert (runtime / feeds.TARGETS["prompthub-blog"]).read_bytes() == b"old"
    assert not list(runtime.rglob(".inrun-*"))
    print(f"deadline subprocess exited in {elapsed:.3f}s; late target unchanged after 5.2s")


def test_cli_all_sources_fail_without_blocking_ingest(runtime):
    process = run_cli(runtime, """
def fail(*args):
    raise RuntimeError('private detail must not escape')
mirrors._default_source_reader = fail
sopilot._fetch_text = fail
""")
    assert process.returncode == 0 and process.stderr == ""
    rows = list(map(json.loads, process.stdout.splitlines()))
    assert len(rows) == 3 and all(row["status"] == "failed" for row in rows)
    assert "private detail" not in process.stdout
    assert all((runtime / target).read_bytes() == b"old" for target in feeds.TARGETS.values())
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/rss-ingest.yml").read_text()
    step = workflow.split("      - name: Refresh public feeds in runner checkout\n")[1].split("      - name:")[0]
    assert "continue-on-error: true" in step and "timeout-minutes: 2" in step
