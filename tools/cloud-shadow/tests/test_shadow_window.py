import contextlib
import datetime as dt
import hashlib
import importlib.util
import io
import json
import socket
import sys
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import shadow_window as window
import sopilot_shadow as sopilot

spec = importlib.util.spec_from_file_location("integration_mirrors", HERE / "public-mirrors-shadow.py")
mirrors = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mirrors)


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket, "socket", side_effect=AssertionError("network forbidden in tests"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.start = dt.datetime(2026, 11, 1, 18, 30, tzinfo=dt.timezone.utc)
        self.end = self.start + dt.timedelta(hours=24)

    def make_window(self, current):
        return window.RequestWindow(clock=lambda: current)

    def new_output(self, prefix):
        return sopilot.OUT / (prefix + "-" + uuid.uuid4().hex)

    def test_closed_before_start_and_at_end_open_at_start(self):
        for current in (self.start - dt.timedelta(microseconds=1), self.end, self.end + dt.timedelta(seconds=1)):
            with self.assertRaises(window.WindowClosed):
                self.make_window(current).require_open()
        self.assertEqual(self.make_window(self.start).require_open(), self.start)
        self.assertEqual(self.make_window(self.end - dt.timedelta(microseconds=1)).require_open(), self.end - dt.timedelta(microseconds=1))

    def test_window_is_fixed_utc_and_exactly_24_hours(self):
        configured = window.RequestWindow()
        self.assertEqual(configured.start, self.start)
        self.assertEqual(configured.end, self.end)
        self.assertEqual(configured.metadata()["interval"], "[start,end)")

    def test_every_final_send_rechecks_after_open(self):
        guard = window.RequestGuard(self.make_window(self.start), {"sopilot.net"})
        clock = iter((self.start, self.end))
        guard.window.clock = lambda: next(clock)
        with patch.object(urllib.request.HTTPSHandler, "https_open", side_effect=AssertionError("no send allowed")):
            with self.assertRaises(window.WindowClosed):
                guard.open(urllib.request.Request("https://sopilot.net/zh/rank/tweets"))
        self.assertEqual([(event["stage"], event["allowed"]) for event in guard.events], [("open", True), ("send", False)])

    def test_redirect_rechecks_window_before_following(self):
        guard = window.RequestGuard(self.make_window(self.end), {"sopilot.net"})
        redirect = next(handler for handler in guard.opener.handlers if isinstance(handler, urllib.request.HTTPRedirectHandler))
        with self.assertRaises(window.WindowClosed):
            redirect.redirect_request(urllib.request.Request("https://sopilot.net/a"), None, 302, "Found", {}, "https://sopilot.net/b")
        self.assertEqual(guard.events[-1]["stage"], "redirect")
        self.assertFalse(guard.events[-1]["allowed"])

    def test_unchanged_reader_retry_after_end_sends_no_second_request(self):
        guard = window.RequestGuard(self.make_window(self.start), {"www.reddit.com"})
        sends = []
        def response(request, **kwargs):
            guard.check(request, "send")
            sends.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO(b""))
        def sleep(seconds):
            guard.window.clock = lambda: self.end
        source = next(row for row in mirrors.select_sources() if row[0].startswith("reddit-"))
        with patch.object(urllib.request, "urlopen", side_effect=response), patch.object(mirrors.frozen.time, "sleep", side_effect=sleep):
            with self.assertRaises(window.WindowClosed):
                mirrors.frozen._default_source_reader(mirrors.frozen.SourceSpec(*source))
        self.assertEqual(len(sends), 1)
        self.assertEqual(len(guard.events), 2)
        self.assertFalse(guard.events[-1]["allowed"])

    def test_default_and_explicit_selection_only_two_approved_sources(self):
        self.assertEqual([row[0] for row in mirrors.select_sources()], list(mirrors.SELECTED_SOURCE_NAMES))
        self.assertEqual([row[0] for row in mirrors.select_sources(["prompthub-blog"])], ["prompthub-blog"])
        for name in ("reddit-sideproject-ai-revenue", "substack-ai-maker", "private-rss"):
            with self.assertRaises(ValueError):
                mirrors.select_sources([name])

    def test_both_live_commands_skip_before_window_with_zero_requests(self):
        closed = self.make_window(self.start - dt.timedelta(seconds=1))
        mirror_output = self.new_output("test-mirrors-skipped")
        with patch.object(mirrors, "RequestWindow", return_value=closed), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mirrors.main(["--out", str(mirror_output)]), 0)
        result = json.loads((mirror_output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual((result["status"], result["success_count"], result["failure_count"], result["skipped_count"], result["network_requests"]), ("skipped", 0, 0, 2, 0))
        self.assertTrue(all(row["item_count"] is None and row["status"] == "skipped" for row in result["sources"]))
        sopilot_output = self.new_output("test-sopilot-skipped")
        with patch.object(sopilot, "RequestWindow", return_value=closed), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sopilot.run(sopilot_output), 0)
        result = json.loads((sopilot_output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual((result["status"], result["network_requests"], result["pages"]), ("skipped", 0, []))

    def test_frozen_sources_unchanged_from_prior_accepted_commits(self):
        self.assertEqual(hashlib.sha256((HERE / "sopilot_watch.py").read_bytes()).hexdigest(), sopilot.SOURCE_SHA256)
        self.assertEqual(hashlib.sha256((HERE / "public-mirrors-frozen.py").read_bytes()).hexdigest(), "9cf9cadce2a535059dc6a604cf6e53695f3247be4c6ca36dd6f668d4541a8de0")

    def test_workflows_only_read_and_separate_shadow_schedules(self):
        root = HERE.parent.parent
        for filename, cron, group in (("sopilot-cloud-shadow.yml", "2 * * * *", "sopilot-cloud-shadow-"),
                                      ("public-mirrors-cloud-shadow.yml", "5,20,35,50 * * * *", "public-mirrors-shadow-")):
            text = (root / ".github/workflows" / filename).read_text(encoding="utf-8")
            self.assertIn("cron: '" + cron + "'", text)
            self.assertIn("contents: read", text)
            self.assertIn("persist-credentials: false", text)
            self.assertIn("group: " + group, text)
            self.assertIn("refs/heads/main", text)
            self.assertIn("codex/cloud-shadow-integration-20261102", text)
            self.assertNotIn("secrets.", text)
            self.assertNotIn("continue-on-error", text)
            self.assertNotIn("feishu-write", text)
        mirror_text = (root / ".github/workflows/public-mirrors-cloud-shadow.yml").read_text(encoding="utf-8")
        self.assertIn("--source prompthub-blog --source reddit-indiehackers-ai-revenue", mirror_text)
        sopilot_text = (root / ".github/workflows/sopilot-cloud-shadow.yml").read_text(encoding="utf-8")
        self.assertIn("steps.capture.outputs.capture_status == 'success'", sopilot_text)


if __name__ == "__main__":
    unittest.main()
