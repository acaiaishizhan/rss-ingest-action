import pytest
from types import SimpleNamespace

import http_safety
import rss_ingest


def test_aipoju_tencent_bucket_is_in_proxy_fake_ip_allowlist():
    assert (
        "breakout-1301344553.cos.ap-beijing.myqcloud.com"
        in rss_ingest.config.IMAGE_ATTACHMENT_PROXY_FAKE_IP_HOSTS
    )


def test_image_download_rejects_private_url_before_requests(monkeypatch):
    monkeypatch.setattr(
        rss_ingest.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unsafe URL reached requests")),
    )

    with pytest.raises(http_safety.UnsafeUrlError):
        rss_ingest.download_image_for_attachment("http://127.0.0.1/private.jpg")


def test_image_download_passes_proxy_fake_ip_host_allowlist(monkeypatch):
    captured = {}

    def fake_fetch(url, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "image/jpeg"},
            content=b"jpg",
            text="",
        )

    monkeypatch.setattr(rss_ingest.config, "IMAGE_ATTACHMENT_PROXY_FAKE_IP_HOSTS", {"pbs.twimg.com"}, raising=False)
    monkeypatch.setattr(rss_ingest, "fetch_public_content", fake_fetch)

    _, content, mime = rss_ingest.download_image_for_attachment(
        "https://pbs.twimg.com/media/example.jpg"
    )

    assert content == b"jpg"
    assert mime == "image/jpeg"
    assert captured["proxy_fake_ip_host_allowlist"] == {"pbs.twimg.com"}
