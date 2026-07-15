import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import article_extractor
import http_safety


def test_generic_article_candidate_rejects_non_public_literal_hosts():
    for url in (
        "http://127.0.0.1:11434/api/tags",
        "http://10.0.0.5/internal",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/admin",
        "http://localhost:8080/health",
    ):
        assert article_extractor._is_generic_article_candidate(url) is False


def test_article_http_get_rejects_hostname_resolving_to_private_ip(monkeypatch):
    monkeypatch.setattr(
        http_safety.socket,
        "getaddrinfo",
        lambda host, port, type=0: [(2, 1, 6, "", ("192.168.1.20", port))],
    )
    monkeypatch.setattr(
        article_extractor.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unsafe URL reached requests")),
    )

    with pytest.raises(http_safety.UnsafeUrlError):
        article_extractor._http_get("https://private.example/story", headers={}, timeout=3)


class DummyResponse:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"


def test_extract_article_text_uses_rss_content_without_fetch(monkeypatch):
    monkeypatch.setattr(
        article_extractor,
        "_http_get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not fetch")),
    )
    entry = {
        "content": [{"value": "<p>" + ("正文" * 80) + "</p>"}],
        "summary": "short",
    }

    result = article_extractor.extract_article_text(
        "https://example.com/post",
        "Example",
        "https://example.com/feed.xml",
        entry,
    )

    assert result["method"] == "rss_content"
    assert result["status"] == "ok"
    assert "正文" in result["text"]


def test_extract_article_text_force_fetches_article_even_when_rss_summary_is_long(monkeypatch):
    html = """
    <html><body>
      <main>
        <article>
          <h1>Original report</h1>
          <p>Original article paragraph with details about the AI product launch.</p>
          <p>The article includes pricing, timeline, customer examples, and deployment limits.</p>
        </article>
      </main>
    </body></html>
    """
    seen = {}

    def fake_get(url, *args, **kwargs):
        seen["url"] = url
        return DummyResponse(text=html)

    monkeypatch.setattr(article_extractor, "_http_get", fake_get)

    result = article_extractor.extract_article_text(
        "https://example.com/posts/ai-product-launch",
        "AI HOT 聚合源",
        "https://aihot.virxact.com/feed/all.xml",
        {"summary": "AI HOT 摘要：" + ("这是一段已经超过长度阈值的中文摘要。" * 20)},
        min_length=80,
        force_fetch=True,
    )

    assert seen["url"] == "https://example.com/posts/ai-product-launch"
    assert result["method"] == "source_parser:generic_article"
    assert result["status"] == "ok"
    assert "Original article paragraph" in result["text"]


def test_extract_article_text_force_fetches_x_status_from_embedded_html_without_browser(monkeypatch):
    seen = {}
    html = r'''
    <html><body><script>
    {"2062487937825255748":{"full_text":"Build your voice agent with MiniMax Speech 2.8 Turbo.","created_at":"2026-06-04T10:53:38.000Z","user":"1875078099538423808"},"1875078099538423808":{"name":"MiniMax (official)","screen_name":"MiniMax_AI"}}
    </script></body></html>
    '''

    def fake_get(url, *args, **kwargs):
        seen["url"] = url
        return DummyResponse(text=html)

    def fail_browser(*args, **kwargs):
        raise AssertionError("browser should not be opened when embedded X payload is available")

    monkeypatch.setattr(article_extractor, "_http_get", fake_get)
    monkeypatch.setattr(article_extractor, "_fetch_browser_x_status_text", fail_browser, raising=False)

    result = article_extractor.extract_article_text(
        "https://x.com/MiniMax_AI/status/2062487937825255748",
        "AI HOT 聚合源",
        "https://aihot.virxact.com/feed/all.xml",
        {"summary": "MiniMax 推出 Speech 2.8 Turbo。"},
        force_fetch=True,
    )

    assert seen["url"] == "https://x.com/MiniMax_AI/status/2062487937825255748"
    assert result["method"] == "source_parser:x_status_browser"
    assert result["status"] == "ok"
    assert "MiniMax (official) (@MiniMax_AI)" in result["text"]
    assert "Speech 2.8 Turbo" in result["text"]


