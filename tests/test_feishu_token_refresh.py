# -*- coding: utf-8 -*-
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import feishu_client


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(feishu_client.time, "time", fake)
    return fake


def _install_token_responses(monkeypatch, responses, calls):
    def fake_http_post(url, headers, json_body, timeout, retries, params=None):
        calls.append({"url": url, "json_body": json_body})
        payload = responses[min(len(calls) - 1, len(responses) - 1)]
        return FakeResponse(payload)

    monkeypatch.setattr(feishu_client, "http_post", fake_http_post)


def test_token_interpolates_into_bearer_header(monkeypatch, clock):
    calls = []
    _install_token_responses(
        monkeypatch,
        [{"code": 0, "tenant_access_token": "t1", "expire": 7200}],
        calls,
    )

    token = feishu_client.get_tenant_access_token("app", "secret", timeout=3, retries=1)

    assert f"Bearer {token}" == "Bearer t1"
    assert len(calls) == 1


def test_token_reused_within_validity(monkeypatch, clock):
    calls = []
    _install_token_responses(
        monkeypatch,
        [
            {"code": 0, "tenant_access_token": "t1", "expire": 7200},
            {"code": 0, "tenant_access_token": "t2", "expire": 7200},
        ],
        calls,
    )

    token = feishu_client.get_tenant_access_token("app", "secret", timeout=3, retries=1)
    clock.advance(600)

    assert f"{token}" == "t1"
    assert f"{token}" == "t1"
    assert len(calls) == 1


def test_token_refreshes_near_expiry(monkeypatch, clock):
    calls = []
    _install_token_responses(
        monkeypatch,
        [
            {"code": 0, "tenant_access_token": "t1", "expire": 7200},
            {"code": 0, "tenant_access_token": "t2", "expire": 7200},
        ],
        calls,
    )

    token = feishu_client.get_tenant_access_token("app", "secret", timeout=3, retries=1)
    assert f"{token}" == "t1"

    # 越过 expire - 刷新余量（600s）的边界后，插值应拿到新 token
    clock.advance(7200 - 300)

    assert f"Bearer {token}" == "Bearer t2"
    assert len(calls) == 2


def test_token_refresh_honors_short_expire_from_feishu(monkeypatch, clock):
    # Feishu 对仍在有效期内的应用返回同一个 token 和剩余秒数，
    # 长跑任务开跑时就可能拿到只剩几十分钟的 token。
    calls = []
    _install_token_responses(
        monkeypatch,
        [
            {"code": 0, "tenant_access_token": "t1", "expire": 1800},
            {"code": 0, "tenant_access_token": "t2", "expire": 7200},
        ],
        calls,
    )

    token = feishu_client.get_tenant_access_token("app", "secret", timeout=3, retries=1)
    clock.advance(1500)

    assert f"{token}" == "t2"
    assert len(calls) == 2


def test_token_fetch_is_eager_and_raises_on_auth_error(monkeypatch, clock):
    calls = []
    _install_token_responses(
        monkeypatch,
        [{"code": 99991663, "msg": "invalid app credentials"}],
        calls,
    )

    with pytest.raises(RuntimeError):
        feishu_client.get_tenant_access_token("app", "secret", timeout=3, retries=1)


def test_token_compares_equal_to_current_string(monkeypatch, clock):
    calls = []
    _install_token_responses(
        monkeypatch,
        [{"code": 0, "tenant_access_token": "t1", "expire": 7200}],
        calls,
    )

    token = feishu_client.get_tenant_access_token("app", "secret", timeout=3, retries=1)

    assert token == "t1"
    assert not (token != "t1")


def test_token_missing_expire_defaults_to_safe_window(monkeypatch, clock):
    calls = []
    _install_token_responses(
        monkeypatch,
        [
            {"code": 0, "tenant_access_token": "t1"},
            {"code": 0, "tenant_access_token": "t2", "expire": 7200},
        ],
        calls,
    )

    token = feishu_client.get_tenant_access_token("app", "secret", timeout=3, retries=1)

    # 没有 expire 字段时按短窗口保守处理，但至少当次可用
    assert f"{token}" == "t1"
    assert len(calls) == 1
