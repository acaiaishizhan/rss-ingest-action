import json
import os
import socket
import sys
from pathlib import Path

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import grok_watch as gw

HOUR_MS = 3600 * 1000


def _topic(**kw):
    base = {
        "key": "deals", "name": "AI羊毛", "interval_hours": 2, "window_hours": 48,
        "prompt_file": "docs/local-grok-prompts/deals.md", "title_prefix": "", "enabled": True,
    }
    base.update(kw)
    return base


def test_load_topics_skips_disabled_and_requires_fields(tmp_path):
    path = tmp_path / "topics.json"
    path.write_text(json.dumps([
        _topic(),
        _topic(key="off", enabled=False),
        {"key": "broken"},
    ]), encoding="utf-8")

    topics = gw.load_topics(path)

    assert [t["key"] for t in topics] == ["deals"]


def test_load_state_returns_default_on_missing_or_corrupt(tmp_path):
    missing = gw.load_state(tmp_path / "nope.json")
    assert missing == {"topics": {}, "seen_posts": {}, "seen_text": {}}

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert gw.load_state(bad) == {"topics": {}, "seen_posts": {}, "seen_text": {}}


def test_save_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    state = {"topics": {"deals": {"last_run_ms": 123}}, "seen_posts": {"1": 1}, "seen_text": {}}
    gw.save_state(path, state)
    assert gw.load_state(path) == state


def test_topic_due_respects_interval():
    topic = _topic(interval_hours=2)
    now = 100 * HOUR_MS
    assert gw.topic_due(topic, {"topics": {}}, now) is True
    fresh = {"topics": {"deals": {"last_run_ms": now - 1 * HOUR_MS}}}
    assert gw.topic_due(topic, fresh, now) is False
    stale = {"topics": {"deals": {"last_run_ms": now - 3 * HOUR_MS}}}
    assert gw.topic_due(topic, stale, now) is True


def test_select_due_topics_caps_and_prioritizes_most_overdue():
    topics = [
        _topic(key="fresh", interval_hours=2),
        _topic(key="oldest", interval_hours=2),
        _topic(key="middle", interval_hours=2),
    ]
    now = 100 * HOUR_MS
    state = {
        "topics": {
            "fresh": {"last_run_ms": now - 3 * HOUR_MS},
            "oldest": {"last_run_ms": now - 8 * HOUR_MS},
            "middle": {"last_run_ms": now - 5 * HOUR_MS},
        },
        "seen_posts": {},
        "seen_text": {},
    }

    due = gw.select_due_topics(topics, state, now, max_topics=2)

    assert [topic["key"] for topic in due] == ["oldest", "middle"]


def test_select_due_topics_force_ignores_cap():
    topics = [_topic(key="t1"), _topic(key="t2"), _topic(key="t3")]

    due = gw.select_due_topics(topics, gw.default_state(), 100 * HOUR_MS, force=True, max_topics=1)

    assert [topic["key"] for topic in due] == ["t1", "t2", "t3"]


def test_topic_due_schedule_hour_uses_local_hour_phase():
    import datetime as dt
    import time as _time

    topic = _topic(interval_hours=4, schedule_hour=2)
    due_at = int(_time.mktime(dt.datetime(2026, 6, 22, 18, 18, 0).timetuple()) * 1000)
    off_phase = int(_time.mktime(dt.datetime(2026, 6, 22, 17, 18, 0).timetuple()) * 1000)

    assert gw.topic_due(topic, gw.default_state(), due_at) is True
    assert gw.topic_due(topic, gw.default_state(), off_phase) is False


def test_topic_due_schedule_hour_does_not_rerun_same_hour():
    import datetime as dt
    import time as _time

    topic = _topic(interval_hours=6, schedule_hour=4)
    now = int(_time.mktime(dt.datetime(2026, 6, 22, 16, 18, 0).timetuple()) * 1000)
    already_ran = int(_time.mktime(dt.datetime(2026, 6, 22, 16, 10, 0).timetuple()) * 1000)
    state = {"topics": {"deals": {"last_run_ms": already_ran}}, "seen_posts": {}, "seen_text": {}}

    assert gw.topic_due(topic, state, now) is False


def test_topic_due_schedule_times_respects_minute_slot():
    import datetime as dt
    import time as _time

    topic = _topic(schedule_times=["10:23", "10:43"])
    due_at = int(_time.mktime(dt.datetime(2026, 6, 22, 10, 24, 0).timetuple()) * 1000)
    off_slot = int(_time.mktime(dt.datetime(2026, 6, 22, 10, 3, 0).timetuple()) * 1000)
    already_ran = int(_time.mktime(dt.datetime(2026, 6, 22, 10, 23, 0).timetuple()) * 1000)

    assert gw.topic_due(topic, gw.default_state(), due_at) is True
    assert gw.topic_due(topic, gw.default_state(), off_slot) is False
    state = {"topics": {"deals": {"last_run_ms": already_ran}}, "seen_posts": {}, "seen_text": {}}
    assert gw.topic_due(topic, state, due_at) is False


def test_extract_items_strips_code_fences():
    text = '```json\n[{"title": "t", "url": "https://x.com/a/status/1"}]\n```'
    items = gw.extract_items(text)
    assert len(items) == 1 and items[0]["title"] == "t"


