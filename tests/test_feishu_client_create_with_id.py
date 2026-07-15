from feishu_client import create_bitable_record_with_id, upload_bitable_media


def test_create_bitable_record_with_id_signature():
    assert callable(create_bitable_record_with_id)


def test_upload_bitable_media_posts_multipart(monkeypatch):
    calls = []

    class DummyResponse:
        status_code = 200
        text = '{"code":0,"data":{"file_token":"box_token"}}'

        def json(self):
            return {"code": 0, "data": {"file_token": "box_token"}}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        calls.append(
            {
                "url": url,
                "headers": headers,
                "data": data,
                "files": files,
                "timeout": timeout,
            }
        )
        return DummyResponse()

    monkeypatch.setattr("feishu_client.requests.post", fake_post)
    monkeypatch.setattr("feishu_client.config.USE_SYSTEM_PROXY", True)

    token = upload_bitable_media(
        app_token="base_token",
        tenant_token="tenant_token",
        file_name="image.png",
        content=b"png-bytes",
        mime_type="image/png",
        timeout=10,
        retries=1,
    )

    assert token == "box_token"
    assert calls[0]["url"] == "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all"
    assert calls[0]["headers"]["Authorization"] == "Bearer tenant_token"
    assert calls[0]["data"] == {
        "file_name": "image.png",
        "parent_type": "bitable_image",
        "parent_node": "base_token",
        "size": "9",
    }
    assert calls[0]["files"]["file"] == ("image.png", b"png-bytes", "image/png")
