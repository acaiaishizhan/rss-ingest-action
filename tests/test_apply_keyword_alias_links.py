import os
import sys
import json

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

import merge_keywords
from tools import apply_keyword_alias_links as alias_links


def _keyword(record_id, name, type_="org", aliases=None, note=""):
    return merge_keywords.KeywordEntry(
        record_id=record_id,
        canonical_name=name,
        type=type_,
        aliases=aliases or [],
        note=note,
    )


def test_build_alias_link_plans_maps_existing_alias_record_to_main():
    plans, conflicts = alias_links.build_alias_link_plans(
        [
            _keyword("rec_google", "Google", aliases=["谷歌"]),
            _keyword("rec_cn", "谷歌"),
            _keyword("rec_cloud", "谷歌云"),
        ]
    )

    assert conflicts == []
    assert plans == {
        "rec_cn": {
            "alias_record_id": "rec_cn",
            "alias_name": "谷歌",
            "main_record_id": "rec_google",
            "main_name": "Google",
            "type": "org",
        }
    }


def test_build_alias_link_plans_skips_conflicting_alias_records():
    plans, conflicts = alias_links.build_alias_link_plans(
        [
            _keyword("rec_google", "Google", aliases=["谷歌"]),
            _keyword("rec_alpha", "Alphabet", aliases=["谷歌"]),
            _keyword("rec_other", "Other", aliases=["谷歌"]),
            _keyword("rec_cn", "谷歌"),
        ]
    )

    assert plans == {}
    assert conflicts[0]["alias_record_id"] == "rec_cn"


def test_build_alias_link_plans_does_not_use_merged_record_as_main():
    plans, conflicts = alias_links.build_alias_link_plans(
        [
            _keyword("rec_main", "Google", aliases=["谷歌"]),
            _keyword("rec_old", "谷歌", aliases=["Google"], note="[merged→Google] rec_main"),
        ]
    )

    assert conflicts == []
    assert plans["rec_old"]["main_record_id"] == "rec_main"
    assert "rec_main" not in plans


def test_build_alias_link_plans_skips_merged_record_with_nonstandard_marker_as_main():
    plans, conflicts = alias_links.build_alias_link_plans(
        [
            _keyword("rec_main", "Google", aliases=["谷歌"]),
            _keyword("rec_old", "谷歌", aliases=["Google"], note="[merged∪Google] rec_main"),
        ]
    )

    assert conflicts == []
    assert plans["rec_old"]["main_record_id"] == "rec_main"
    assert "rec_main" not in plans


def test_remap_link_ids_replaces_alias_and_dedupes_main():
    new_ids, changed, replacements = alias_links.remap_link_ids(
        ["rec_story", "rec_cn", "rec_google"],
        {
            "rec_cn": {
                "alias_record_id": "rec_cn",
                "alias_name": "谷歌",
                "main_record_id": "rec_google",
                "main_name": "Google",
                "type": "org",
            }
        },
    )

    assert changed is True
    assert new_ids == ["rec_story", "rec_google"]
    assert replacements[0]["alias_name"] == "谷歌"


def test_build_table_link_updates_counts_replacements():
    updates, counts = alias_links.build_table_link_updates(
        [
            {"record_id": "news_a", "fields": {"关键词记录": {"link_record_ids": ["rec_cn", "rec_other"]}}},
            {"record_id": "news_b", "fields": {"关键词记录": {"link_record_ids": ["rec_other"]}}},
        ],
        "关键词记录",
        {
            "rec_cn": {
                "alias_record_id": "rec_cn",
                "alias_name": "谷歌",
                "main_record_id": "rec_google",
                "main_name": "Google",
                "type": "org",
            }
        },
    )

    assert updates == [{"record_id": "news_a", "fields": {"关键词记录": ["rec_google", "rec_other"]}}]
    assert counts == {"rec_cn": 1}


def test_build_keyword_note_updates_marks_only_used_alias_records():
    updates = alias_links.build_keyword_note_updates(
        {
            "rec_cn": {
                "alias_record_id": "rec_cn",
                "alias_name": "谷歌",
                "main_record_id": "rec_google",
                "main_name": "Google",
                "type": "org",
            },
            "rec_unused": {
                "alias_record_id": "rec_unused",
                "alias_name": "unused",
                "main_record_id": "rec_main",
                "main_name": "Main",
                "type": "org",
            },
        },
        [{"record_id": "rec_cn", "fields": {"备注": "old note"}}],
        {"rec_cn": 2},
    )

    assert updates == [
        {
            "record_id": "rec_cn",
            "fields": {"备注": "old note\n[merged→Google] rec_google"},
        }
    ]


