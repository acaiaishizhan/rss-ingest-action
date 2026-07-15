import pytest
from types import SimpleNamespace

import http_safety
import rss_ingest


class _JsonResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


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


def test_fetch_x_media_urls_keeps_photos_and_video_thumbnails_but_not_mp4(monkeypatch):
    payload = {
        "code": 200,
        "tweet": {
            "media": {
                "photos": [{"url": "https://pbs.twimg.com/media/photo.jpg"}],
                "videos": [
                    {
                        "url": "https://video.twimg.com/amplify_video/video.mp4",
                        "thumbnail_url": "https://pbs.twimg.com/amplify_video_thumb/video.jpg",
                    }
                ],
                "all": [
                    {"type": "video", "url": "https://video.twimg.com/amplify_video/video.mp4"},
                ],
            }
        },
    }
    monkeypatch.setattr(rss_ingest.requests, "get", lambda *args, **kwargs: _JsonResponse(payload))

    urls = rss_ingest.fetch_x_media_urls("https://x.com/example/status/123")

    assert urls == [
        "https://pbs.twimg.com/media/photo.jpg",
        "https://pbs.twimg.com/amplify_video_thumb/video.jpg",
    ]
    assert not any(url.endswith(".mp4") for url in urls)


def test_collect_x_images_keeps_feed_snapshot_when_live_lookup_is_empty(monkeypatch):
    monkeypatch.setattr(rss_ingest, "fetch_x_media_urls", lambda url: [])
    article = {
        "link": "https://x.com/example/status/123",
        "image_urls": ["https://pbs.twimg.com/media/snapshot.jpg"],
    }

    assert rss_ingest.collect_article_image_urls(article) == [
        "https://pbs.twimg.com/media/snapshot.jpg"
    ]
