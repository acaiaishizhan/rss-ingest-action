import socket

import pytest

import http_safety


class FakeResponse:
    def __init__(self, status_code=200, headers=None, chunks=None, encoding="utf-8"):
        self.status_code = status_code
        self.headers = headers or {}
        self.encoding = encoding
        self.apparent_encoding = encoding
        self._chunks = list(chunks or [])
        self.closed = False

    def iter_content(self, chunk_size=65536):
        yield from self._chunks

    def close(self):
        self.closed = True


class FakeBinaryResponse(FakeResponse):
    def __init__(self, status_code=200, headers=None, chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.encoding = None
        self._chunks = list(chunks or [])
        self.closed = False

    @property
    def apparent_encoding(self):
        raise RuntimeError("stream content must not be read for encoding detection")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def public_dns(host, port, type=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def test_fetch_public_content_rejects_hostname_resolving_to_private_ip(monkeypatch):
    monkeypatch.setattr(
        http_safety.socket,
        "getaddrinfo",
        lambda host, port, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port))],
    )
    monkeypatch.setattr(
        http_safety.requests,
        "Session",
        lambda: (_ for _ in ()).throw(AssertionError("unsafe URL must be rejected before HTTP")),
    )

    with pytest.raises(http_safety.UnsafeUrlError, match="non-public"):
        http_safety.fetch_public_content(
            "https://internal.example/article",
            headers={},
            timeout=3,
            max_bytes=1024,
            use_system_proxy=False,
        )


def test_fetch_public_content_revalidates_redirect_targets(monkeypatch):
    session = FakeSession(
        [FakeResponse(status_code=302, headers={"Location": "http://127.0.0.1/admin"})]
    )
    monkeypatch.setattr(http_safety.socket, "getaddrinfo", public_dns)
    monkeypatch.setattr(http_safety.requests, "Session", lambda: session)

    with pytest.raises(http_safety.UnsafeUrlError):
        http_safety.fetch_public_content(
            "https://example.com/start",
            headers={},
            timeout=3,
            max_bytes=1024,
            use_system_proxy=False,
        )

    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.calls[0][1]["stream"] is True


def test_fetch_public_content_stops_when_stream_exceeds_limit(monkeypatch):
    response = FakeResponse(status_code=200, chunks=[b"abcd", b"ef"])
    session = FakeSession([response])
    monkeypatch.setattr(http_safety.socket, "getaddrinfo", public_dns)
    monkeypatch.setattr(http_safety.requests, "Session", lambda: session)

    with pytest.raises(http_safety.ResponseTooLargeError, match="exceeds 5 bytes"):
        http_safety.fetch_public_content(
            "https://example.com/image.jpg",
            headers={},
            timeout=3,
            max_bytes=5,
            use_system_proxy=False,
        )

    assert response.closed is True


def test_fetch_public_content_does_not_probe_consumed_binary_stream_encoding(monkeypatch):
    response = FakeBinaryResponse(status_code=200, headers={"Content-Type": "image/jpeg"}, chunks=[b"jpg"])
    session = FakeSession([response])
    monkeypatch.setattr(http_safety.socket, "getaddrinfo", public_dns)
    monkeypatch.setattr(http_safety.requests, "Session", lambda: session)

    fetched = http_safety.fetch_public_content(
        "https://example.com/image.jpg",
        headers={},
        timeout=3,
        max_bytes=1024,
        use_system_proxy=False,
    )

    assert fetched.content == b"jpg"
    assert fetched.encoding == "utf-8"


def test_fetch_public_content_allows_allowlisted_proxy_fake_ip(monkeypatch):
    response = FakeResponse(status_code=200, headers={"Content-Type": "image/jpeg"}, chunks=[b"jpg"])
    session = FakeSession([response])
    monkeypatch.setattr(
        http_safety.socket,
        "getaddrinfo",
        lambda host, port, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.23", port))],
    )
    monkeypatch.setattr(http_safety.requests, "Session", lambda: session)

    fetched = http_safety.fetch_public_content(
        "https://pbs.twimg.com/media/example.jpg",
        headers={},
        timeout=3,
        max_bytes=1024,
        use_system_proxy=True,
        proxy_fake_ip_host_allowlist={"pbs.twimg.com"},
    )

    assert fetched.content == b"jpg"
    assert session.trust_env is True


def test_fetch_public_content_rejects_unlisted_proxy_fake_ip(monkeypatch):
    monkeypatch.setattr(
        http_safety.socket,
        "getaddrinfo",
        lambda host, port, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.23", port))],
    )

    with pytest.raises(http_safety.UnsafeUrlError, match="non-public"):
        http_safety.fetch_public_content(
            "https://untrusted.example/image.jpg",
            headers={},
            timeout=3,
            max_bytes=1024,
            use_system_proxy=True,
            proxy_fake_ip_host_allowlist={"pbs.twimg.com"},
        )


def test_fetch_public_content_rejects_literal_proxy_fake_ip(monkeypatch):
    monkeypatch.setattr(
        http_safety.requests,
        "Session",
        lambda: (_ for _ in ()).throw(AssertionError("literal fake IP must be rejected before HTTP")),
    )

    with pytest.raises(http_safety.UnsafeUrlError):
        http_safety.fetch_public_content(
            "https://198.18.0.23/image.jpg",
            headers={},
            timeout=3,
            max_bytes=1024,
            use_system_proxy=True,
            proxy_fake_ip_host_allowlist={"198.18.0.23"},
        )
