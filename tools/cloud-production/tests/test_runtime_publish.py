import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import runtime_publish as pub
import sopilot_producer


def shell(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    return result.stdout.decode().strip()


def rss(title="new", empty=False):
    item = "" if empty else "<item><guid>https://x.com/i/status/123</guid><title>" + title + "</title></item>"
    return ("<rss version='2.0'><channel><title>SoPilot</title>" + item + "</channel></rss>").encode()


def proof(payload, empty=False):
    return {"status": "success", "rss_sha256": pub.digest(payload), "entries": int(not empty),
            "discovered": int(not empty), "prefiltered": 0,
            "pages": [{"category": cat, "page": 1, "totalPages": 1, "http_success": True,
                       "parse_success": True} for cat in ("AI", "Creator")]}


class PublicationTests(unittest.TestCase):
    def setUp(self):
        (HERE / "out").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=HERE / "out")
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.repo = self.root / "runtime"
        self.repo.mkdir()
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(self.remote)], capture_output=True, check=True)
        shell(self.repo, "init", "--initial-branch=main")
        shell(self.repo, "config", "user.name", "Test")
        shell(self.repo, "config", "user.email", "test@example.invalid")
        shell(self.repo, "config", "core.autocrlf", "false")
        (self.repo / "feeds").mkdir()
        (self.repo / "feeds/sopilot.xml").write_bytes(rss("old"))
        (self.repo / "non-target.txt").write_bytes(b"must stay\n")
        shell(self.repo, "add", ".")
        shell(self.repo, "commit", "-m", "Initial")
        shell(self.repo, "remote", "add", "origin", str(self.remote))
        shell(self.repo, "push", "origin", "main")
        self.output = self.root / "evidence"
        self.payload = rss()

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, payload=None, capture=None):
        payload = payload or self.payload
        return pub.prepare(self.repo, payload, capture or proof(payload), "feeds/sopilot.xml", self.output, test_local=True)

    def publish(self, **kwargs):
        return pub.publish(self.repo, self.output, allow_write=True, test_local=True, **kwargs)

    def advance(self, path, content):
        other = self.root / "other"
        subprocess.run(["git", "clone", str(self.remote), str(other)], capture_output=True, check=True)
        shell(other, "config", "user.name", "Other owner")
        shell(other, "config", "user.email", "other@example.invalid")
        target = other / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        shell(other, "add", path)
        shell(other, "commit", "-m", "Concurrent change")
        shell(other, "push", "origin", "main")
        return shell(other, "rev-parse", "HEAD")

    def test_prepare_is_remote_read_only_and_dry_run_default(self):
        before = shell(self.remote, "rev-parse", "main")
        pending = self.prepare()
        self.assertEqual(before, shell(self.remote, "rev-parse", "main"))
        result = pub.publish(self.repo, self.output, test_local=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(result["push_attempted"])
        self.assertEqual(pending["changed_paths"], ["feeds/sopilot.xml"])

    def test_real_push_only_target_non_target_tree_entry_unchanged(self):
        before = shell(self.remote, "ls-tree", "main", "--", "non-target.txt")
        pending = self.prepare()
        result = self.publish()
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(shell(self.remote, "rev-parse", "main"), pending["pending_commit"])
        self.assertEqual(before, shell(self.remote, "ls-tree", "main", "--", "non-target.txt"))
        self.assertEqual(self.payload, pub.git(self.remote, "show", "main:feeds/sopilot.xml"))

    def test_second_publish_reconciles_without_second_commit(self):
        self.prepare()
        self.publish()
        before = shell(self.remote, "rev-parse", "main")
        self.assertEqual(self.publish()["status"], "confirmed")
        self.assertEqual(before, shell(self.remote, "rev-parse", "main"))

    def test_same_target_concurrent_update_blocks_without_overwrite(self):
        self.prepare()
        tip = self.advance("feeds/sopilot.xml", rss("new other owner"))
        with self.assertRaisesRegex(pub.PublishError, "same-target"):
            self.publish()
        self.assertEqual(tip, shell(self.remote, "rev-parse", "main"))

    def test_other_target_advance_reprepares_and_requires_durable_checkpoint(self):
        pending = self.prepare()
        tip = self.advance("feeds/another.xml", b"other owner's new bytes")
        result = self.publish()
        self.assertEqual(result["status"], "reprepare_required")
        self.assertFalse(result["push_attempted"])
        self.assertEqual(tip, shell(self.remote, "rev-parse", "main"))
        updated = json.loads((self.output / "pending.json").read_text())
        self.assertNotEqual(pending["pending_commit"], updated["pending_commit"])
        # Simulate artifact checkpoint then resume, with no recapture.
        self.assertEqual(self.publish()["status"], "confirmed")
        self.assertEqual(b"other owner's new bytes", pub.git(self.remote, "show", "main:feeds/another.xml"))

    def test_successful_push_followed_by_client_timeout_reconciles(self):
        pending = self.prepare()
        calls = []
        def timed_out_after_push():
            calls.append(1)
            shell(self.repo, "push", "origin", pending["pending_commit"] + ":refs/heads/main")
            raise subprocess.TimeoutExpired("git push", 1)
        self.assertEqual(self.publish(push=timed_out_after_push)["status"], "confirmed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(shell(self.remote, "rev-parse", "main"), pending["pending_commit"])

    def test_non_fast_forward_race_reconciles_before_reprepare(self):
        self.prepare()
        def racing_push():
            self.advance("non-target.txt", b"new external content")
            raise pub.PublishError("non-fast-forward")
        result = self.publish(push=racing_push)
        self.assertEqual(result["status"], "reprepare_required")
        self.assertEqual(b"new external content", pub.git(self.remote, "show", "main:non-target.txt"))

    def test_unreachable_remote_never_invokes_push(self):
        self.prepare()
        shell(self.repo, "remote", "set-url", "origin", str(self.root / "absent.git"))
        calls = []
        with self.assertRaises(pub.PublishError):
            self.publish(push=lambda: calls.append(1))
        self.assertEqual(calls, [])

    def test_unknown_after_push_preserves_original_pending_for_later_reconcile(self):
        pending = self.prepare()
        calls = []
        def push_then_lose_remote():
            calls.append(1)
            shell(self.repo, "push", "origin", pending["pending_commit"] + ":refs/heads/main")
            shell(self.repo, "remote", "set-url", "origin", str(self.root / "unreachable.git"))
            raise subprocess.TimeoutExpired("git push", 1)
        with self.assertRaises(pub.PublishError):
            self.publish(push=push_then_lose_remote)
        unknown = json.loads((self.output / "publish-receipt.json").read_text())
        self.assertEqual(unknown["status"], "blocked")
        self.assertTrue(unknown["push_attempted"])
        self.assertFalse(unknown["target_blob_verified"])
        shell(self.repo, "remote", "set-url", "origin", str(self.remote))
        self.assertEqual(self.publish()["status"], "confirmed")
        self.assertEqual(calls, [1])
        self.assertEqual(pending["pending_commit"], shell(self.remote, "rev-parse", "main"))

    def test_pending_checkpoint_exports_only_public_payload_and_commit_metadata(self):
        self.prepare()
        self.assertEqual({p.name for p in self.output.iterdir()}, {"candidate.xml", "pending.json", "pending.commit"})
        private_other_feed = b"must stay"
        self.assertTrue(all(private_other_feed not in path.read_bytes() for path in self.output.iterdir()))
        raw = (self.output / "pending.commit").read_bytes()
        self.assertTrue(raw.startswith(b"tree "))
        self.assertNotIn(b"non-target.txt", raw)

    def test_empty_snapshot_requires_complete_success_proof(self):
        payload = rss(empty=True)
        self.prepare(payload, proof(payload, empty=True))
        self.assertEqual(self.publish()["status"], "confirmed")

    def test_failed_or_incomplete_capture_cannot_prepare_replace(self):
        base = shell(self.remote, "rev-parse", "main")
        for modification in ({"status": "failed"}, {"pages": []}, {"entries": 9}):
            with self.subTest(modification=modification):
                with self.assertRaises(pub.PublishError):
                    self.prepare(capture={**proof(self.payload), **modification})
        self.assertEqual(base, shell(self.remote, "rev-parse", "main"))

    def test_target_allowlist_and_path_traversal_rejected(self):
        for target in ("feeds/../source-map.json", "source-map.json", "/feeds/sopilot.xml", "feeds\\sopilot.xml"):
            with self.subTest(target=target), self.assertRaises(pub.PublishError):
                pub.prepare(self.repo, self.payload, proof(self.payload), target, self.output, test_local=True)

    def test_unchanged_feed_still_reads_back_remote(self):
        self.payload = rss("old")
        self.assertEqual(self.prepare()["status"], "unchanged")
        result = self.publish()
        self.assertEqual(result["status"], "unchanged")
        self.assertFalse(result["push_attempted"])

    def test_exact_commit_resume_in_fresh_checkout(self):
        self.prepare()
        fresh = self.root / "fresh"
        subprocess.run(["git", "clone", str(self.remote), str(fresh)], capture_output=True, check=True)
        result = pub.publish(fresh, self.output, allow_write=True, test_local=True)
        self.assertEqual(result["status"], "confirmed")

    def test_contract_receipt_generated_by_actual_capture_and_bare_publish(self):
        run = self.root / "run"
        fixtures = HERE.parent / "cloud-shadow/tests/fixtures"
        class Response(io.BytesIO):
            status = 200
            def geturl(self):
                return self.url
        def frozen_http(request, **kwargs):
            category = parse_qs(urlparse(request.full_url).query)["category"][0]
            response = Response((fixtures / (category + "-1.html")).read_bytes())
            response.url = request.full_url
            return response
        env = {"GITHUB_REPOSITORY": "acaiaishizhan/rss-ingest-action", "GITHUB_RUN_ID": "9000000000",
               "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "7f75e63dc05ad9310cfd59c4bfef24a815c3be9a"}
        with patch("urllib.request.urlopen", side_effect=frozen_http), patch.dict(os.environ, env):
            captured = sopilot_producer.capture(run)
            self.assertEqual(captured["status"], "success")
            self.assertEqual(captured["entries"], 2)
            self.assertTrue(all(page["http_status"] == 200 and len(page["sha256"]) == 64 for page in captured["pages"]))
            pub.prepare(self.repo, (run / "sopilot.xml").read_bytes(), captured,
                        "feeds/sopilot.xml", run / "pending", test_local=True)
            pub.publish(self.repo, run / "pending", allow_write=True, test_local=True)
            value = sopilot_producer.receipt(run)
            self.assertEqual(value["publish"]["status"], "confirmed")
            self.assertTrue(value["publish"]["target_blob_verified"])
            self.assertEqual(len(value["publish"]["target_blob"]), 40)
            self.assertTrue(value["publish"]["started_at"])
            self.assertTrue(value["publish"]["finished_at"])
            pub.save(HERE / "out/receipt-contract.json", value)
            # A downloaded receipt must never establish verification for a later run.
            with patch.dict(os.environ, {"GITHUB_RUN_ID": "9000000001"}):
                stale = sopilot_producer.receipt(run)
                self.assertFalse(stale["publish"]["target_blob_verified"])
                self.assertEqual(stale["publish"]["status"], "not_verified_this_run")

    def test_unexpected_target_mode_is_not_normalized(self):
        shell(self.repo, "update-index", "--chmod=+x", "feeds/sopilot.xml")
        shell(self.repo, "commit", "-m", "Mode conflict")
        shell(self.repo, "push", "origin", "main")
        with self.assertRaisesRegex(pub.PublishError, "mode/type"):
            self.prepare()


if __name__ == "__main__":
    unittest.main()
