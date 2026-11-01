import hashlib
import json
import sys
import unittest
import uuid
import contextlib
import io
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import sopilot_shadow as shadow
import sopilot_watch as producer


class FrozenProductionTests(unittest.TestCase):
    def test_json_fingerprint_uses_utf8_lf_bytes_on_every_platform(self):
        output = shadow.OUT / ("test-json-" + uuid.uuid4().hex)
        output.mkdir(parents=True)
        path = output / "canonical.json"
        shadow.save_json(path, {"title": "第一行\n第二行", "id": "123"})
        self.assertEqual(path.read_bytes(), '{\n  "id": "123",\n  "title": "第一行\\n第二行"\n}\n'.encode("utf-8"))
        self.assertNotIn(b"\r\n", path.read_bytes())

    def capture(self):
        def fetch(url):
            query = parse_qs(urlparse(url).query)
            return (HERE / "fixtures" / (query["category"][0] + "-1.html")).read_text(encoding="utf-8")
        return producer.fetch_sopilot(fetch)

    def test_vendored_source_is_exact_production_file(self):
        self.assertEqual(hashlib.sha256((HERE.parent / "sopilot_watch.py").read_bytes()).hexdigest(), shadow.SOURCE_SHA256)

    def test_frozen_fixture_exact_fields_rss_filter_and_identity(self):
        capture = self.capture()
        self.assertEqual((capture.discovered, capture.prefiltered, len(capture.entries)), (3, 1, 2))
        self.assertEqual(shadow.canonical_entries(capture), json.loads((HERE / "fixtures/expected-entries.json").read_text(encoding="utf-8")))
        self.assertEqual(shadow.canonical_rss(capture), (HERE / "fixtures/expected.xml").read_bytes())
        self.assertEqual(capture.entries[0]["id"], "https://x.com/i/status/100")
        self.assertEqual(capture.entries[0]["content"], "Codex Agent 自动化工作流\nprint('<ok>')\n完整正文")

    def test_invalid_frame_does_not_become_successful_empty(self):
        with self.assertRaises(ValueError):
            producer.fetch_sopilot(lambda url: "<html>access denied</html>")

    def test_valid_empty_is_proven_parsed(self):
        def fetch(url):
            category = parse_qs(urlparse(url).query)["category"][0]
            props = {"range": "6h", "category": category, "page": 1, "totalPages": 1,
                     "risingTweets": [], "hotTweets": []}
            stream = "1:" + json.dumps(props) + "\n"
            return "<script>self.__next_f.push(" + json.dumps([1, stream]) + ")</script>"
        capture = producer.fetch_sopilot(fetch)
        self.assertEqual((capture.discovered, capture.prefiltered, capture.entries), (0, 0, []))
        self.assertEqual(len(capture.pages), 2)

    def test_output_cannot_target_production(self):
        with self.assertRaises(ValueError):
            shadow.run(HERE.parent.parent.parent / "feeds/sopilot.xml")

    def test_real_offline_replay_cannot_call_http(self):
        output = shadow.OUT / ("test-replay-" + uuid.uuid4().hex)
        with patch.object(producer, "_fetch_text", side_effect=AssertionError("network forbidden")):
            with contextlib.redirect_stdout(io.StringIO()):
                result = shadow.run(output, HERE / "fixtures")
        self.assertEqual(result, 0)
        self.assertEqual((output / "canonical-sopilot.xml").read_bytes(), (HERE / "fixtures/expected.xml").read_bytes())
        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["writes"]["repository_files_changed_outside_output"], [])


if __name__ == "__main__":
    unittest.main()
