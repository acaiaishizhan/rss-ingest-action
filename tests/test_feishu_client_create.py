import uuid

import feishu_client


class DummyResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def test_create_record_with_id_uses_batch_create_and_uuid4_client_token(monkeypatch):
    captured = {}

    def fake_http_post(url, headers, json_body, timeout, retries, params=None):
        captured.update(url=url, body=json_body, params=params)
        return DummyResponse(
            {"code": 0, "data": {"records": [{"record_id": "rec-new", "fields": json_body["records"][0]["fields"]}]}}
        )

    monkeypatch.setattr(feishu_client, "http_post", fake_http_post)

    ok, record_id = feishu_client.create_bitable_record_with_id(
        "app", "table", "tenant", {"标题": "测试"}, timeout=3, retries=2
    )

    assert ok is True
    assert record_id == "rec-new"
    assert captured["url"].endswith("/records/batch_create")
    assert captured["body"] == {"records": [{"fields": {"标题": "测试"}}]}
    assert uuid.UUID(captured["params"]["client_token"]).version == 4


def test_http_post_retries_transient_feishu_business_code(monkeypatch):
    responses = [
        DummyResponse({"code": 1254291, "msg": "Write conflict"}),
        DummyResponse({"code": 0, "data": {"ok": True}}),
    ]
    calls = []
    monkeypatch.setattr(feishu_client, "_request", lambda *args, **kwargs: calls.append(args) or responses.pop(0))
    monkeypatch.setattr(feishu_client, "_sleep_backoff", lambda attempt, response=None: None)

    response = feishu_client.http_post(
        "https://open.feishu.cn/open-apis/example",
        {"Authorization": "Bearer token"},
        {"records": []},
        timeout=3,
        retries=2,
    )

    assert response.json()["code"] == 0
    assert len(calls) == 2


def test_http_post_retries_transient_http_status(monkeypatch):
    responses = [
        DummyResponse({"code": 0}, status_code=503),
        DummyResponse({"code": 0}, status_code=200),
    ]
    calls = []
    monkeypatch.setattr(feishu_client, "_request", lambda *args, **kwargs: calls.append(args) or responses.pop(0))
    monkeypatch.setattr(feishu_client, "_sleep_backoff", lambda attempt, response=None: None)

    response = feishu_client.http_post(
        "https://open.feishu.cn/open-apis/example",
        {},
        {},
        timeout=3,
        retries=2,
    )

    assert response.status_code == 200
    assert len(calls) == 2


def test_http_post_uses_feishu_retry_floor_and_raises_clear_error(monkeypatch):
    calls = []
    monkeypatch.setattr(feishu_client.config, "FEISHU_HTTP_RETRIES", 4, raising=False)
    monkeypatch.setattr(
        feishu_client,
        "_request",
        lambda *args, **kwargs: calls.append(args) or DummyResponse({}, status_code=503),
    )
    monkeypatch.setattr(feishu_client, "_sleep_backoff", lambda attempt, response=None: None)

    try:
        feishu_client.http_post(
            "https://open.feishu.cn/open-apis/example",
            {},
            {},
            timeout=3,
            retries=1,
        )
    except RuntimeError as exc:
        assert "transient POST response after 4 attempts" in str(exc)
    else:
        raise AssertionError("expected exhausted transient response to fail")

    assert len(calls) == 4
