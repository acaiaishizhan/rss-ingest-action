import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import rss_parser


def test_entry_text_content_prefers_content_encoded_key_over_summary():
    entry = {
        "content:encoded": "<p>这是 content encoded 全文。" + "正文" * 80 + "</p>",
        "summary": "一句话摘要",
    }

    text = rss_parser.entry_text_content(entry)

    assert "content encoded 全文" in text
    assert text != "一句话摘要"


def test_entry_text_content_prefers_content_encoded_attr_over_summary():
    entry = {
        "content_encoded": "<p>这是 content_encoded 全文。" + "正文" * 80 + "</p>",
        "summary": "一句话摘要",
    }

    text = rss_parser.entry_text_content(entry)

    assert "content_encoded 全文" in text
    assert text != "一句话摘要"


MINIMAL_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title><link>https://x.com</link>
<item><title>hello</title><link>https://x.com/a/status/1</link>
<guid isPermaLink="true">https://x.com/a/status/1</guid>
<pubDate>Wed, 10 Jun 2026 16:00:09 GMT</pubDate>
<description>desc</description></item>
</channel></rss>"""


def test_fetch_feed_reads_file_uri(tmp_path):
    feed_path = tmp_path / "local.xml"
    feed_path.write_text(MINIMAL_RSS, encoding="utf-8")

    feed = rss_parser.fetch_feed(feed_path.as_uri(), timeout=5, retries=1)

    assert feed.entries[0]["title"] == "hello"
    assert feed.entries[0]["link"] == "https://x.com/a/status/1"


def test_fetch_feed_reads_bare_windows_path(tmp_path):
    feed_path = tmp_path / "local.xml"
    feed_path.write_text(MINIMAL_RSS, encoding="utf-8")

    feed = rss_parser.fetch_feed(str(feed_path), timeout=5, retries=1)

    assert feed.entries[0]["title"] == "hello"


def test_fetch_feed_local_missing_file_raises(tmp_path):
    try:
        rss_parser.fetch_feed(str(tmp_path / "nope.xml"), timeout=5, retries=1)
        assert False, "should raise"
    except RuntimeError:
        pass
