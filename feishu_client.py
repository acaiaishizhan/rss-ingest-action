# -*- coding: utf-8 -*-
import random
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import requests

import config


TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
TRANSIENT_FEISHU_CODES = {1254002, 1254290, 1254291, 1254607, 1255001, 1255002}


class PaginationLimitError(RuntimeError):
    pass


class FeishuTransientError(RuntimeError):
    pass


def _effective_retries(retries: int) -> int:
    configured = int(getattr(config, "FEISHU_HTTP_RETRIES", retries) or retries)
    return max(1, int(retries), configured)


def _retry_after_seconds(resp: Optional[requests.Response]) -> float:
    if resp is None:
        return 0.0
    raw = str((getattr(resp, "headers", {}) or {}).get("Retry-After") or "").strip()
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def _sleep_backoff(attempt: int, resp: Optional[requests.Response] = None) -> None:
    base = max(0.0, float(getattr(config, "FEISHU_RETRY_BASE_SECONDS", 1.5) or 1.5))
    cap = max(base, float(getattr(config, "FEISHU_RETRY_MAX_SECONDS", 30.0) or 30.0))
    jittered = base * (2 ** attempt) + random.random() * min(1.0, base)
    time.sleep(min(cap, max(_retry_after_seconds(resp), jittered)))


def _response_snippet(resp: requests.Response, limit: int = 300) -> str:
    text = (getattr(resp, "text", "") or "").strip()
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return f"HTTP {getattr(resp, 'status_code', 'unknown')}: {text}"


def _safe_json(resp: requests.Response) -> Dict[str, Any]:
    try:
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"[Feishu] non-JSON response: {_response_snippet(resp)}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"[Feishu] invalid JSON payload: {_response_snippet(resp)}")
    return data


def _response_is_transient(resp: requests.Response) -> bool:
    if int(getattr(resp, "status_code", 0) or 0) in TRANSIENT_HTTP_STATUSES:
        return True
    try:
        data = resp.json()
    except Exception:
        return False
    try:
        return int(data.get("code") or 0) in TRANSIENT_FEISHU_CODES if isinstance(data, dict) else False
    except (TypeError, ValueError):
        return False


