"""User-locked prompt baseline; changes require explicit user authorization."""
import hashlib
import json
from pathlib import Path


def test_user_locked_prompt_baseline():
    root = Path(__file__).resolve().parents[1]
    baseline = json.loads((root / "docs/rss-screening-baseline.json").read_text(encoding="utf-8"))
    for name, expected in baseline["sha256"].items():
        # Ignore checkout line-ending conversion, preserve every prompt character.
        actual = (root / name).read_text(encoding="utf-8").encode("utf-8")
        assert hashlib.sha256(actual).hexdigest() == expected, name
