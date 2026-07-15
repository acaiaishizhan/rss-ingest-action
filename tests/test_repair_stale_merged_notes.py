import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

from tools import repair_stale_merged_notes  # noqa: E402


def test_strip_stale_marker_lines_preserves_other_note_text():
    note = "keep this\n[merged→Old] rec_missing\nalso keep"

    cleaned = repair_stale_merged_notes.strip_stale_marker_lines(note, ["rec_missing"])

    assert cleaned == "keep this\nalso keep"


def test_strip_stale_marker_lines_keeps_non_missing_target():
    note = "[merged→Old] rec_existing"

    cleaned = repair_stale_merged_notes.strip_stale_marker_lines(note, ["rec_missing"])

    assert cleaned == "[merged→Old] rec_existing"


def test_build_updates_from_stale_audit_details():
    payload = {
        "stale_merged_note_details": [
            {
                "record_id": "rec_alias",
                "note": "[merged→Old] rec_missing",
                "missing_target_ids": ["rec_missing"],
            },
            {
                "record_id": "rec_keep",
                "note": "[merged→Old] rec_existing",
                "missing_target_ids": ["rec_missing"],
            },
        ],
    }

    assert repair_stale_merged_notes.build_updates(payload) == [
        {"record_id": "rec_alias", "fields": {"备注": ""}}
    ]
