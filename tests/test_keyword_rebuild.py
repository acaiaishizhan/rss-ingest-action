import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

import config  # noqa: E402
from tools import keyword_rebuild  # noqa: E402


def test_build_clear_link_payload_clears_both_keyword_fields():
    spec = keyword_rebuild.TableSpec(
        name="NEWS",
        table_id="tbl_news",
        keywords_field=config.NEWS_FIELD_KEYWORDS,
        keyword_records_field=config.NEWS_FIELD_KEYWORD_RECORDS,
    )

    payload = keyword_rebuild.build_clear_link_payload(spec, "rec1")

    assert payload == {
        "record_id": "rec1",
        "fields": {
            config.NEWS_FIELD_KEYWORDS: "",
            config.NEWS_FIELD_KEYWORD_RECORDS: [],
        },
    }


def test_delete_plan_blocks_apply_when_article_links_remain():
    plan = keyword_rebuild.build_delete_plan(
        keyword_records=[{"record_id": "kw1"}, {"record_id": "kw2"}],
        news_links=[{"record_id": "rec1", "keyword_ids": ["kw1"]}],
        filtered_links=[],
    )

    assert plan["keyword_total"] == 2
    assert plan["delete_count"] == 2
    assert plan["article_keyword_link_count"] == 1
    assert plan["apply_allowed"] is False


def test_delete_plan_allows_apply_when_no_links_remain():
    plan = keyword_rebuild.build_delete_plan(
        keyword_records=[{"record_id": "kw1"}],
        news_links=[],
        filtered_links=[],
    )

    assert plan["apply_allowed"] is True
