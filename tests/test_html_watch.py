import time

import html_watch


def test_html_watch_returns_unchanged_on_304():
    source = {
        "record_id": "src-1",
        "name": "DeepSeek Updates",
        "feed_url": "https://api-docs.deepseek.com/updates",
        "watch_state": '{"etag": "abc", "last_modified": "Tue, 12 May 2026 01:00:00 GMT"}',
    }

    captured = {}

    def fake_get(url, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        return html_watch.HtmlResponse(
            status_code=304,
            url=url,
            text="",
            headers={},
        )

    result = html_watch.fetch_html_watch(source, now_ms=1_000_000, http_get=fake_get)

    assert result.status == "unchanged"
    assert result.entries == []
    assert captured["headers"]["If-None-Match"] == "abc"
    assert captured["headers"]["If-Modified-Since"] == "Tue, 12 May 2026 01:00:00 GMT"


def test_html_watch_parses_deepseek_updates_as_entries():
    source = {
        "record_id": "src-1",
        "name": "DeepSeek Updates",
        "feed_url": "https://api-docs.deepseek.com/updates",
        "watch_state": "",
    }
    html = """
    <html><body>
      <a href="/news/news260424">DeepSeek V4 Preview Release</a>
      <p>2026-04-24</p>
      <a href="/news/news260101">DeepSeek API Pricing Update</a>
      <p>2026-01-01</p>
    </body></html>
    """

    def fake_get(url, headers, timeout):
        return html_watch.HtmlResponse(
            status_code=200,
            url=url,
            text=html,
            headers={"ETag": "new-etag", "Last-Modified": "Tue, 12 May 2026 01:00:00 GMT"},
        )

    result = html_watch.fetch_html_watch(source, now_ms=1_000_000, http_get=fake_get)

    assert result.status == "ok"
    assert [entry["title"] for entry in result.entries] == [
        "DeepSeek V4 Preview Release",
        "DeepSeek API Pricing Update",
    ]
    assert result.entries[0]["link"] == "https://api-docs.deepseek.com/news/news260424"
    assert result.entries[0]["id"] == "https://api-docs.deepseek.com/news/news260424"
    assert result.entries[0]["published"] == "Fri, 24 Apr 2026 00:00:00 GMT"
    assert result.watch_state["etag"] == "new-etag"
    assert result.watch_state["last_modified"] == "Tue, 12 May 2026 01:00:00 GMT"


def test_html_watch_rate_limits_with_retry_after():
    source = {
        "record_id": "src-1",
        "name": "DeepSeek Updates",
        "feed_url": "https://api-docs.deepseek.com/updates",
        "watch_state": "",
    }
    now_ms = int(time.time() * 1000)

    def fake_get(url, headers, timeout):
        return html_watch.HtmlResponse(
            status_code=429,
            url=url,
            text="too many requests",
            headers={"Retry-After": "120"},
        )

    result = html_watch.fetch_html_watch(source, now_ms=now_ms, http_get=fake_get)

    assert result.status == "rate_limited"
    assert result.entries == []
    assert result.watch_state["backoff_until"] == now_ms + 120_000
    assert "HTTP 429" in result.watch_state["last_error"]


def test_deepseek_parser_ignores_navigation_links():
    html = """
    <a href="/api/deepseek-api">API Reference</a>
    <a href="/news/news260424">DeepSeek V4 Preview Release</a>
    <p>2026-04-24</p>
    """

    entries = html_watch.parse_html_entries(html, "https://api-docs.deepseek.com/updates")

    assert [entry["title"] for entry in entries] == ["DeepSeek V4 Preview Release"]


def test_dated_release_note_parsers_emit_one_entry_per_date():
    claude_html = """
    <h3><div id="july-10-2026">July 10, 2026</div></h3>
    <ul><li>Added expiring API keys.</li></ul>
    <h3><div id="july-8-2026">July 8, 2026</div></h3>
    <p>Released a new SDK.</p>
    """
    gemini_html = """
    <h2 id="07-06-2026">July 6, 2026</h2>
    <p>Developer logs now support the Interactions API.</p>
    """

    claude = html_watch.parse_html_entries(
        claude_html,
        "https://platform.claude.com/docs/en/release-notes/overview",
    )
    gemini = html_watch.parse_html_entries(
        gemini_html,
        "https://ai.google.dev/gemini-api/docs/changelog",
    )

    assert len(claude) == 2
    assert claude[0]["published"] == "Fri, 10 Jul 2026 00:00:00 GMT"
    assert claude[0]["link"].endswith("#july-10-2026")
    assert claude[0]["id"].startswith("html:")
    assert len(gemini) == 1
    assert "Developer logs" in gemini[0]["title"]


def test_xai_and_mistral_parsers_preserve_release_dates():
    xai_html = """
    <h2 id="july">July</h2>
    <p>July 8</p>
    <h3 id="grok-45">Grok 4.5</h3>
    <p>Grok 4.5 is now available on the API.</p>
    <h2 id="june">June</h2>
    <p>June 15</p>
    <h3 id="priority">Priority Processing</h3>
    <p>Priority processing is now available.</p>
    """
    mistral_html = """
    <h2>June 30</h2><p>We released Leanstral 1.5.</p>
    <h2>June 23</h2><p>We released OCR 4.</p>
    """

    xai = html_watch.parse_html_entries(xai_html, "https://docs.x.ai/developers/release-notes")
    mistral = html_watch.parse_html_entries(mistral_html, "https://docs.mistral.ai/resources/changelogs")

    assert [entry["published"] for entry in xai] == [
        "Wed, 08 Jul 2026 00:00:00 GMT",
        "Mon, 15 Jun 2026 00:00:00 GMT",
    ]
    assert xai[0]["title"] == "xAI API 更新：Grok 4.5"
    assert mistral[0]["published"] == "Tue, 30 Jun 2026 00:00:00 GMT"
    assert "Leanstral 1.5" in mistral[0]["title"]


def test_minimax_and_tencent_parsers_emit_actionable_entries():
    minimax_md = """
    # Changelog
    ## v3.0.47
    ### Bug Fixes
    * Fixed proxy recovery.
    ## v3.0.46
    ### New Features
    * Added worktree picker.
    """
    tencent_html = """
    <table><tbody><tr>
      <td>Tencent HY 旧版本模型下线</td>
      <td>建议迁移至 TokenHub 使用最新版本模型。</td>
      <td>2026-06-22</td>
      <td><a href="/document/product/1729/131925">查看详情</a></td>
    </tr></tbody></table>
    """

    minimax = html_watch.parse_html_entries(minimax_md, "https://agent.minimax.io/docs/changelog.md")
    tencent = html_watch.parse_html_entries(tencent_html, "https://cloud.tencent.com/document/product/1729/97765")

    assert [entry["title"] for entry in minimax] == ["MiniMax Agent v3.0.47", "MiniMax Agent v3.0.46"]
    assert tencent[0]["title"] == "腾讯混元更新：Tencent HY 旧版本模型下线"
    assert tencent[0]["link"] == "https://cloud.tencent.com/document/product/1729/131925"


def test_aliyun_model_lifecycle_parser_ignores_navigation():
    html = """
    <a href="/zh/model-studio/model-user-guide/">用户指南（模型）</a>
    <table><tbody><tr>
      <td>文字提取</td><td>2026-06-16</td><td>中国内地</td><td>qwen3.5-ocr</td>
      <td>千问文字提取模型。<a href="/zh/model-studio/qwen-vl-ocr">文字提取</a></td>
    </tr></tbody></table>
    """

    entries = html_watch.parse_html_entries(
        html,
        "https://help.aliyun.com/zh/model-studio/newly-released-models",
    )

    assert len(entries) == 1
    assert entries[0]["title"] == "阿里云百炼模型更新：qwen3.5-ocr"
    assert entries[0]["published"] == "Tue, 16 Jun 2026 00:00:00 GMT"
    assert entries[0]["link"] == "https://help.aliyun.com/zh/model-studio/qwen-vl-ocr"


def test_markdown_and_card_parsers_use_small_official_endpoints():
    claude_md = """
    # Claude Platform
    ### July 10, 2026
    * Added expiring API keys.
    ### July 8, 2026
    * Released a new SDK.
    """
    xai_md = """
    # Release Notes
    ## July
    ### Grok 4.5
    Grok 4.5 is now available on the API.
    ## June
    ### Priority Processing
    Priority processing is now available.
    """
    artificial_html = """
    <a class="relative flex" href="/articles/model-a">
      <h3>Model A benchmark</h3><p>July 10, 2026</p>
    </a>
    """
    metr_html = """
    <div class="blog-post-card"><div><a href="/blog/model-b/">
      <div class="card-title">Model B evaluation</div>
      <div class="card-date">June 26, 2026</div>
      <div class="card-description"><p>An independent evaluation.</p></div>
    </a></div></div></div>
    """

    claude = html_watch.parse_html_entries(claude_md, "https://platform.claude.com/docs/en/release-notes/overview.md")
    xai = html_watch.parse_html_entries(xai_md, "https://docs.x.ai/developers/release-notes.md")
    artificial = html_watch.parse_html_entries(artificial_html, "https://artificialanalysis.ai/articles")
    metr = html_watch.parse_html_entries(metr_html, "https://metr.org/blog/")

    assert len(claude) == 2
    assert claude[0]["link"].endswith("#july-10-2026")
    assert [entry["title"] for entry in xai] == ["xAI API 更新：Grok 4.5"]
    assert artificial[0]["published"] == "Fri, 10 Jul 2026 00:00:00 GMT"
    assert metr[0]["title"] == "METR：Model B evaluation"
