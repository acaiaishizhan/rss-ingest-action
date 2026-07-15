import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

from tools import audit_keyword_duplicates as audit  # noqa: E402


def _entry(record_id, name, *, type_="product", aliases=None, note="", news=0, filtered=0):
    return audit.KeywordAuditEntry(
        record_id=record_id,
        canonical_name=name,
        type=type_,
        aliases=aliases or [],
        note=note,
        news_count=news,
        filtered_count=filtered,
    )


def test_duplicate_audit_chooses_highest_usage_active_record_as_target():
    report = audit.build_duplicate_audit(
        [
            _entry("rec_low", "Claude Code", news=2),
            _entry("rec_high", "claude-code", news=5, filtered=1),
            _entry("rec_merged", "claudecode", note="[merged→Claude Code] rec_high", news=99),
        ],
        [
            {"table": "NEWS", "record_id": "news_a", "keyword_ids": ["rec_low"]},
            {"table": "FILTERED", "record_id": "filtered_a", "keyword_ids": ["rec_merged"]},
        ],
        generic_names=set(),
    )

    by_old_id = {item["old_record_id"]: item for item in report["candidates"]}

    assert by_old_id["rec_low"]["target_record_id"] == "rec_high"
    assert by_old_id["rec_low"]["reason"] == "duplicate_alias_key"
    assert by_old_id["rec_low"]["needs_relink"] is True
    assert by_old_id["rec_low"]["old_usage"]["total_count"] == 2
    assert by_old_id["rec_low"]["target_usage"]["total_count"] == 6

    assert by_old_id["rec_merged"]["reason"] == "merged_note_linked"
    assert by_old_id["rec_merged"]["target_record_id"] == "rec_high"
    assert by_old_id["rec_merged"]["needs_relink"] is True


def test_blocklist_record_is_not_used_as_duplicate_target_even_when_high_usage():
    report = audit.build_duplicate_audit(
        [
            _entry("rec_gpt", "GPT", type_="model", news=100),
            _entry("rec_chatgpt", "ChatGPT", type_="model", aliases=["GPT"], news=1),
        ],
        [{"table": "NEWS", "record_id": "news_a", "keyword_ids": ["rec_gpt"]}],
        generic_names={"gpt"},
    )

    assert report["candidate_count"] == 1
    candidate = report["candidates"][0]
    assert candidate["old_record_id"] == "rec_gpt"
    assert candidate["target_record_id"] == "rec_chatgpt"
    assert candidate["reason"] == "blocklist_alias_key"
    assert candidate["needs_relink"] is True
    assert candidate["old_is_blocked"] is True
    assert candidate["target_is_blocked"] is False