def test_extract_article_text_force_fetches_x_status_from_oembed_without_browser(monkeypatch):
    seen = {}
    oembed_json = r"""{
      "url": "https://x.com/MiniMax_AI/status/2062487937825255748",
      "author_name": "MiniMax (official)",
      "author_url": "https://x.com/MiniMax_AI",
      "html": "<blockquote class=\"twitter-tweet\"><p lang=\"en\" dir=\"ltr\">Speech 2.8 Turbo now supports 40+ languages.</p>&mdash; MiniMax (official) (@MiniMax_AI) <a href=\"https://x.com/MiniMax_AI/status/2062487937825255748\">June 4, 2026</a></blockquote>"
    }"""

    def fake_get(url, *args, **kwargs):
        seen["url"] = url
        return DummyResponse(
            text=oembed_json,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    monkeypatch.setattr(article_extractor.config, "ENABLE_X_BROWSER_FALLBACK", False, raising=False)
    monkeypatch.setattr(
        article_extractor,
        "_fetch_embedded_x_status_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("embedded unavailable")),
        raising=False,
    )
    monkeypatch.setattr(article_extractor, "_http_get", fake_get)
    monkeypatch.setattr(
        article_extractor,
        "_fetch_browser_x_status_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("browser should not be opened")),
        raising=False,
    )

    result = article_extractor.extract_article_text(
        "https://x.com/MiniMax_AI/status/2062487937825255748",
        "AI HOT 聚合源",
        "https://aihot.virxact.com/feed/all.xml",
        {"summary": "MiniMax 推出 Speech 2.8 Turbo。"},
        force_fetch=True,
    )

    assert seen["url"].startswith("https://publish.twitter.com/oembed?")
    assert "url=https%3A%2F%2Fx.com%2FMiniMax_AI%2Fstatus%2F2062487937825255748" in seen["url"]
    assert result["method"] == "source_parser:x_status_browser"
    assert result["status"] == "ok"
    assert "MiniMax (official) (@MiniMax_AI)" in result["text"]
    assert "Speech 2.8 Turbo now supports 40+ languages." in result["text"]


def test_extract_article_text_keeps_x_summary_when_http_fallbacks_fail(monkeypatch):
    def fake_get(*args, **kwargs):
        return DummyResponse(status_code=404, text="not found")

    monkeypatch.setattr(article_extractor.config, "ENABLE_X_BROWSER_FALLBACK", False, raising=False)
    monkeypatch.setattr(
        article_extractor,
        "_fetch_embedded_x_status_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("embedded unavailable")),
        raising=False,
    )
    monkeypatch.setattr(article_extractor, "_http_get", fake_get)
    monkeypatch.setattr(
        article_extractor,
        "_fetch_browser_x_status_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("browser should not be opened")),
        raising=False,
    )

    result = article_extractor.extract_article_text(
        "https://x.com/MiniMax_AI/status/2062487937825255748",
        "AI HOT 聚合源",
        "https://aihot.virxact.com/feed/all.xml",
        {"summary": "MiniMax 推出 Speech 2.8 Turbo。"},
        force_fetch=True,
    )

    assert result["method"] == "rss_summary"
    assert result["status"] == "fetch_error"
    assert result["text"] == "MiniMax 推出 Speech 2.8 Turbo。"
    assert "browser x fallback disabled" in result["error"]


def test_extract_article_text_force_fetches_x_status_with_enabled_browser_fallback(monkeypatch):
    seen = {}

    def fake_x_fetch(url, timeout):
        seen["url"] = url
        seen["timeout"] = timeout
        return "X 原帖\n作者：MiniMax (@MiniMax_AI)\n正文：Speech 2.8 Turbo now supports 40+ languages."

    monkeypatch.setattr(
        article_extractor,
        "_fetch_embedded_x_status_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("embedded unavailable")),
        raising=False,
    )
    monkeypatch.setattr(
        article_extractor,
        "_fetch_oembed_x_status_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("oembed unavailable")),
        raising=False,
    )
    monkeypatch.setattr(article_extractor.config, "ENABLE_X_BROWSER_FALLBACK", True, raising=False)
    monkeypatch.setattr(article_extractor, "_fetch_browser_x_status_text", fake_x_fetch, raising=False)

    result = article_extractor.extract_article_text(
        "https://x.com/MiniMax_AI/status/2062487937825255748",
        "AI HOT 聚合源",
        "https://aihot.virxact.com/feed/all.xml",
        {"summary": "MiniMax 推出 Speech 2.8 Turbo。"},
        force_fetch=True,
    )

    assert seen["url"] == "https://x.com/MiniMax_AI/status/2062487937825255748"
    assert seen["timeout"] == 12
    assert result["method"] == "source_parser:x_status_browser"
    assert result["status"] == "ok"
    assert "Speech 2.8 Turbo" in result["text"]


def test_extract_article_text_fetches_generic_article_when_summary_short(monkeypatch):
    html = """
    <html><body>
      <header>Site navigation should be skipped</header>
      <main>
        <article>
          <h1>Useful article</h1>
          <p>First paragraph from a normal article page with useful context.</p>
          <p>Second paragraph gives enough detail for the screen LLM to judge.</p>
        </article>
      </main>
      <footer>Footer should be skipped</footer>
    </body></html>
    """
    seen = {}

    def fake_get(url, *args, **kwargs):
        seen["url"] = url
        return DummyResponse(text=html)

    monkeypatch.setattr(article_extractor, "_http_get", fake_get)

    result = article_extractor.extract_article_text(
        "https://example.com/posts/ai-update",
        "Example",
        "https://example.com/feed.xml",
        {"summary": "short summary"},
        min_length=40,
    )

    assert seen["url"] == "https://example.com/posts/ai-update"
    assert result["method"] == "source_parser:generic_article"
    assert result["status"] == "ok"
    assert "First paragraph from a normal article page" in result["text"]
    assert "Site navigation" not in result["text"]
    assert "Footer" not in result["text"]


def test_extract_article_text_fetches_towards_ai_preview_with_browser(monkeypatch):
    browser_text = (
        "Claude Code Cheat Sheet full browser article. "
        "Every command, shortcut, and config template that actually matters. "
        * 20
    )
    seen = {}

    def fake_browser_fetch(url, timeout):
        seen["url"] = url
        seen["timeout"] = timeout
        return browser_text

    monkeypatch.setattr(article_extractor.config, "ENABLE_BROWSER_ARTICLE_FETCH", True, raising=False)
    monkeypatch.setattr(article_extractor, "_fetch_browser_article_text", fake_browser_fetch, raising=False)
    monkeypatch.setattr(
        article_extractor,
        "_http_get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should use browser, not requests")),
    )

    result = article_extractor.extract_article_text(
        "https://pub.towardsai.net/claude-code-cheat-sheet-every-command-shortcut-and-config-template-that-actually-matters-88fbe108ff1c",
        "Towards AI - Medium",
        "https://pub.towardsai.net/feed",
        {
            "summary": (
                "Part 19: A working reference for engineers who use Claude Code daily: "
                "the keyboard shortcuts worth memorizing, the built-in commands... "
                "Continue reading on Towards AI »"
            )
        },
    )

    assert seen["url"].startswith("https://pub.towardsai.net/claude-code-cheat-sheet")
    assert seen["timeout"] == 12
    assert result["method"] == "source_parser:browser_medium_article"
    assert result["status"] == "ok"
    assert result["text"] == browser_text