def test_extract_items_handles_bare_array_and_garbage():
    assert gw.extract_items('[{"title": "t"}]')[0]["title"] == "t"
    assert gw.extract_items("") == []
    assert gw.extract_items("搜索不可用，原因是……") == []
    assert gw.extract_items('{"error": "search unavailable"}') == []


def test_extract_items_filters_non_dict_elements():
    assert gw.extract_items('[{"title": "t"}, "junk", 3]') == [{"title": "t"}]


def test_canonical_status_parses_x_twitter_and_rejects_others():
    assert gw.canonical_status("https://x.com/foo/status/123456") == ("123456", "foo")
    assert gw.canonical_status("https://twitter.com/foo/status/123456?s=20") == ("123456", "foo")
    assert gw.canonical_status("https://www.x.com/i/web/status/9") == ("9", "i")
    assert gw.canonical_status("https://example.com/foo/status/1") is None
    assert gw.canonical_status("not a url") is None


def test_canonical_reddit_parses_posts_and_comments():
    assert gw.canonical_reddit("https://www.reddit.com/r/codex/comments/1abcxyz/title/") == ("1abcxyz", "codex", "")
    assert gw.canonical_reddit("https://old.reddit.com/r/codex/comments/1abcxyz/title/k9comment/") == ("1abcxyz:k9comment", "codex", "k9comment")
    assert gw.canonical_reddit("https://reddit.com/r/codex/comments/1abcxyz/title/?comment=k9comment") == ("1abcxyz:k9comment", "codex", "k9comment")
    assert gw.canonical_reddit("https://example.com/r/codex/comments/1abcxyz/title/") is None


def test_text_hash_ignores_case_spacing_and_width():
    a = gw.text_hash("Free Credits！ 快来领取")
    b = gw.text_hash("free  credits！快来 领取")
    assert a == b
    assert a != gw.text_hash("完全不同的内容")


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_fxtwitter_lookup_extracts_fields(monkeypatch):
    payload = {
        "code": 200,
        "tweet": {
            "author": {"screen_name": "RealGuy", "followers": 1234},
            "created_timestamp": 1781234567,
            "likes": 5, "views": 340,
            "text": "first time I made money with AI",
        },
    }
    monkeypatch.setattr(gw.requests, "get", lambda url, timeout, headers: _FakeResp(200, payload))

    tweet = gw.fxtwitter_lookup("123", "wronghandle")

    assert tweet == {
        "platform": "x", "source_id": "123",
        "author": "RealGuy", "followers": 1234,
        "created_ms": 1781234567000, "likes": 5, "views": 340,
        "text": "first time I made money with AI",
    }


def test_fxtwitter_lookup_returns_none_on_404_or_error(monkeypatch):
    monkeypatch.setattr(gw.time, "sleep", lambda s: None)
    monkeypatch.setattr(gw.requests, "get", lambda url, timeout, headers: _FakeResp(404, {"code": 404}))
    assert gw.fxtwitter_lookup("123", "a") is None

    def _boom(url, timeout, headers):
        raise OSError("network down")

    monkeypatch.setattr(gw.requests, "get", _boom)
    assert gw.fxtwitter_lookup("123", "a") is None


def test_fxtwitter_lookup_retries_once_on_transient_error(monkeypatch):
    monkeypatch.setattr(gw.time, "sleep", lambda s: None)
    payload = {
        "code": 200,
        "tweet": {"author": {"screen_name": "A", "followers": 1}, "created_timestamp": 1,
                  "likes": 0, "views": 0, "text": "t"},
    }
    calls = []

    def flaky(url, timeout, headers):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("SSL EOF")
        return _FakeResp(200, payload)

    monkeypatch.setattr(gw.requests, "get", flaky)

    tweet = gw.fxtwitter_lookup("123", "a")

    assert len(calls) == 2
    assert tweet is not None and tweet["author"] == "A"


def test_reddit_lookup_extracts_post_and_comment(monkeypatch):
    payload = [
        {
            "data": {
                "children": [
                    {"data": {
                        "id": "1abcxyz", "title": "Codex workflow", "selftext": "post body",
                        "author": "op", "subreddit": "codex", "created_utc": 1781234000,
                        "score": 12, "permalink": "/r/codex/comments/1abcxyz/title/",
                        "url_overridden_by_dest": "https://i.redd.it/direct.jpg",
                        "preview": {
                            "images": [
                                {"source": {"url": "https://preview.redd.it/preview.jpg?width=960&amp;format=pjpg"}}
                            ]
                        },
                        "gallery_data": {"items": [{"media_id": "abc"}]},
                        "media_metadata": {
                            "abc": {"status": "valid", "s": {"u": "https://preview.redd.it/gallery.png?width=800&amp;format=png"}}
                        },
                    }}
                ]
            }
        },
        {
            "data": {
                "children": [
                    {"kind": "t1", "data": {
                        "id": "k9comment", "body": "use /resume after switching desktop and CLI",
                        "author": "helper", "subreddit": "codex", "created_utc": 1781234567,
                        "score": 5, "permalink": "/r/codex/comments/1abcxyz/title/k9comment/",
                    }}
                ]
            }
        },
    ]
    monkeypatch.setattr(gw.requests, "get", lambda url, timeout, headers: _FakeResp(200, payload))

    post = gw.reddit_lookup("https://www.reddit.com/r/codex/comments/1abcxyz/title/")
    comment = gw.reddit_lookup("https://www.reddit.com/r/codex/comments/1abcxyz/title/k9comment/")

    assert post["source_id"] == "reddit:1abcxyz"
    assert post["author"] == "op"
    assert "Codex workflow" in post["text"]
    assert post["image_urls"] == [
        "https://i.redd.it/direct.jpg",
        "https://preview.redd.it/preview.jpg?width=960&format=pjpg",
        "https://preview.redd.it/gallery.png?width=800&format=png",
    ]
    assert comment["source_id"] == "reddit:1abcxyz:k9comment"
    assert comment["author"] == "helper"
    assert comment["created_ms"] == 1781234567000


