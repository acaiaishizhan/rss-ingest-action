# -*- coding: utf-8 -*-
"""Safety boundary for HTTP URLs derived from untrusted feed content."""

import ipaddress
import socket
from dataclasses import dataclass
from typing import Collection, Dict, Mapping, Optional
from urllib.parse import urljoin, urlparse

import requests


class UnsafeUrlError(ValueError):
    pass


class ResponseTooLargeError(RuntimeError):
    pass


_PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


@dataclass
class BufferedHttpResponse:
    status_code: int
    headers: Dict[str, str]
    content: bytes
    text: str
    url: str
    encoding: str
    apparent_encoding: str


def _host_shape_is_safe(host: str) -> bool:
    if not host or len(host) > 253:
        return False
    return all(0 < len(label) <= 63 for label in host.rstrip(".").split("."))


def is_public_http_url_literal(url: str) -> bool:
    """Reject obviously unsafe URL forms without performing DNS lookups."""
    try:
        clean_url = str(url or "").strip()
        if not clean_url or len(clean_url) > 4096:
            return False
        parsed = urlparse(clean_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith(".localhost"):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return _host_shape_is_safe(host)
    except ValueError:
        return False


def validate_public_http_url(
    url: str,
    *,
    use_system_proxy: bool = False,
    proxy_fake_ip_host_allowlist: Optional[Collection[str]] = None,
) -> None:
    if not is_public_http_url_literal(url):
        raise UnsafeUrlError(f"unsafe or non-public URL: {url!r}")
    parsed = urlparse(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    fake_ip_hosts = {
        str(value or "").strip().rstrip(".").lower()
        for value in (proxy_fake_ip_host_allowlist or ())
        if str(value or "").strip()
    }
    allow_proxy_fake_ip = bool(use_system_proxy and host in fake_ip_hosts)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeUrlError(f"URL hostname cannot be resolved safely: {host}") from exc
    if not addresses:
        raise UnsafeUrlError(f"URL hostname resolved to no addresses: {host}")
    for address in addresses:
        raw_ip = address[4][0]
        try:
            parsed_ip = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise UnsafeUrlError(f"URL resolved to invalid address: {raw_ip}") from exc
        if parsed_ip.is_global:
            continue
        if allow_proxy_fake_ip and parsed_ip in _PROXY_FAKE_IP_NETWORK:
            continue
        raise UnsafeUrlError(f"URL resolves to non-public address: {raw_ip}")


def _response_encoding(response: requests.Response) -> str:
    # ``apparent_encoding`` reads ``response.content``. That is unsafe for a
    # streamed response after ``iter_content`` has consumed the body, and it
    # would also buffer the whole body before our size guard if called first.
    return str(response.encoding or "utf-8")


def fetch_public_content(
    url: str,
    *,
    headers: Optional[Mapping[str, str]],
    timeout: int,
    max_bytes: int,
    use_system_proxy: bool,
    max_redirects: int = 5,
    proxy_fake_ip_host_allowlist: Optional[Collection[str]] = None,
) -> BufferedHttpResponse:
    """Fetch and buffer a public URL without exceeding ``max_bytes``."""
    current_url = str(url or "").strip()
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    validate_public_http_url(
        current_url,
        use_system_proxy=use_system_proxy,
        proxy_fake_ip_host_allowlist=proxy_fake_ip_host_allowlist,
    )
    with requests.Session() as session:
        session.trust_env = bool(use_system_proxy)
        for redirect_count in range(max_redirects + 1):
            if redirect_count:
                validate_public_http_url(
                    current_url,
                    use_system_proxy=use_system_proxy,
                    proxy_fake_ip_host_allowlist=proxy_fake_ip_host_allowlist,
                )
            response = session.get(
                current_url,
                headers=dict(headers or {}),
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = str(response.headers.get("Location") or "").strip()
                    if not location:
                        raise UnsafeUrlError("redirect response is missing Location")
                    if redirect_count >= max_redirects:
                        raise UnsafeUrlError(f"too many redirects for URL: {url!r}")
                    current_url = urljoin(current_url, location)
                    continue

                content_length = str(response.headers.get("Content-Length") or "").strip()
                if content_length.isdigit() and int(content_length) > max_bytes:
                    raise ResponseTooLargeError(
                        f"response exceeds {max_bytes} bytes (Content-Length={content_length})"
                    )

                chunks = []
                total = 0
                for chunk in response.iter_content(chunk_size=min(65536, max_bytes + 1)):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise ResponseTooLargeError(f"response exceeds {max_bytes} bytes")
                    chunks.append(chunk)
                content = b"".join(chunks)
                encoding = _response_encoding(response)
                return BufferedHttpResponse(
                    status_code=int(response.status_code),
                    headers={str(key): str(value) for key, value in response.headers.items()},
                    content=content,
                    text=content.decode(encoding, errors="replace"),
                    url=current_url,
                    encoding=encoding,
                    apparent_encoding=encoding,
                )
            finally:
                response.close()

    raise UnsafeUrlError(f"unable to fetch URL safely: {url!r}")
