import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import feishu_client


class DummyResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = ""

    def json(self):
        return self._payload


def test_list_bitable_records_sends_pagination_as_query_params(monkeypatch):
    calls = []

    def fake_http_post(url, headers, json_body, timeout, retries, params=None):
        calls.append({"body": json_body, "params": params})
        if len(calls) == 1:
            return DummyResponse(
                {
                    "code": 0,
                    "data": {
                        "items": [{"record_id": "rec1"}],
                        "has_more": True,
                        "page_token": "next-token",
                    },
                }
            )
        return DummyResponse(
            {
                "code": 0,
                "data": {
                    "items": [{"record_id": "rec2"}],
                    "has_more": False,
                },
            }
        )

    monkeypatch.setattr(feishu_client, "http_post", fake_http_post)

    records = feishu_client.list_bitable_records(
        "app",
        "table",
        "tenant",
        timeout=3,
        retries=1,
        page_size=123,
        max_pages=2,
        sort=[{"field_name": "创建时间", "desc": True}],
    )

    assert [record["record_id"] for record in records] == ["rec1", "rec2"]
    assert calls[0]["params"] == {"page_size": 123}
    assert calls[1]["params"] == {"page_size": 123, "page_token": "next-token"}
    assert calls[0]["body"] == {"sort": [{"field_name": "创建时间", "desc": True}]}
    assert "page_token" not in calls[1]["body"]


def test_list_bitable_records_raises_when_max_pages_truncates_results(monkeypatch):
    monkeypatch.setattr(
        feishu_client,
        "http_post",
        lambda *args, **kwargs: DummyResponse(
            {
                "code": 0,
                "data": {
                    "items": [{"record_id": "rec1"}],
                    "has_more": True,
                    "page_token": "still-more",
                },
            }
        ),
    )

    with pytest.raises(feishu_client.PaginationLimitError, match="max_pages=1"):
        feishu_client.list_bitable_records(
            "app", "table", "tenant", timeout=3, retries=1, page_size=500, max_pages=1
        )


def test_list_bitable_records_can_explicitly_accept_partial_results(monkeypatch):
    monkeypatch.setattr(
        feishu_client,
        "http_post",
        lambda *args, **kwargs: DummyResponse(
            {
                "code": 0,
                "data": {
                    "items": [{"record_id": "rec1"}],
                    "has_more": True,
                    "page_token": "still-more",
                },
            }
        ),
    )

    records = feishu_client.list_bitable_records(
        "app",
        "table",
        "tenant",
        timeout=3,
        retries=1,
        page_size=500,
        max_pages=1,
        allow_partial=True,
    )

    assert records == [{"record_id": "rec1"}]


def test_batch_update_bitable_records_posts_records_body(monkeypatch):
    captured = {}

    def fake_http_post(url, headers, json_body, timeout, retries, params=None):
        captured["url"] = url
        captured["body"] = json_body
        captured["params"] = params
        return DummyResponse({"code": 0, "data": {"records": []}})

    monkeypatch.setattr(feishu_client, "http_post", fake_http_post)

    ok, payload = feishu_client.batch_update_bitable_records(
        "app",
        "table",
        "tenant",
        [{"record_id": "rec1", "fields": {"NEWS次数": 1}}],
        timeout=3,
        retries=1,
    )

    assert ok is True
    assert payload["code"] == 0
    assert captured["url"].endswith("/records/batch_update")
    assert captured["body"] == {"records": [{"record_id": "rec1", "fields": {"NEWS次数": 1}}]}
    assert captured["params"] is None


def test_update_bitable_field_puts_field_name(monkeypatch):
    captured = {}

    def fake_http_put(url, headers, json_body, timeout, retries):
        captured["url"] = url
        captured["body"] = json_body
        return DummyResponse({"code": 0, "data": {"field": {"field_name": "24h 本期"}}})

    monkeypatch.setattr(feishu_client, "http_put", fake_http_put)

    ok, payload = feishu_client.update_bitable_field(
        "app",
        "table",
        "field",
        "tenant",
        timeout=3,
        retries=1,
        field_name="24h 本期",
        field_type=20,
    )

    assert ok is True
    assert payload["code"] == 0
    assert captured["url"].endswith("/fields/field")
    assert captured["body"] == {"field_name": "24h 本期", "type": 20}