DAY_MS = 24 * HOUR_MS


def _tweet(**kw):
    base = {"author": "a", "followers": 5000, "created_ms": 99 * HOUR_MS, "likes": 10, "views": 500, "text": "hello"}
    base.update(kw)
    return base


def test_hard_filter_passes_normal_item():
    now = 100 * HOUR_MS
    assert gw.hard_filter({"category": "case"}, _tweet(), now, window_hours=48) is None


def test_hard_filter_drops_stale_and_missing_timestamp():
    now = 100 * HOUR_MS
    assert gw.hard_filter({"category": "case"}, _tweet(created_ms=now - 49 * HOUR_MS), now, 48) == "stale"
    assert gw.hard_filter({"category": "case"}, _tweet(created_ms=0), now, 48) == "no_timestamp"


def test_hard_filter_drops_low_cred_deal_without_official_url():
    now = 100 * HOUR_MS
    shill = _tweet(followers=79, views=29)
    assert gw.hard_filter({"category": "deal", "official_url": ""}, shill, now, 48) == "low_cred_deal"
    assert gw.hard_filter({"category": "deal", "official_url": "https://b.ai"}, shill, now, 48) is None
    assert gw.hard_filter({"category": "case"}, shill, now, 48) is None
    assert gw.hard_filter({"category": "deal", "official_url": ""}, _tweet(followers=79, views=600), now, 48) is None
    assert gw.hard_filter({"category": "deal", "official_url": ""}, _tweet(platform="reddit", followers=0, views=0), now, 48) is None


import feedparser


def _feed_item(**kw):
    base = {
        "status_id": "123456",
        "link": "https://x.com/RealGuy/status/123456",
        "title": "AI 视频接单一晚赚 $250",
        "created_ms": 1781234567000,
        "author": "RealGuy",
        "description": "原帖全文……\n\n[Grok摘要] 按秒收费。",
    }
    base.update(kw)
    return base


def test_build_feed_xml_roundtrips_through_feedparser():
    topic = _topic(title_prefix="[未核实] ")
    xml = gw.build_feed_xml(topic, [_feed_item()], 1781300000000)

    feed = feedparser.parse(xml)

    assert not feed.bozo
    entry = feed.entries[0]
    assert entry["title"] == "[未核实] AI 视频接单一晚赚 $250"
    assert entry["link"] == "https://x.com/RealGuy/status/123456"
    assert entry["id"] == "https://x.com/RealGuy/status/123456"
    assert "[Grok摘要]" in entry["summary"]
    assert entry["published_parsed"] is not None


def test_build_feed_xml_pubdate_uses_feed_ts_not_tweet_time():
    # pubDate 必须用入库时刻（feed_ts_ms / feed_now_ms），不是推文真实时间 created_ms，
    # 否则历史推文会被 rss_ingest 的增量时间窗砍掉。
    import calendar
    old_tweet_ms = 1700000000000  # 远早于 feed_now
    feed_now = 1781300000000
    item = _feed_item(created_ms=old_tweet_ms)
    item.pop("feed_ts_ms", None)
    xml = gw.build_feed_xml(_topic(), [item], feed_now)
    feed = feedparser.parse(xml)
    pub_ms = calendar.timegm(feed.entries[0]["published_parsed"]) * 1000
    assert abs(pub_ms - feed_now) < 2000  # 用 feed_now，不是 old_tweet_ms
    assert pub_ms > old_tweet_ms


def test_build_feed_xml_escapes_special_chars():
    item = _feed_item(title='<b>标题 & 引号"</b>', description="a < b & c")
    xml = gw.build_feed_xml(_topic(), [item], 1781300000000)
    feed = feedparser.parse(xml)
    assert not feed.bozo
    assert "标题" in feed.entries[0]["title"]


def test_build_feed_xml_writes_image_enclosures():
    item = _feed_item(image_urls=["https://i.redd.it/direct.jpg", "https://preview.redd.it/a.webp?width=800"])
    xml = gw.build_feed_xml(_topic(platform="reddit"), [item], 1781300000000)
    feed = feedparser.parse(xml)

    assert not feed.bozo
    enclosures = feed.entries[0].get("enclosures") or []
    assert [item["href"] for item in enclosures] == [
        "https://i.redd.it/direct.jpg",
        "https://preview.redd.it/a.webp?width=800",
    ]
    assert enclosures[0]["type"] == "image/jpeg"
    assert enclosures[1]["type"] == "image/webp"