def _request(
    method: str,
    url: str,
    headers: Dict[str, str],
    timeout: int,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> requests.Response:
    if config.USE_SYSTEM_PROXY:
        if method == "GET":
            return requests.get(url, headers=headers, params=params, timeout=timeout)
        if method == "POST":
            return requests.post(url, headers=headers, params=params, json=json_body, timeout=timeout)
        if method == "PUT":
            return requests.put(url, headers=headers, params=params, json=json_body, timeout=timeout)
        raise ValueError(f"unsupported method: {method}")

    with requests.Session() as sess:
        sess.trust_env = False
        return sess.request(method, url, headers=headers, params=params, json=json_body, timeout=timeout)


def _request_multipart(
    url: str,
    headers: Dict[str, str],
    timeout: int,
    data: Dict[str, Any],
    files: Dict[str, Any],
) -> requests.Response:
    if config.USE_SYSTEM_PROXY:
        return requests.post(url, headers=headers, data=data, files=files, timeout=timeout)

    with requests.Session() as sess:
        sess.trust_env = False
        return sess.post(url, headers=headers, data=data, files=files, timeout=timeout)


def http_get(url: str, headers: Dict[str, str], timeout: int, retries: int, params: Optional[Dict[str, Any]] = None) -> requests.Response:
    last_err: Optional[Exception] = None
    attempts = _effective_retries(retries)
    for i in range(attempts):
        try:
            resp = _request("GET", url, headers, timeout, params=params)
            if _response_is_transient(resp):
                if i < attempts - 1:
                    _sleep_backoff(i, resp)
                    continue
                raise FeishuTransientError(
                    f"[Feishu] transient GET response after {attempts} attempts: {_response_snippet(resp)}"
                )
            return resp
        except Exception as exc:
            last_err = exc
            if isinstance(exc, FeishuTransientError):
                break
            if i < attempts - 1:
                _sleep_backoff(i)
    raise RuntimeError(f"HTTP GET failed after retries: {last_err}")


def http_post(
    url: str,
    headers: Dict[str, str],
    json_body: Dict[str, Any],
    timeout: int,
    retries: int,
    params: Optional[Dict[str, Any]] = None,
) -> requests.Response:
    last_err: Optional[Exception] = None
    attempts = _effective_retries(retries)
    for i in range(attempts):
        try:
            resp = _request("POST", url, headers, timeout, params=params, json_body=json_body)
            if _response_is_transient(resp):
                if i < attempts - 1:
                    _sleep_backoff(i, resp)
                    continue
                raise FeishuTransientError(
                    f"[Feishu] transient POST response after {attempts} attempts: {_response_snippet(resp)}"
                )
            return resp
        except Exception as exc:
            last_err = exc
            if isinstance(exc, FeishuTransientError):
                break
            if i < attempts - 1:
                _sleep_backoff(i)
    raise RuntimeError(f"HTTP POST failed after retries: {last_err}")


def http_post_multipart(
    url: str,
    headers: Dict[str, str],
    data: Dict[str, Any],
    files: Dict[str, Any],
    timeout: int,
    retries: int,
) -> requests.Response:
    last_err: Optional[Exception] = None
    attempts = _effective_retries(retries)
    for i in range(attempts):
        try:
            resp = _request_multipart(url, headers, timeout, data=data, files=files)
            if _response_is_transient(resp):
                if i < attempts - 1:
                    _sleep_backoff(i, resp)
                    continue
                raise FeishuTransientError(
                    f"[Feishu] transient multipart response after {attempts} attempts: {_response_snippet(resp)}"
                )
            return resp
        except Exception as exc:
            last_err = exc
            if isinstance(exc, FeishuTransientError):
                break
            if i < attempts - 1:
                _sleep_backoff(i)
    raise RuntimeError(f"HTTP multipart POST failed after retries: {last_err}")


def http_put(url: str, headers: Dict[str, str], json_body: Dict[str, Any], timeout: int, retries: int) -> requests.Response:
    last_err: Optional[Exception] = None
    attempts = _effective_retries(retries)
    for i in range(attempts):
        try:
            resp = _request("PUT", url, headers, timeout, json_body=json_body)
            if _response_is_transient(resp):
                if i < attempts - 1:
                    _sleep_backoff(i, resp)
                    continue
                raise FeishuTransientError(
                    f"[Feishu] transient PUT response after {attempts} attempts: {_response_snippet(resp)}"
                )
            return resp
        except Exception as exc:
            last_err = exc
            if isinstance(exc, FeishuTransientError):
                break
            if i < attempts - 1:
                _sleep_backoff(i)
    raise RuntimeError(f"HTTP PUT failed after retries: {last_err}")


class TenantAccessToken:
    """Auto-refreshing tenant access token.

    Feishu returns the *remaining* validity in ``expire``: a job that starts
    right after another process fetched the same app's token can receive one
    with only minutes left. Interpolating this object (``f"Bearer {token}"``)
    always yields a token with at least REFRESH_MARGIN_SECONDS of validity.
    """

    REFRESH_MARGIN_SECONDS = 600.0
    DEFAULT_EXPIRE_SECONDS = 1800.0

    def __init__(self, app_id: str, app_secret: str, timeout: int, retries: int):
        self._app_id = app_id
        self._app_secret = app_secret
        self._timeout = timeout
        self._retries = retries
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = 0.0
        with self._lock:
            self._refresh_locked()

    def _refresh_locked(self) -> None:
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        payload = {"app_id": self._app_id, "app_secret": self._app_secret}
        headers = {"Content-Type": "application/json; charset=utf-8"}
        resp = http_post(url, headers, payload, self._timeout, self._retries)
        data = _safe_json(resp)
        if data.get("code") != 0:
            raise RuntimeError(f"[Feishu] token error: {data}")
        token = data.get("tenant_access_token")
        if not token:
            raise RuntimeError(f"[Feishu] token missing: {data}")
        try:
            expire = float(data.get("expire"))
        except (TypeError, ValueError):
            expire = self.DEFAULT_EXPIRE_SECONDS
        self._token = token
        self._expires_at = time.time() + expire

    def current(self) -> str:
        with self._lock:
            if time.time() >= self._expires_at - self.REFRESH_MARGIN_SECONDS:
                self._refresh_locked()
            return self._token

    def __str__(self) -> str:
        return self.current()

    def __format__(self, format_spec: str) -> str:
        return format(self.current(), format_spec)

    def __eq__(self, other: object):
        if isinstance(other, str):
            return self.current() == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.current())

    def __repr__(self) -> str:
        return "<TenantAccessToken ****>"


def get_tenant_access_token(app_id: str, app_secret: str, timeout: int, retries: int) -> TenantAccessToken:
    return TenantAccessToken(app_id, app_secret, timeout, retries)


