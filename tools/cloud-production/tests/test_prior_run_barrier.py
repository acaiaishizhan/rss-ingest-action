import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prior_run_barrier as barrier
import runtime_publish as pub

REPO = "acaiaishizhan/rss-ingest-action"


def receipt(run_id="9", capture=None, publish=None, **extra):
    return {"schema_version": 1, "owner": "cloud", "mode": "production", "source": "sopilot",
            "workflow": "sopilot-producer.yml", "run": {"repository": REPO, "id": run_id, "attempt": "1", "code_sha": "abc"},
            "capture": capture or {}, "publish": publish or {}, **extra}


def zipped(value):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("receipt.json", json.dumps(value))
    return output.getvalue()


class BarrierTests(unittest.TestCase):
    def mock_api(self, receipts, missing=False, jobs=None):
        def api(path):
            if "workflows/" in path:
                return json.dumps({"workflow_runs": [{"id": int(run), "status": "completed", "run_attempt": 1, "head_sha": "abc"}
                                                      for run in receipts]}).encode()
            if "/jobs?" in path:
                return json.dumps({"jobs": jobs or []}).encode()
            if "/runs/" in path:
                run = path.split("/runs/")[1].split("/")[0]
                return json.dumps({"artifacts": [] if missing else [{"id": int(run), "name": "sopilot-production-receipt-" + run + "-1", "expired": False}]}).encode()
            run = path.split("/artifacts/")[1].split("/")[0]
            return zipped(receipts[run])
        return patch.object(barrier, "api", side_effect=api)

    def test_confirmed_readback_unblocks_new_capture(self):
        value = receipt(capture={"status": "success"}, publish={"status": "confirmed", "target_blob_verified": True, "remote_sha": "def"})
        with self.mock_api({"9": value}):
            self.assertEqual(barrier.check(REPO, "10")["status"], "confirmed")

    def test_unknown_push_blocks_new_capture(self):
        value = receipt(capture={"status": "success"}, publish={"status": "blocked", "push_attempted": True})
        with self.mock_api({"9": value}), self.assertRaisesRegex(pub.PublishError, "explicitly resume"):
            barrier.check(REPO, "10")

    def test_missing_receipt_is_unknown(self):
        with self.mock_api({"9": receipt()}, missing=True), self.assertRaisesRegex(pub.PublishError, "missing/ambiguous"):
            barrier.check(REPO, "10")

    def test_owner_gate_skipped_job_without_steps_is_proven_no_push(self):
        # Shape returned by GitHub Actions list-jobs-for-workflow-run-attempt.
        skipped = {"id": 123456, "run_id": 9, "name": "produce", "status": "completed",
                   "conclusion": "skipped", "steps": [], "started_at": None, "completed_at": None}
        with self.mock_api({"9": receipt()}, missing=True, jobs=[skipped]):
            self.assertEqual(barrier.check(REPO, "10")["status"], "no_prior_push")
        # Even a nominally skipped job with any executed step cannot be ignored.
        with self.mock_api({"9": receipt()}, missing=True,
                           jobs=[{**skipped, "steps": [{"name": "Capture", "conclusion": "success"}]}]), self.assertRaises(pub.PublishError):
            barrier.check(REPO, "10")

    def test_proven_failed_capture_does_not_stop_next_attempt(self):
        value = receipt(capture={"status": "failed"}, publish={"status": "not_started"})
        with self.mock_api({"9": value}):
            self.assertEqual(barrier.check(REPO, "10")["status"], "no_prior_push")

    def test_blocked_followup_still_finds_original_unknown(self):
        values = {"9": receipt(barrier={"status": "blocked"}),
                  "8": receipt("8", capture={"status": "success"}, publish={"status": "retry_required"})}
        with self.mock_api(values), self.assertRaisesRegex(pub.PublishError, "prior run 8"):
            barrier.check(REPO, "10")

    def test_receipt_run_fingerprint_must_match(self):
        value = receipt(capture={"status": "success"}, publish={"status": "confirmed", "target_blob_verified": True})
        value["run"]["code_sha"] = "wrong"
        with self.mock_api({"9": value}), self.assertRaisesRegex(pub.PublishError, "fingerprint"):
            barrier.check(REPO, "10")


if __name__ == "__main__":
    unittest.main()