def test_build_item_description_assembles_sections():
    item = {
        "summary": "摘要内容",
        "evidence": "链上地址可查",
        "red_flags": ["需绑卡"],
        "signal_score": 4,
        "source_chain": "内部人士爆料",
    }
    tweet = _tweet(author="RealGuy", likes=5, views=340, text="原帖全文", created_ms=1781234567000)
    desc = gw.build_item_description(item, tweet)
    assert desc.startswith("原帖全文")
    for fragment in ("[Grok摘要] 摘要内容", "[证据] 链上地址可查", "[红旗] 需绑卡", "[评分] 4/5", "source_chain: 内部人士爆料", "@RealGuy", "5赞", "发布 2026-"):
        assert fragment in desc


def test_run_grok_api_posts_x_search_request_and_extracts_output(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200
        text = json.dumps({"output_text": "```json\n[]\n```"})

        def json(self):
            return json.loads(self.text)

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Resp()

    monkeypatch.setenv("XAI_API_KEY", "test-key")
    monkeypatch.setenv("GROK_WATCH_MODEL", "grok-test")
    monkeypatch.setattr(gw.requests, "post", fake_post)

    text = gw.run_grok_api("找点真料", timeout_s=7)

    assert text == "```json\n[]\n```"
    assert captured["url"] == "https://api.x.ai/v1/responses"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["timeout"] == 7
    assert captured["json"]["model"] == "grok-test"
    assert captured["json"]["tools"] == [{"type": "x_search", "x_search": {}}]
    assert captured["json"]["input"][0]["content"][0]["text"] == "找点真料"


def test_run_grok_api_extracts_nested_output_text(monkeypatch):
    class _Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "output": [
                    {"content": [{"type": "output_text", "text": "[]"}]},
                ],
            }

    monkeypatch.setenv("XAI_API_KEY", "test-key")
    monkeypatch.setattr(gw.requests, "post", lambda *a, **k: _Resp())

    assert gw.run_grok_api("p") == "[]"


def test_run_grok_api_requires_api_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)

    try:
        gw.run_grok_api("p")
        assert False, "should raise"
    except RuntimeError as exc:
        assert "XAI_API_KEY" in str(exc)


def test_run_grok_defaults_to_web_transport(monkeypatch):
    monkeypatch.delenv("GROK_WATCH_TRANSPORT", raising=False)
    monkeypatch.setattr(gw, "run_grok_api", lambda prompt, timeout_s=0: "api")
    monkeypatch.setattr(gw, "run_grok_web", lambda prompt, timeout_s=0: "web")
    monkeypatch.setattr(gw, "run_grok_cli", lambda *a, **k: "cli")

    assert gw.run_grok("p") == "web"


def test_run_grok_api_transport_is_explicit(monkeypatch):
    monkeypatch.setenv("GROK_WATCH_TRANSPORT", "api")
    monkeypatch.setattr(gw, "run_grok_api", lambda prompt, timeout_s=0: "api")
    monkeypatch.setattr(gw, "run_grok_cli", lambda *a, **k: "cli")

    assert gw.run_grok("p") == "api"


def test_run_grok_web_transport_is_explicit(monkeypatch):
    monkeypatch.setenv("GROK_WATCH_TRANSPORT", "web")
    monkeypatch.setattr(gw, "run_grok_web", lambda prompt, timeout_s=0: "web")
    monkeypatch.setattr(gw, "run_grok_cli", lambda *a, **k: "cli")

    assert gw.run_grok("p") == "web"


@pytest.mark.parametrize("broken_endpoint", ["stale", "invalid"])
def test_validate_grok_transport_relaunches_stale_gpt_browser_endpoint(tmp_path, monkeypatch, broken_endpoint):
    stale_socket = socket.socket()
    stale_socket.bind(("127.0.0.1", 0))
    stale_port = stale_socket.getsockname()[1]
    stale_socket.close()

    live_socket = socket.socket()
    live_socket.bind(("127.0.0.1", 0))
    live_socket.listen(1)
    live_port = live_socket.getsockname()[1]

    endpoint = tmp_path / "endpoint.txt"
    endpoint.write_text(
        (
            f"ws://127.0.0.1:{stale_port}/devtools/browser/stale"
            if broken_endpoint == "stale"
            else "ws://127.0.0.1:not-a-port/devtools/browser/invalid"
        ),
        encoding="utf-8",
    )
    cli = tmp_path / "cli.js"
    cli.write_text("cli", encoding="utf-8")
    captured = {}

    class _Completed:
        returncode = 0
        stdout = "launched"
        stderr = ""

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        endpoint.write_text(
            f"ws://127.0.0.1:{live_port}/devtools/browser/live",
            encoding="utf-8",
        )
        return _Completed()

    monkeypatch.setenv("GROK_WATCH_TRANSPORT", "web")
    monkeypatch.setattr(gw, "GROK_WEB_ENDPOINT_FILE", str(endpoint))
    monkeypatch.setattr(gw, "GROK_BROWSER_CLI", cli)
    monkeypatch.setattr(gw, "GROK_WEB_COMMAND", sys.executable)
    monkeypatch.setattr(gw, "GROK_GPT_BROWSER_COMMAND", sys.executable, raising=False)
    monkeypatch.setattr(gw, "GROK_GPT_BROWSER_LAUNCH_TIMEOUT_S", 30, raising=False)
    monkeypatch.setattr(gw.subprocess, "run", fake_run)

    try:
        gw.validate_grok_transport()
    finally:
        live_socket.close()

    assert captured["args"] == [sys.executable, "launch"]
    assert captured["kwargs"]["timeout"] == 30


