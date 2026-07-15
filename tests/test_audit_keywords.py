import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

from tools import audit_keywords as keyword_audit  # noqa: E402


def test_audit_fails_when_merged_keyword_is_linked():
    report = keyword_audit.build_report(
        keyword_records=[
            {"record_id": "recold", "name": "Old Codex", "note": "[merged→Codex] recmain"},
            {"record_id": "recmain", "name": "Codex", "note": ""},
        ],
        article_links=[{"table": "NEWS", "record_id": "rec1", "keyword_ids": ["recold"]}],
        generic_names=set(),
    )

    assert report["merged_linked_count"] == 1
    assert report["merged_linked_details"] == [
        {
            "table": "NEWS",
            "record_id": "rec1",
            "merged_keyword_ids": ["recold"],
            "merged_keyword_targets": [{"alias_record_id": "recold", "target_record_ids": ["recmain"]}],
            "keyword_ids": ["recold"],
        }
    ]
    assert keyword_audit.report_is_healthy(report) is False


def test_audit_detects_compact_duplicate_groups():
    report = keyword_audit.build_report(
        keyword_records=[
            {"record_id": "kw1", "name": "Claude Code", "type": "product", "note": ""},
            {"record_id": "kw2", "name": "claudecode", "type": "product", "note": ""},
        ],
        article_links=[],
        generic_names=set(),
    )

    assert report["compact_duplicate_groups"] == 1
    assert keyword_audit.report_is_healthy(report) is False


def test_audit_ignores_compact_duplicate_groups_for_merged_records():
    report = keyword_audit.build_report(
        keyword_records=[
            {"record_id": "kwold", "name": "ClaudeCode", "type": "product", "note": "[merged→Claude Code] kwmain"},
            {"record_id": "kwmain", "name": "Claude Code", "type": "product", "note": ""},
        ],
        article_links=[{"table": "NEWS", "record_id": "rec1", "keyword_ids": ["kwmain"]}],
        generic_names=set(),
    )

    assert report["merged_linked_count"] == 0
    assert report["compact_duplicate_groups"] == 0
    assert keyword_audit.report_is_healthy(report) is True


def test_audit_allows_duplicate_groups_with_reason():
    report = keyword_audit.build_report(
        keyword_records=[
            {"record_id": "kw1", "name": "OpenAI Codex", "type": "product", "note": "duplicate-ok: org product phrase"},
            {"record_id": "kw2", "name": "openaicodex", "type": "product", "note": "duplicate-ok: legacy spelling"},
        ],
        article_links=[],
        generic_names=set(),
    )

    assert report["compact_duplicate_groups"] == 0


def test_audit_allows_known_bai_compact_duplicate_group():
    report = keyword_audit.build_report(
        keyword_records=[
            {"record_id": "kw1", "name": "BAI", "type": "org", "note": ""},
            {"record_id": "kw2", "name": "B.AI", "type": "org", "note": ""},
        ],
        article_links=[],
        generic_names=set(),
    )

    assert report["compact_duplicate_groups"] == 0
    assert report["allowed_compact_duplicate_groups"] == 1
    assert keyword_audit.report_is_healthy(report) is True
