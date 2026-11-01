"""Shared fixed 24-hour request window for both read-only cloud probes."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

CONFIG = Path(__file__).with_name("shadow-window.json")


class WindowClosed(RuntimeError):
    pass


class RequestWindow:
    def __init__(self, path=CONFIG, clock=None):
        self.path = Path(path)
        raw = self.path.read_bytes()
        config = json.loads(raw)
        self.start = dt.datetime.fromisoformat(config["start_utc"].replace("Z", "+00:00"))
        self.end = dt.datetime.fromisoformat(config["end_utc"].replace("Z", "+00:00"))
        if (self.start.utcoffset() != dt.timedelta(0) or self.end.utcoffset() != dt.timedelta(0)
                or self.end - self.start != dt.timedelta(hours=24)):
            raise ValueError("Shadow window must be exactly 24 hours with explicit UTC boundaries")
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))

    def metadata(self):
        return {"start_utc": self.start.isoformat(), "end_utc": self.end.isoformat(),
                "interval": "[start,end)", "config_sha256": self.sha256,
                "guard_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}

    def require_open(self):
        current = self.clock()
        if current < self.start:
            raise WindowClosed("skipped: request window has not started")
        if current >= self.end:
            raise WindowClosed("skipped: request window has ended")
        return current


class RequestGuard:
    """Check every open, redirect and final HTTP send, including reader retries."""
    def __init__(self, window, allowed_hosts):
        self.window, self.allowed_hosts = window, set(allowed_hosts)
        self.events = []
        guard = self

        class HTTPS(urllib.request.HTTPSHandler):
            def https_open(self, request):
                guard.check(request, "send")
                return super().https_open(request)

        class HTTP(urllib.request.HTTPHandler):
            def http_open(self, request):
                guard.check(request, "send")
                return super().http_open(request)

        class Redirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, message, headers, newurl):
                guard.check(urllib.request.Request(newurl), "redirect")
                return super().redirect_request(request, fp, code, message, headers, newurl)

        self.opener = urllib.request.build_opener(HTTPS(), HTTP(), Redirect())

    def check(self, request, stage):
        url = request.full_url
        event = {"stage": stage, "url": url, "method": request.get_method(), "allowed": False}
        try:
            event["checked_at"] = self.window.require_open().isoformat()
            parsed = urlparse(url)
            if (request.get_method() != "GET" or request.data is not None or parsed.scheme != "https"
                    or parsed.hostname not in self.allowed_hosts or parsed.username or parsed.password):
                raise ValueError("Only the selected fixed public HTTPS GET hosts are allowed")
            if any(key.lower() in {"authorization", "cookie", "proxy-authorization"} for key, _ in request.header_items()):
                raise ValueError("Authenticated requests are forbidden in public shadow capture")
            event["allowed"] = True
        except Exception as error:
            event.update(error_type=type(error).__name__, error=str(error))
            raise
        finally:
            self.events.append(event)

    def open(self, request, *args, **kwargs):
        self.check(request, "open")
        return self.opener.open(request, *args, **kwargs)