def test_run_grok_web_invokes_grok_browser_and_extracts_text(tmp_path, monkeypatch):
    endpoint = tmp_path / "endpoint.txt"
    endpoint.write_text("ws://127.0.0.1:123/devtools/browser/test", encoding="utf-8")
    cli = tmp_path / "cli.js"
    cli.write_text("cli", encoding="utf-8")
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    captured = {}

    class _Completed:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        prompt_path = args[args.index("--file") + 1]
        captured["prompt"] = open(prompt_path, encoding="utf-8").read()
        return _Completed(json.dumps({"text": "```json\n[]\n```"}))

    monkeypatch.setattr(gw, "GROK_WEB_COMMAND", sys.executable)
    monkeypatch.setattr(gw, "GROK_BROWSER_CLI", cli)
    monkeypatch.setattr(gw, "GROK_LEGACY_WEB_RUNNER", Path(""))
    monkeypatch.setattr(gw, "GROK_WEB_ENDPOINT_FILE", str(endpoint))
    monkeypatch.setattr(gw, "GROK_WEB_NODE_MODULES", str(node_modules))
    monkeypatch.setattr(gw, "GROK_WEB_URL", "https://grok.com/test")
    monkeypatch.setattr(gw, "GROK_WEB_MODEL", "Expert")
    monkeypatch.setattr(gw, "GROK_WEB_KEEP_PAGE", "")
    monkeypatch.setattr(gw.subprocess, "run", fake_run)

    text = gw.run_grok_web("找点真料", timeout_s=9)

    assert text == "```json\n[]\n```"
    assert captured["args"] == [
        sys.executable,
        str(cli),
        "send",
        "--file",
        captured["args"][captured["args"].index("--file") + 1],
        "--model",
        "Expert",
        "--endpoint-file",
        str(endpoint),
        "--url",
        "https://grok.com/test",
        "--timeout",
        "9",
        "--json",
    ]
    assert captured["prompt"] == "找点真料\n"
    env = captured["kwargs"]["env"]
    assert env["GROK_BROWSER_ENDPOINT_FILE"] == str(endpoint)
    assert str(node_modules) in env["NODE_PATH"]
    assert captured["kwargs"]["timeout"] == 39


def test_run_grok_cli_builds_guarded_command_and_parses_json(monkeypatch):
    captured = {}

    class _Proc:
        pid = 1234
        returncode = 0

        def communicate(self, timeout=None):
            captured["timeout"] = timeout
            return (json.dumps({"text": "```json\n[]\n```", "sessionId": "s1"}), "")

        def wait(self, timeout=None):
            return self.returncode

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(gw.subprocess, "Popen", fake_popen)

    text = gw.run_grok_cli("找点真料", timeout_s=12)

    assert text == "```json\n[]\n```"
    assert captured["args"][0] == gw.GROK_COMMAND
    assert "--prompt-file" in captured["args"]
    assert "--output-format" in captured["args"] and "json" in captured["args"]
    assert "--leader-socket" in captured["args"]
    assert captured["args"][captured["args"].index("--leader-socket") + 1] == gw.GROK_LEADER_SOCKET
    assert ".grok-search" in gw.GROK_LEADER_SOCKET
    assert "--cwd" in captured["args"]
    assert captured["args"][captured["args"].index("--cwd") + 1] == str(gw.GROK_CWD)
    assert captured["kwargs"]["cwd"] == str(gw.GROK_CWD)
    assert ".grok-search" in str(gw.GROK_CWD)
    assert "watch-cwd" in str(gw.GROK_CWD)
    assert "--verbatim" in captured["args"]
    tools_value = captured["args"][captured["args"].index("--tools") + 1]
    assert tools_value == ""
    assert "--deny" in captured["args"]
    deny_values = [captured["args"][i + 1] for i, arg in enumerate(captured["args"][:-1]) if arg == "--deny"]
    assert "Bash" in deny_values
    assert "MCPTool" in deny_values
    assert "--disallowed-tools" in captured["args"]
    assert "PowerShell" in captured["args"][captured["args"].index("--disallowed-tools") + 1]
    assert "update_goal" in captured["args"][captured["args"].index("--disallowed-tools") + 1]
    assert "--system-prompt-override" in captured["args"]
    assert "strict JSON-only social and AI news search worker" in captured["args"][captured["args"].index("--system-prompt-override") + 1]
    assert "native X/web search" in captured["args"][captured["args"].index("--system-prompt-override") + 1]
    assert "browser automation" in captured["args"][captured["args"].index("--system-prompt-override") + 1]
    assert "--rules" in captured["args"]
    assert "Do not invoke skills" in captured["args"][captured["args"].index("--rules") + 1]
    assert "--max-turns" in captured["args"] and "12" in captured["args"]
    assert "--no-subagents" in captured["args"]
    assert "--no-memory" in captured["args"]
    assert "--no-plan" in captured["args"]
    assert "--disable-web-search" not in captured["args"]
    child_env = captured["kwargs"]["env"]
    for vendor in ("CLAUDE", "CURSOR"):
        for cell in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS"):
            assert child_env[f"GROK_{vendor}_{cell}_ENABLED"] == "false"
    assert child_env["GROK_SUBAGENTS"] == "0"
    assert child_env["GROK_MEMORY"] == "0"
    assert child_env["GROK_HOME"].endswith(".grok-search")
    assert captured["timeout"] == 12


