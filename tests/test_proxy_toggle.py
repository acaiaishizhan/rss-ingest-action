import os
import sys
import calendar

import feedparser

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import feishu_client
import rss_ingest
import rss_parser


class DummySession:
    def __init__(self, response, capture):
        self.response = response
        self.capture = capture
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.capture["method"] = method
        self.capture["url"] = url
        self.capture["headers"] = headers
        self.capture["params"] = params
        self.capture["json"] = json
        self.capture["timeout"] = timeout
        self.capture["trust_env"] = self.trust_env
        return self.response

    def get(self, url, headers=None, params=None, timeout=None):
        return self.request("GET", url, headers=headers, params=params, timeout=timeout)

    def post(self, url, headers=None, json=None, timeout=None):
        return self.request("POST", url, headers=headers, json=json, timeout=timeout)

    def put(self, url, headers=None, json=None, timeout=None):
        return self.request("PUT", url, headers=headers, json=json, timeout=timeout)


class DummyResponse:
    def __init__(self, status_code=200, payload=None, text="", content=b"", headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self._payload


def test_feishu_client_ignores_system_proxy_when_disabled(monkeypatch):
    capture = {}
    response = DummyResponse(payload={"code": 0, "tenant_access_token": "tenant"})

    monkeypatch.setattr(feishu_client.config, "USE_SYSTEM_PROXY", False, raising=False)
    monkeypatch.setattr(feishu_client.requests, "post", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should use Session")))
    monkeypatch.setattr(feishu_client.requests, "Session", lambda: DummySession(response, capture))

    token = feishu_client.get_tenant_access_token("app", "secret", timeout=3, retries=1)

    assert token == "tenant"
    assert capture["trust_env"] is False
    assert capture["method"] == "POST"


def test_rss_parser_ignores_system_proxy_when_disabled(monkeypatch):
    capture = {}
    response = DummyResponse(status_code=200, content=b"<rss></rss>")

    monkeypatch.setattr(rss_parser.config, "USE_SYSTEM_PROXY", False, raising=False)
    monkeypatch.setattr(rss_parser.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should use Session")))
    monkeypatch.setattr(rss_parser.requests, "Session", lambda: DummySession(response, capture))
    monkeypatch.setattr(rss_parser.feedparser, "parse", lambda content: type("Feed", (), {"bozo": False, "entries": []})())

    feed = rss_parser.fetch_feed("https://example.com/rss", timeout=3, retries=1)

    assert feed.entries == []
    assert capture["trust_env"] is False
    assert capture["method"] == "GET"


def test_rss_parser_accepts_bozo_feed_when_entries_exist(monkeypatch):
    response = DummyResponse(status_code=200, content=b"<rss></rss>")

    monkeypatch.setattr(rss_parser, "_http_get", lambda *args, **kwargs: response)
    monkeypatch.setattr(
        rss_parser.feedparser,
        "parse",
        lambda content: type(
            "Feed",
            (),
            {
                "bozo": True,
                "bozo_exception": ValueError("broken xml"),
                "entries": [{"title": "still usable"}],
            },
        )(),
    )

    feed = rss_parser.fetch_feed("https://example.com/rss", timeout=3, retries=1)

    assert feed.entries == [{"title": "still usable"}]


def test_rss_parser_falls_back_to_jina_for_linux_do_cloudflare_challenge(monkeypatch):
    challenge = DummyResponse(
        status_code=403,
        text="<html><head><title>Just a moment...</title></head></html>",
        content=b"<html><head><title>Just a moment...</title></head></html>",
        headers={"cf-mitigated": "challenge", "content-type": "text/html; charset=UTF-8"},
    )
    jina_html = """
    <html><head><title>LINUX DO - Topics tagged 人工智能</title></head><body>
      <div>
        <h3><a href="https://linux.do/t/topic/2520455">求助帖！AI辅助编程 要讲什么呢？</a></h3>
        <a href="https://linux.do/t/topic/2520455">https://linux.do/t/topic/2520455</a><br>
        <time>Fri, 03 Jul 2026 12:11:08 +0000</time>
      </div>
      <div>
        <h3><a href="https://linux.do/t/topic/2520362">关于 GPT pro 降智</a></h3>
        <a href="https://linux.do/t/topic/2520362">https://linux.do/t/topic/2520362</a><br>
        <time>Fri, 03 Jul 2026 11:55:23 +0000</time>
      </div>
    </body></html>
    """
    jina = DummyResponse(status_code=200, text=jina_html, content=jina_html.encode("utf-8"))
    requested_urls = []
    requested_headers = []

    def fake_get(url, headers=None, timeout=None):
        requested_urls.append(url)
        requested_headers.append(headers or {})
        if url.startswith("https://r.jina.ai/https://"):
            return jina
        return challenge

    monkeypatch.setattr(rss_parser, "_http_get", fake_get)

    feed = rss_parser.fetch_feed("https://linux.do/tag/444-tag/444.rss", timeout=3, retries=1)

    assert requested_urls == [
        "https://linux.do/tag/444-tag/444.rss",
        "https://r.jina.ai/https://linux.do/tag/444-tag/444.rss",
    ]
    assert "x-engine" not in requested_headers[1]
    assert requested_headers[1]["x-respond-with"] == "html"
    assert feed.feed["title"] == "LINUX DO - Topics tagged 人工智能"
    assert len(feed.entries) == 2
    assert feed.entries[0]["title"] == "求助帖！AI辅助编程 要讲什么呢？"
    assert feed.entries[0]["link"] == "https://linux.do/t/topic/2520455"
    assert feed.entries[0]["published"] == "Fri, 03 Jul 2026 12:11:08 +0000"
    assert calendar.timegm(feed.entries[0]["published_parsed"]) == 1_783_080_668


def test_rss_parser_does_not_jina_fallback_for_non_linux_do_challenge(monkeypatch):
    challenge = DummyResponse(
        status_code=403,
        text="<html><head><title>Just a moment...</title></head></html>",
        content=b"<html><head><title>Just a moment...</title></head></html>",
        headers={"cf-mitigated": "challenge", "content-type": "text/html; charset=UTF-8"},
    )
    requested_urls = []

    def fake_get(url, headers=None, timeout=None):
        requested_urls.append(url)
        return challenge

    monkeypatch.setattr(rss_parser, "_http_get", fake_get)

    try:
        rss_parser.fetch_feed("https://example.com/feed.xml", timeout=3, retries=1)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert "HTTP 403" in message
    assert requested_urls == ["https://example.com/feed.xml"]


def test_rss_parser_falls_back_to_jina_for_linux_do_cloudflare_gateway_error(monkeypatch):
    gateway_error = DummyResponse(
        status_code=502,
        text="<html><head><title>502 Bad Gateway</title></head><body><center>cloudflare</center></body></html>",
        content=b"<html><head><title>502 Bad Gateway</title></head><body><center>cloudflare</center></body></html>",
        headers={"server": "cloudflare", "content-type": "text/html"},
    )
    jina_html = """
    <html><head><title>LINUX DO - Topics tagged 人工智能</title></head><body>
      <div>
        <h3><a href="https://linux.do/t/topic/2520455">求助帖！AI辅助编程 要讲什么呢？</a></h3>
        <time>Fri, 03 Jul 2026 12:11:08 +0000</time>
      </div>
    </body></html>
    """
    jina = DummyResponse(status_code=200, text=jina_html, content=jina_html.encode("utf-8"))

    def fake_get(url, headers=None, timeout=None):
        if url.startswith("https://r.jina.ai/https://"):
            return jina
        return gateway_error

    monkeypatch.setattr(rss_parser, "_http_get", fake_get)

    feed = rss_parser.fetch_feed("https://linux.do/tag/444-tag/444.rss", timeout=3, retries=1)

    assert len(feed.entries) == 1
    assert feed.entries[0]["link"] == "https://linux.do/t/topic/2520455"


def test_rss_parser_accepts_ithome_tag_html(monkeypatch):
    html = """
    <html><body>
      <ul class="bl">
        <li>
          <h2><a href="https://www.ithome.com/0/947/648.htm">400 万周活的 Codex 推出 Chrome 扩展</a></h2>
          <div class="c" data-ot="2026-05-08 12:34:56">2026-05-08 12:34:56</div>
        </li>
        <li>
          <h2><a href="/0/947/482.htm">OpenAI 最智能 AI 语音模型：GPT-Realtime-2 登场</a></h2>
          <div class="c" data-ot="2026-05-07 10:00:00">2026-05-07 10:00:00</div>
        </li>
      </ul>
    </body></html>
    """
    response = DummyResponse(status_code=200, text=html, content=html.encode("utf-8"))
    monkeypatch.setattr(rss_parser, "_http_get", lambda *args, **kwargs: response)

    feed = rss_parser.fetch_feed("https://www.ithome.com/tags/AI/", timeout=3, retries=1)

    assert len(feed.entries) == 2
    assert feed.entries[0]["id"] == "https://www.ithome.com/0/947/648.htm"
    assert feed.entries[0]["title"] == "400 万周活的 Codex 推出 Chrome 扩展"
    assert feed.entries[1]["link"] == "https://www.ithome.com/0/947/482.htm"
    assert feed.entries[0]["published_parsed"]
    assert calendar.timegm(feed.entries[0]["published_parsed"]) == 1_778_214_896


def test_rss_parser_reads_ithome_data_ot_iso_timestamp(monkeypatch):
    html = """
    <html><body>
      <a href="https://www.ithome.com/0/953/834.htm" target="_blank" class="img">
        <img alt="谷歌回应 Gemini 按 AI 算力计费变更，付费用户配额上调 3 倍" />
      </a>
      <div class="c" data-ot="2026-05-22T12:55:03.5130000+08:00">
        <h2>
          <a title="谷歌回应 Gemini 按 AI 算力计费变更，付费用户配额上调 3 倍"
             target="_blank"
             href="https://www.ithome.com/0/953/834.htm"
             class="title">谷歌回应 Gemini 按 AI 算力计费变更，付费用户配额上调 3 倍</a>
        </h2>
        <div class="d"><span class="date">05月22日</span></div>
      </div>
    </body></html>
    """
    response = DummyResponse(status_code=200, text=html, content=html.encode("utf-8"))
    monkeypatch.setattr(rss_parser, "_http_get", lambda *args, **kwargs: response)

    feed = rss_parser.fetch_feed("https://www.ithome.com/tags/Gemini/", timeout=3, retries=1)

    assert len(feed.entries) == 1
    assert feed.entries[0]["link"] == "https://www.ithome.com/0/953/834.htm"
    assert feed.entries[0]["published"] == "Fri, 22 May 2026 04:55:03 GMT"
    assert calendar.timegm(feed.entries[0]["published_parsed"]) == 1_779_425_703


def test_rss_ingest_ignores_system_proxy_when_disabled(monkeypatch):
    capture = {}
    response = DummyResponse(
        payload={
            "choices": [
                {
                    "message": {
                        "content": '{"action":"ingest","categories":["AI工具与自动化"],"score":8.0,"title_zh":"标题","qa":[{"question":"q1","answer":"a1"},{"question":"q2","answer":"a2"},{"question":"q3","answer":"a3"}]}'
                    }
                }
            ]
        }
    )

    monkeypatch.setattr(rss_ingest.config, "USE_SYSTEM_PROXY", False, raising=False)
    monkeypatch.setattr(rss_ingest.requests, "post", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should use Session")))
    monkeypatch.setattr(rss_ingest.requests, "Session", lambda: DummySession(response, capture))
    monkeypatch.setattr(rss_ingest.config, "DEEPSEEK_API_KEY", "deepseek-key", raising=False)
    monkeypatch.setattr(rss_ingest.config, "DEEPSEEK_BASE_URL", "https://api.deepseek.com", raising=False)
    monkeypatch.setattr(rss_ingest.config, "DEEPSEEK_MODEL", "deepseek-chat", raising=False)
    monkeypatch.setattr(rss_ingest.config, "DEEPSEEK_RETRIES", 1, raising=False)

    result = rss_ingest.analyze_with_deepseek_prompt(
        {"title": "t", "content": "c", "link": "https://example.com", "published": 0, "source": "src"},
        "screen prompt",
    )

    assert result["action"] == "ingest"
    assert capture["trust_env"] is False
    assert capture["method"] == "POST"


def test_feishu_client_wraps_non_json_response(monkeypatch):
    class NonJsonResponse(DummyResponse):
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(
        feishu_client,
        "http_post",
        lambda *args, **kwargs: NonJsonResponse(status_code=502, text="<html>bad gateway</html>"),
    )

    try:
        feishu_client.get_tenant_access_token("app", "secret", timeout=3, retries=1)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert "HTTP 502" in message