def test_extract_article_text_skips_excluded_short_summary_without_fetch(monkeypatch):
    monkeypatch.setattr(
        article_extractor,
        "_http_get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not fetch")),
    )

    result = article_extractor.extract_article_text(
        "https://x.com/example/status/1",
        "RSSHub Twitter",
        "https://rsshub.app/twitter/user/example",
        {"summary": "short summary"},
    )

    assert result["method"] == "rss_summary"
    assert result["status"] == "short"
    assert result["text"] == "short summary"


def test_extract_article_text_skips_pdf_link_without_fetch(monkeypatch):
    monkeypatch.setattr(
        article_extractor,
        "_http_get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not fetch pdf")),
    )

    result = article_extractor.extract_article_text(
        "https://example.com/report.pdf",
        "Example",
        "https://example.com/feed.xml",
        {"summary": "short summary"},
    )

    assert result["method"] == "rss_summary"
    assert result["status"] == "short"
    assert result["text"] == "short summary"


def test_extract_article_text_fetches_huggingface_blog_when_feed_empty(monkeypatch):
    html = """
    <main>
      <div class="blog-content prose">
        <div class="not-prose">Author card should be skipped</div>
        <h1>Title</h1>
        <p>First useful paragraph for the Hugging Face article.</p>
        <p>Second useful paragraph with enough text to pass the threshold.</p>
      </div>
    </main>
    """
    monkeypatch.setattr(
        article_extractor,
        "_http_get",
        lambda *args, **kwargs: DummyResponse(text=html),
    )

    result = article_extractor.extract_article_text(
        "https://huggingface.co/blog/example-post",
        "Hugging Face Blog",
        "https://huggingface.co/blog/feed.xml",
        {"title": "Example"},
        min_length=40,
    )

    assert result["method"] == "source_parser:huggingface_blog"
    assert result["status"] == "ok"
    assert "First useful paragraph" in result["text"]
    assert "Author card" not in result["text"]