def test_run_grok_cli_falls_back_to_raw_stdout_and_raises_on_failure(monkeypatch):
    class _Plain:
        pid = 1234
        returncode = 0

        def communicate(self, timeout=None):
            return ("[]", "")

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(gw.subprocess, "Popen", lambda *a, **k: _Plain())
    assert gw.run_grok_cli("p") == "[]"

    class _Fail:
        pid = 1234
        returncode = 1

        def communicate(self, timeout=None):
            return ("", "boom")

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(gw.subprocess, "Popen", lambda *a, **k: _Fail())
    try:
        gw.run_grok_cli("p")
        assert False, "should raise"
    except RuntimeError as exc:
        assert "boom" in str(exc)


def test_main_fails_fast_when_explicit_api_key_missing(tmp_path, monkeypatch):
    topics_path = tmp_path / "topics.json"
    state_path = tmp_path / "state.json"
    prompt_path = tmp_path / "p.md"
    prompt_path.write_text("prompt", encoding="utf-8")
    topics_path.write_text(json.dumps([
        _topic(prompt_file=str(prompt_path)),
    ]), encoding="utf-8")
    monkeypatch.setenv("GROK_WATCH_TRANSPORT", "api")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)

    code = gw.main([
        "--topics-path", str(topics_path),
        "--state-path", str(state_path),
        "--feed-dir", str(tmp_path / "feeds"),
        "--force",
    ])

    assert code == 2
    assert not state_path.exists()


def test_prune_seen_drops_entries_older_than_14_days():
    now = 100 * DAY_MS
    state = {
        "topics": {},
        "seen_posts": {"old": now - 15 * DAY_MS, "new": now - 1 * DAY_MS},
        "seen_text": {"oldh": now - 15 * DAY_MS, "newh": now - 1 * DAY_MS},
    }
    gw.prune_seen(state, now)
    assert set(state["seen_posts"]) == {"new"}
    assert set(state["seen_text"]) == {"newh"}


def _grok_payload(items):
    return "```json\n" + json.dumps(items, ensure_ascii=False) + "\n```"


NOW_MS = 1781234567000


def test_process_topic_end_to_end(tmp_path):
    topic = _topic(prompt_file=str(tmp_path / "p.md"))
    (tmp_path / "p.md").write_text("prompt", encoding="utf-8")
    state = gw.default_state()

    items = [
        {"title": "好羊毛", "url": "https://x.com/A/status/111", "summary": "s1",
         "category": "deal", "official_url": "https://good.example", "red_flags": [], "signal_score": 4},
        {"title": "无效链接", "url": "https://example.com/nope", "summary": "s2", "category": "deal"},
        {"title": "查无此帖", "url": "https://x.com/B/status/222", "summary": "s3", "category": "deal"},
    ]
    tweets = {
        "111": {"author": "RealA", "followers": 1000, "created_ms": NOW_MS - HOUR_MS, "likes": 3, "views": 100, "text": "tweet111"},
        "222": None,
    }

    stats = gw.process_topic(
        topic, state, NOW_MS,
        run_grok_fn=lambda prompt: _grok_payload(items),
        lookup_fn=lambda sid, handle: tweets.get(sid),
        feed_dir=tmp_path / "feeds",
    )

    assert stats["returned"] == 3 and stats["accepted"] == 1
    assert "111" in state["seen_posts"]
    assert state["topics"]["deals"]["last_run_ms"] == NOW_MS

    xml = (tmp_path / "feeds" / "deals.xml").read_text(encoding="utf-8")
    feed = feedparser.parse(xml)
    assert feed.entries[0]["link"] == "https://x.com/RealA/status/111"

    stats2 = gw.process_topic(
        topic, state, NOW_MS + HOUR_MS,
        run_grok_fn=lambda prompt: _grok_payload(items[:1]),
        lookup_fn=lambda sid, handle: tweets.get(sid),
        feed_dir=tmp_path / "feeds",
    )
    assert stats2["accepted"] == 0 and stats2["dropped"]["dup_post"] == 1