def test_apply_keyword_note_updates_skips_deleted_alias_records(monkeypatch):
    updates = [{"record_id": "rec_missing", "fields": {"备注": "[merged→Google] rec_google"}}]

    def fake_batch_update(app_token, table_id, tenant_token, batch, timeout, retries):
        return False, {
            "code": 1254043,
            "msg": "record not found,id = rec_missing",
        }

    monkeypatch.setattr(alias_links, "batch_update_bitable_records", fake_batch_update)

    updated, failed, skipped = alias_links.apply_keyword_note_updates("tbl_keyword", "tenant", updates)

    assert updated == 0
    assert failed == []
    assert skipped == [{"record_ids": ["rec_missing"], "reason": "record_not_found"}]


def test_repair_from_audit_report_updates_only_listed_records(monkeypatch, tmp_path):
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "merged_linked_details": [
                    {
                        "table": "NEWS",
                        "record_id": "news_a",
                        "keyword_ids": ["rec_cn", "rec_other"],
                        "merged_keyword_ids": ["rec_cn"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "repair.json"
    updates = []

    monkeypatch.setattr(alias_links, "get_tenant_access_token", lambda *args, **kwargs: "tenant")
    monkeypatch.setattr(
        alias_links,
        "load_alias_entries",
        lambda *args, **kwargs: (
            [
                _keyword("rec_google", "Google", aliases=["谷歌"]),
                _keyword("rec_cn", "谷歌", note="[merged→Google] rec_google"),
                _keyword("rec_other", "Other"),
            ],
            [],
        ),
    )

    def fake_update(app_token, table_id, tenant_token, record_id, fields, timeout, retries):
        updates.append((table_id, record_id, fields))
        return True

    monkeypatch.setattr(alias_links, "update_bitable_record_fields", fake_update)
    monkeypatch.setattr(alias_links.config, "FEISHU_NEWS_TABLE_ID", "tbl_news", raising=False)

    result = alias_links.repair_from_audit_report(audit_path, output_path, 500, 1, "", 0, dry_run=False)

    assert result["updated"] == {"news": 1, "filtered": 0}
    assert updates == [("tbl_news", "news_a", {alias_links.config.NEWS_FIELD_KEYWORD_RECORDS: ["rec_google", "rec_other"]})]


def test_repair_from_audit_report_uses_explicit_merged_target_when_alias_plan_missing(monkeypatch, tmp_path):
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "merged_linked_details": [
                    {
                        "table": "NEWS",
                        "record_id": "news_a",
                        "keyword_ids": ["rec_old", "rec_other"],
                        "merged_keyword_ids": ["rec_old"],
                        "merged_keyword_targets": [
                            {"alias_record_id": "rec_old", "target_record_ids": ["rec_main"]}
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "repair.json"
    updates = []

    monkeypatch.setattr(alias_links, "get_tenant_access_token", lambda *args, **kwargs: "tenant")
    monkeypatch.setattr(
        alias_links,
        "load_alias_entries",
        lambda *args, **kwargs: (
            [
                _keyword("rec_main", "Main"),
                _keyword("rec_old", "Old", note="[merged→Main] rec_main"),
                _keyword("rec_other", "Other"),
            ],
            [],
        ),
    )

    def fake_update(app_token, table_id, tenant_token, record_id, fields, timeout, retries):
        updates.append((table_id, record_id, fields))
        return True

    monkeypatch.setattr(alias_links, "update_bitable_record_fields", fake_update)
    monkeypatch.setattr(alias_links.config, "FEISHU_NEWS_TABLE_ID", "tbl_news", raising=False)

    result = alias_links.repair_from_audit_report(audit_path, output_path, 500, 1, "", 0, dry_run=False)

    assert result["updated"] == {"news": 1, "filtered": 0}
    assert updates == [("tbl_news", "news_a", {alias_links.config.NEWS_FIELD_KEYWORD_RECORDS: ["rec_main", "rec_other"]})]


def test_repair_from_audit_report_dry_run_does_not_write(monkeypatch, tmp_path):
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "merged_linked_details": [
                    {
                        "table": "NEWS",
                        "record_id": "news_a",
                        "keyword_ids": ["rec_cn", "rec_other"],
                        "merged_keyword_ids": ["rec_cn"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "repair.json"
    updates = []

    monkeypatch.setattr(alias_links, "get_tenant_access_token", lambda *args, **kwargs: "tenant")
    monkeypatch.setattr(
        alias_links,
        "load_alias_entries",
        lambda *args, **kwargs: (
            [
                _keyword("rec_google", "Google", aliases=["谷歌"]),
                _keyword("rec_cn", "谷歌", note="[merged→Google] rec_google"),
            ],
            [],
        ),
    )
    monkeypatch.setattr(alias_links, "update_bitable_record_fields", lambda *args, **kwargs: updates.append(args) or True)
    monkeypatch.setattr(alias_links.config, "FEISHU_NEWS_TABLE_ID", "tbl_news", raising=False)

    result = alias_links.repair_from_audit_report(audit_path, output_path, 500, 1, "", 0, dry_run=True)

    assert result["mode"] == "alias-link-repair-dry-run"
    assert result["planned"] == {"news": 1, "filtered": 0}
    assert result["updated"] == {"news": 0, "filtered": 0}
    assert updates == []