def list_bitable_fields(
    app_token: str,
    table_id: str,
    tenant_token: str,
    timeout: int,
    retries: int,
    page_size: int = 200,
    max_pages: int = 20,
) -> List[Dict[str, Any]]:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    headers = {
        "Authorization": f"Bearer {tenant_token}",
    }

    items: List[Dict[str, Any]] = []
    page_token: Optional[str] = None

    for _ in range(max_pages):
        params: Dict[str, Any] = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        resp = http_get(url, headers, timeout, retries, params=params)
        data = _safe_json(resp)
        if data.get("code") != 0:
            raise RuntimeError(f"[Feishu] list fields error: {data}")
        data_block = data.get("data") or {}
        items.extend(data_block.get("items") or [])
        if not data_block.get("has_more"):
            break
        page_token = data_block.get("page_token")
        if not page_token:
            break

    return items


def create_bitable_field(
    app_token: str,
    table_id: str,
    tenant_token: str,
    field_name: str,
    field_type: int,
    timeout: int,
    retries: int,
    field_property: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Dict[str, Any]]:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    headers = {
        "Authorization": f"Bearer {tenant_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    body: Dict[str, Any] = {
        "field_name": field_name,
        "type": field_type,
    }
    if field_property:
        body["property"] = field_property

    resp = http_post(url, headers, body, timeout, retries)
    data = _safe_json(resp)
    if data.get("code") != 0:
        return False, data
    return True, data