def test_process_topic_accepts_reddit_permalink(tmp_path):
    topic = _topic(key="reddit_codex", platform="reddit", prompt_file=str(tmp_path / "p.md"))
    (tmp_path / "p.md").write_text("prompt", encoding="utf-8")
    state = gw.default_state()
    items = [
        {"title": "Codex /resume 技巧", "platform": "reddit",
         "url": "https://www.reddit.com/r/codex/comments/1abcxyz/title/k9comment/",
         "summary": "CLI 和 Desktop 切换", "category": "codex", "red_flags": [], "signal_score": 4},
    ]

    def reddit_lookup(url):
        return {
            "platform": "reddit",
            "source_id": "reddit:1abcxyz:k9comment",
            "author": "helper",
            "subreddit": "codex",
            "followers": 0,
            "created_ms": NOW_MS - HOUR_MS,
            "likes": 5,
            "views": 0,
            "text": "use /resume after switching desktop and CLI",
            "link": "https://www.reddit.com/r/codex/comments/1abcxyz/title/k9comment/",
            "image_urls": ["https://i.redd.it/direct.jpg"],
        }

    stats = gw.process_topic(
        topic, state, NOW_MS,
        run_grok_fn=lambda prompt: _grok_payload(items),
        lookup_fn=lambda sid, handle: None,
        feed_dir=tmp_path / "feeds",
        reddit_lookup_fn=reddit_lookup,
    )

    assert stats["accepted"] == 1
    assert "reddit:1abcxyz:k9comment" in state["seen_posts"]
    xml = (tmp_path / "feeds" / "reddit_codex.xml").read_text(encoding="utf-8")
    feed = feedparser.parse(xml)
    assert feed.entries[0]["link"] == "https://www.reddit.com/r/codex/comments/1abcxyz/title/k9comment/"
    assert "r/codex" in feed.entries[0]["summary"]
    assert feed.entries[0]["enclosures"][0]["href"] == "https://i.redd.it/direct.jpg"


def test_process_topic_uses_original_prompt_file_even_if_query_templates_exist(tmp_path):
    prompt_path = tmp_path / "p.md"
    prompt_path.write_text("original rich prompt", encoding="utf-8")
    topic = _topic(prompt_file=str(prompt_path), query_templates=["site:x.com/*/status test"])
    state = gw.default_state()
    prompts = []

    def run_grok(prompt):
        prompts.append(prompt)
        return _grok_payload([{"title": "bad", "url": "https://example.com/nope"}])

    stats = gw.process_topic(topic, state, NOW_MS, run_grok, lambda s, h: None, tmp_path / "feeds")

    assert prompts == ["original rich prompt"]
    assert stats["returned"] == 1


def test_process_topic_dedups_same_text_across_posts(tmp_path):
    topic = _topic(prompt_file=str(tmp_path / "p.md"))
    (tmp_path / "p.md").write_text("prompt", encoding="utf-8")
    state = gw.default_state()
    items = [
        {"title": "矩阵帖1", "url": "https://x.com/A/status/111", "summary": "s", "category": "deal", "official_url": "https://x.example"},
        {"title": "矩阵帖2", "url": "https://x.com/B/status/222", "summary": "s", "category": "deal", "official_url": "https://x.example"},
    ]

    def lookup(sid, handle):
        return {"author": handle, "followers": 1000, "created_ms": NOW_MS - HOUR_MS,
                "likes": 1, "views": 100, "text": "同一段营销文案"}

    stats = gw.process_topic(topic, state, NOW_MS, lambda p: _grok_payload(items), lookup, tmp_path / "feeds")
    assert stats["accepted"] == 1 and stats["dropped"]["dup_text"] == 1


def test_process_topic_retries_once_on_empty_output(tmp_path):
    topic = _topic(prompt_file=str(tmp_path / "p.md"))
    (tmp_path / "p.md").write_text("prompt", encoding="utf-8")
    state = gw.default_state()
    calls = []

    def flaky(prompt):
        calls.append(1)
        return "" if len(calls) == 1 else _grok_payload([])

    stats = gw.process_topic(topic, state, NOW_MS, flaky, lambda s, h: None, tmp_path / "feeds")
    assert len(calls) == 2
    assert stats["returned"] == 0
    assert state["topics"]["deals"]["last_run_ms"] == NOW_MS


def test_process_topic_provider_failure_does_not_advance_state(tmp_path):
    topic = _topic(prompt_file=str(tmp_path / "p.md"))
    (tmp_path / "p.md").write_text("prompt", encoding="utf-8")
    state = gw.default_state()
    before = json.loads(json.dumps(state))

    with pytest.raises(RuntimeError, match="provider failed"):
        gw.process_topic(
            topic,
            state,
            NOW_MS,
            run_grok_fn=lambda prompt: (_ for _ in ()).throw(RuntimeError("network down")),
            lookup_fn=lambda sid, handle: None,
            feed_dir=tmp_path / "feeds",
        )

    assert state == before
    assert not (tmp_path / "feeds" / "deals.xml").exists()