def test_extract_article_text_fetches_ithome_article_when_summary_short(monkeypatch):
    html = """
    <html><body>
      <div class="post_content" id="paragraph">
        <p>IT之家 5 月 9 日消息，这是正文第一段，提供完整上下文。</p>
        <p>这是正文第二段，继续解释新闻背景和具体影响。</p>
        <p class="ad-tips">广告声明：这段应该被跳过。</p>
      </div>
    </body></html>
    """
    monkeypatch.setattr(
        article_extractor,
        "_http_get",
        lambda *args, **kwargs: DummyResponse(text=html),
    )

    result = article_extractor.extract_article_text(
        "https://www.ithome.com/0/947/994.htm",
        "IT之家：AI 标签",
        "https://www.ithome.com/tags/AI/",
        {"summary": "短标题"},
        min_length=20,
    )

    assert result["method"] == "source_parser:ithome_article"
    assert result["status"] == "ok"
    assert "正文第一段" in result["text"]
    assert "广告声明" not in result["text"]


def test_extract_article_text_falls_back_to_rss_summary_on_fetch_error(monkeypatch):
    monkeypatch.setattr(
        article_extractor,
        "_http_get",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("timeout")),
    )

    result = article_extractor.extract_article_text(
        "https://huggingface.co/blog/example-post",
        "Hugging Face Blog",
        "https://huggingface.co/blog/feed.xml",
        {"summary": "fallback summary"},
    )

    assert result["text"] == "fallback summary"
    assert result["method"] == "rss_summary"
    assert result["status"] == "fetch_error"
    assert "timeout" in result["error"]


def test_extract_article_text_fetches_hacker_news_article_link_stub(monkeypatch):
    html = """
    <html><body>
      <nav>Navigation should be skipped</nav>
      <article>
        <h1>Real article title</h1>
        <p>First paragraph from the external article with useful context.</p>
        <p>Second paragraph explains the practical implications in detail.</p>
      </article>
    </body></html>
    """
    seen = {}

    def fake_get(url, *args, **kwargs):
        seen["url"] = url
        return DummyResponse(text=html)

    monkeypatch.setattr(article_extractor, "_http_get", fake_get)

    result = article_extractor.extract_article_text(
        "https://example.com/real-article",
        "Hacker News",
        "https://hnrss.org/frontpage",
        {
            "summary": (
                '<p>Article URL: <a href="https://example.com/real-article">https://example.com/real-article</a></p>'
                '<p>Comments URL: <a href="https://news.ycombinator.com/item?id=1">https://news.ycombinator.com/item?id=1</a></p>'
                "<p>Points: 34</p><p># Comments: 17</p>"
            )
        },
        min_length=40,
    )

    assert seen["url"] == "https://example.com/real-article"
    assert result["method"] == "source_parser:hacker_news_article"
    assert result["status"] == "ok"
    assert "First paragraph from the external article" in result["text"]
    assert "Navigation should be skipped" not in result["text"]
    assert "Article URL:" not in result["text"]


def test_extract_article_text_keeps_hacker_news_self_post_without_fetch(monkeypatch):
    monkeypatch.setattr(
        article_extractor,
        "_http_get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not fetch HN self post")),
    )

    result = article_extractor.extract_article_text(
        "https://news.ycombinator.com/item?id=1",
        "Hacker News",
        "https://hnrss.org/frontpage",
        {"summary": "Hi HN, " + ("we built a useful tool. " * 20)},
    )

    assert result["method"] == "rss_summary"
    assert result["status"] == "ok"
    assert "we built a useful tool" in result["text"]


def test_extract_article_text_keeps_hacker_news_link_stub_when_external_text_too_short(monkeypatch):
    monkeypatch.setattr(
        article_extractor,
        "_http_get",
        lambda *args, **kwargs: DummyResponse(text="<html><title>Only title</title></html>"),
    )

    summary = (
        '<p>Article URL: <a href="https://example.com/real-article">https://example.com/real-article</a></p>'
        '<p>Comments URL: <a href="https://news.ycombinator.com/item?id=1">https://news.ycombinator.com/item?id=1</a></p>'
        "<p>Points: 34</p><p># Comments: 17</p>"
    )
    result = article_extractor.extract_article_text(
        "https://example.com/real-article",
        "Hacker News",
        "https://hnrss.org/frontpage",
        {"summary": summary},
        min_length=40,
    )

    assert result["method"] == "rss_summary"
    assert result["status"] == "short"
    assert "Article URL:" in result["text"]
    assert "short text" in result["error"]