def update_bitable_field(
    app_token: str,
    table_id: str,
    field_id: str,
    tenant_token: str,
    timeout: int,
    retries: int,
    field_name: Optional[str] = None,
    field_type: Optional[int] = None,
    field_property: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Dict[str, Any]]:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{field_id}"
    headers = {
        "Authorization": f"Bearer {tenant_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    body: Dict[str, Any] = {}
    if field_name:
        body["field_name"] = field_name
    if field_type is not None:
        body["type"] = field_type
    if field_property:
        body["property"] = field_property

    resp = http_put(url, headers, body, timeout, retries)
    data = _safe_json(resp)
    if data.get("code") != 0:
        return False, data
    return True, data


def list_bitable_tables(
    app_token: str,
    tenant_token: str,
    timeout: int,
    retries: int,
    page_size: int = 100,
    max_pages: int = 20,
    allow_partial: bool = False,
) -> List[Dict[str, Any]]:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables"
    headers = {
        "Authorization": f"Bearer {tenant_token}",
    }

    items: List[Dict[str, Any]] = []
    page_token: Optional[str] = None

    has_more = False
    for _ in range(max_pages):
        params: Dict[str, Any] = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        resp = http_get(url, headers, timeout, retries, params=params)
        data = _safe_json(resp)
        if data.get("code") != 0:
            raise RuntimeError(f"[Feishu] list tables error: {data}")
        data_block = data.get("data") or {}
        items.extend(data_block.get("items") or [])
        has_more = bool(data_block.get("has_more"))
        if not has_more:
            break
        page_token = data_block.get("page_token")
        if not page_token:
            break

    if has_more and not allow_partial:
        raise PaginationLimitError(f"[Feishu] list tables exceeded max_pages={max_pages}")
    return items


def list_bitable_records(
    app_token: str,
    table_id: str,
    tenant_token: str,
    timeout: int,
    retries: int,
    page_size: int = 500,
    max_pages: int = 50,
    filter_obj: Optional[Dict[str, Any]] = None,
    sort: Optional[List[Dict[str, Any]]] = None,
    allow_partial: bool = False,
) -> List[Dict[str, Any]]:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search"
    headers = {
        "Authorization": f"Bearer {tenant_token}",
        "Content-Type": "application/json; charset=utf-8",
    }

    items: List[Dict[str, Any]] = []
    page_token: Optional[str] = None

    has_more = False
    for _ in range(max_pages):
        params: Dict[str, Any] = {"page_size": page_size}
        body: Dict[str, Any] = {}
        if page_token:
            params["page_token"] = page_token
        if filter_obj:
            body["filter"] = filter_obj
        if sort:
            body["sort"] = sort

        resp = http_post(url, headers, body, timeout, retries, params=params)
        data = _safe_json(resp)
        if data.get("code") != 0:
            raise RuntimeError(f"[Feishu] list records error: {data}")

        data_block = data.get("data") or {}
        items.extend(data_block.get("items") or [])
        has_more = bool(data_block.get("has_more"))
        if not has_more:
            break
        page_token = data_block.get("page_token")
        if not page_token:
            break

    if has_more and not allow_partial:
        raise PaginationLimitError(
            f"[Feishu] list records table={table_id} exceeded max_pages={max_pages}"
        )
    return items


def update_bitable_record_fields(
    app_token: str,
    table_id: str,
    tenant_token: str,
    record_id: str,
    fields: Dict[str, Any],
    timeout: int,
    retries: int,
) -> bool:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
    headers = {
        "Authorization": f"Bearer {tenant_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    body = {"fields": fields}
    resp = http_put(url, headers, body, timeout, retries)
    data = _safe_json(resp)
    if data.get("code") != 0:
        return False
    return True


def batch_update_bitable_records(
    app_token: str,
    table_id: str,
    tenant_token: str,
    records: List[Dict[str, Any]],
    timeout: int,
    retries: int,
) -> Tuple[bool, Dict[str, Any]]:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update"
    headers = {
        "Authorization": f"Bearer {tenant_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    body = {"records": records}
    resp = http_post(url, headers, body, timeout, retries)
    data = _safe_json(resp)
    if data.get("code") != 0:
        return False, data
    return True, data


def batch_delete_bitable_records(
    app_token: str,
    table_id: str,
    tenant_token: str,
    record_ids: List[str],
    timeout: int,
    retries: int,
) -> Tuple[bool, Dict[str, Any]]:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete"
    headers = {
        "Authorization": f"Bearer {tenant_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    body = {"records": record_ids}
    resp = http_post(url, headers, body, timeout, retries)
    data = _safe_json(resp)
    if data.get("code") != 0:
        return False, data
    return True, data


def create_bitable_record(
    app_token: str,
    table_id: str,
    tenant_token: str,
    fields: Dict[str, Any],
    timeout: int,
    retries: int,
) -> bool:
    ok, _ = create_bitable_record_with_id(
        app_token, table_id, tenant_token, fields, timeout, retries
    )
    return ok


def batch_create_bitable_records(
    app_token: str,
    table_id: str,
    tenant_token: str,
    records: List[Dict[str, Any]],
    timeout: int,
    retries: int,
    client_token: Optional[str] = None,
) -> Tuple[bool, Dict[str, Any]]:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
    headers = {
        "Authorization": f"Bearer {tenant_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    params = {"client_token": client_token or str(uuid.uuid4())}
    body = {"records": records}
    resp = http_post(url, headers, body, timeout, retries, params=params)
    data = _safe_json(resp)
    if data.get("code") != 0:
        return False, data
    return True, data


def create_bitable_record_with_id(
    app_token: str,
    table_id: str,
    tenant_token: str,
    fields: Dict[str, Any],
    timeout: int,
    retries: int,
) -> Tuple[bool, Optional[Any]]:
    ok, data = batch_create_bitable_records(
        app_token,
        table_id,
        tenant_token,
        [{"fields": fields}],
        timeout,
        retries,
    )
    if not ok:
        print(f"[Feishu] create record error: {data}", flush=True)
        return False, data
    records = (data.get("data") or {}).get("records") or []
    record = records[0] if records and isinstance(records[0], dict) else {}
    return True, record.get("record_id")


def upload_bitable_media(
    app_token: str,
    tenant_token: str,
    file_name: str,
    content: bytes,
    mime_type: str,
    timeout: int,
    retries: int,
    parent_type: str = "bitable_image",
) -> str:
    url = "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all"
    safe_name = str(file_name or "image").strip() or "image"
    body = {
        "file_name": safe_name,
        "parent_type": parent_type,
        "parent_node": app_token,
        "size": str(len(content or b"")),
    }
    headers = {
        "Authorization": f"Bearer {tenant_token}",
    }
    files = {
        "file": (safe_name, content or b"", mime_type or "application/octet-stream"),
    }
    resp = http_post_multipart(url, headers, body, files, timeout, retries)
    data = _safe_json(resp)
    if data.get("code") != 0:
        raise RuntimeError(f"[Feishu] upload media error: {data}")
    token = ((data.get("data") or {}).get("file_token") or "").strip()
    if not token:
        raise RuntimeError(f"[Feishu] upload media token missing: {data}")
    return token


def send_feishu_webhook(webhook_url: str, text: str, timeout: int, retries: int) -> bool:
    headers = {"Content-Type": "application/json"}
    body = {"msg_type": "text", "content": {"text": text}}
    resp = http_post(webhook_url, headers, body, timeout, retries)
    data = _safe_json(resp)
    return data.get("code", 0) == 0


def send_feishu_webhook_post(webhook_url: str, title: str, link: str, content_text: str, timeout: int, retries: int) -> bool:
    headers = {"Content-Type": "application/json"}
    content_blocks = []
    if content_text:
        content_blocks.append([{"tag": "text", "text": content_text}])
    if link:
        content_blocks.append([{"tag": "a", "text": "原文链接", "href": link}])
    body = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": content_blocks,
                }
            }
        },
    }
    resp = http_post(webhook_url, headers, body, timeout, retries)
    data = _safe_json(resp)
    return data.get("code", 0) == 0