def test_process_topic_feed_failure_does_not_commit_seen_or_topic_state(tmp_path, monkeypatch):
    topic = _topic(prompt_file=str(tmp_path / "p.md"))
    (tmp_path / "p.md").write_text("prompt", encoding="utf-8")
    state = gw.default_state()
    before = json.loads(json.dumps(state))
    items = [{"title": "新消息", "url": "https://x.com/A/status/111", "category": "deal"}]

    monkeypatch.setattr(
        gw,
        "write_feed",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        gw.process_topic(
            topic,
            state,
            NOW_MS,
            run_grok_fn=lambda prompt: _grok_payload(items),
            lookup_fn=lambda sid, handle: {
                "author": "A",
                "followers": 100,
                "created_ms": NOW_MS - HOUR_MS,
                "likes": 3,
                "views": 100,
                "text": "真实正文",
            },
            feed_dir=tmp_path / "feeds",
        )

    assert state == before


def test_process_topic_keeps_recent_items_within_72h(tmp_path):
    topic = _topic(prompt_file=str(tmp_path / "p.md"))
    (tmp_path / "p.md").write_text("prompt", encoding="utf-8")
    state = gw.default_state()
    state["topics"]["deals"] = {
        "last_run_ms": 0,
        "recent_items": [
            _feed_item(status_id="old", link="https://x.com/o/status/9", created_ms=NOW_MS - 73 * HOUR_MS),
            _feed_item(status_id="keep", link="https://x.com/k/status/8", created_ms=NOW_MS - 10 * HOUR_MS),
        ],
    }

    gw.process_topic(topic, state, NOW_MS, lambda p: _grok_payload([]), lambda s, h: None, tmp_path / "feeds")

    links = [i["link"] for i in state["topics"]["deals"]["recent_items"]]
    assert links == ["https://x.com/k/status/8"]


def test_main_runs_due_topics_and_saves_state(tmp_path, monkeypatch):
    import time as _time

    topics_path = tmp_path / "topics.json"
    topics_path.write_text(json.dumps([
        _topic(key="t1", prompt_file=str(tmp_path / "p.md")),
        _topic(key="t2", prompt_file=str(tmp_path / "p.md"), interval_hours=9999),
    ]), encoding="utf-8")
    (tmp_path / "p.md").write_text("prompt", encoding="utf-8")
    state_path = tmp_path / "state.json"
    gw.save_state(state_path, {"topics": {"t2": {"last_run_ms": int(_time.time() * 1000)}}, "seen_posts": {}, "seen_text": {}})

    ran = []

    def fake_process(topic, state, now_ms, run_grok_fn, lookup_fn, feed_dir):
        ran.append(topic["key"])
        state["topics"].setdefault(topic["key"], {})["last_run_ms"] = now_ms
        return {"topic": topic["key"], "returned": 0, "accepted": 0, "dropped": {}}

    monkeypatch.setattr(gw, "process_topic", fake_process)
    monkeypatch.setattr(gw, "validate_grok_transport", lambda: None)

    code = gw.main([
        "--topics-path", str(topics_path),
        "--state-path", str(state_path),
        "--feed-dir", str(tmp_path / "feeds"),
    ])

    assert code == 0
    assert ran == ["t1"]
    saved = gw.load_state(state_path)
    assert saved["topics"]["t1"]["last_run_ms"] > 0


def test_main_topic_filter_and_force(tmp_path, monkeypatch):
    import time as _time

    topics_path = tmp_path / "topics.json"
    topics_path.write_text(json.dumps([
        _topic(key="t1", prompt_file=str(tmp_path / "p.md")),
        _topic(key="t2", prompt_file=str(tmp_path / "p.md")),
    ]), encoding="utf-8")
    (tmp_path / "p.md").write_text("prompt", encoding="utf-8")
    state_path = tmp_path / "state.json"
    now = int(_time.time() * 1000)
    gw.save_state(state_path, {"topics": {"t2": {"last_run_ms": now}}, "seen_posts": {}, "seen_text": {}})

    ran = []
    monkeypatch.setattr(
        gw, "process_topic",
        lambda topic, *a, **k: (ran.append(topic["key"]), {"topic": topic["key"], "returned": 0, "accepted": 0, "dropped": {}})[1],
    )
    monkeypatch.setattr(gw, "validate_grok_transport", lambda: None)

    gw.main(["--topics-path", str(topics_path), "--state-path", str(state_path),
             "--feed-dir", str(tmp_path / "f"), "--topic", "t2", "--force"])

    assert ran == ["t2"]


def test_main_continues_after_single_topic_failure(tmp_path, monkeypatch):
    topics_path = tmp_path / "topics.json"
    topics_path.write_text(json.dumps([
        _topic(key="t1", prompt_file=str(tmp_path / "p.md")),
        _topic(key="t2", prompt_file=str(tmp_path / "p.md")),
    ]), encoding="utf-8")
    (tmp_path / "p.md").write_text("prompt", encoding="utf-8")
    state_path = tmp_path / "state.json"

    ran = []

    def boom_then_ok(topic, *a, **k):
        ran.append(topic["key"])
        if topic["key"] == "t1":
            raise RuntimeError("boom")
        return {"topic": topic["key"], "returned": 0, "accepted": 0, "dropped": {}}

    monkeypatch.setattr(gw, "process_topic", boom_then_ok)
    monkeypatch.setattr(gw, "validate_grok_transport", lambda: None)

    code = gw.main(["--topics-path", str(topics_path), "--state-path", str(state_path), "--feed-dir", str(tmp_path / "f")])

    assert code == 1
    assert ran == ["t1", "t2"]
