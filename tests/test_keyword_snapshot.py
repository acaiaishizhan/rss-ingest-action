import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

import config  # noqa: E402
from tools import keyword_snapshot  # noqa: E402


def test_keyword_entries_from_records_and_snapshot_preserve_parent_owner_links():
    records = [
        {
            "record_id": "rec_child",
            "fields": {
                config.KEYWORD_FIELD_CANONICAL_NAME: "GPT-5",
                config.KEYWORD_FIELD_TYPE: "model",
                config.KEYWORD_FIELD_ALIASES: "gpt5",
                config.KEYWORD_FIELD_NEWS_COUNT: 3,
                config.KEYWORD_FIELD_FILTERED_COUNT: 1,
                config.KEYWORD_FIELD_NOTE: "",
                config.KEYWORD_FIELD_PARENT: {"link_record_ids": ["rec_parent"]},
                config.KEYWORD_FIELD_OWNERS: [{"record_id": "rec_owner"}],
            },
        }
    ]

    entries = keyword_snapshot.keyword_entries_from_records(records)
    payload = keyword_snapshot.keyword_entry_to_snapshot(entries[0])
    restored = keyword_snapshot.keyword_entry_from_snapshot(payload)

    assert payload["parent_ids"] == ["rec_parent"]
    assert payload["owner_ids"] == ["rec_owner"]
    assert restored.parent_ids == ["rec_parent"]
    assert restored.owner_ids == ["rec_owner"]


def test_recent_keyword_snapshot_explicitly_allows_one_page_sample(monkeypatch):
    captured = {}

    def fake_list(*args, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(keyword_snapshot, "list_bitable_records", fake_list)

    records = keyword_snapshot.fetch_recent_keyword_records(
        page_size=500,
        max_pages=10,
        since_ms=0,
        tenant_token="tenant",
    )

    assert records == []
    assert captured["max_pages"] == 1
    assert captured["allow_partial"] is True
