import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import archive_old_records


def test_is_archive_candidate_uses_existing_30d_flag():
    assert archive_old_records.is_archive_candidate({"30d": 0}) is True
    assert archive_old_records.is_archive_candidate({"30d": {"value": [0]}}) is True
    assert archive_old_records.is_archive_candidate({"30d": 1}) is False
    assert archive_old_records.is_archive_candidate({"30d": {"value": [1]}}) is False
    assert archive_old_records.is_archive_candidate({}) is False


def test_quarter_name_uses_published_time_then_created_time():
    fields = {
        "发布时间": 1719792000000,  # 2024-07-01 UTC-ish timestamp, quarter only matters.
        "创建时间": 1711929600000,
    }
    assert archive_old_records.archive_table_name(fields, "NEWS") == "2024Q3"

    fields = {
        "发布时间": None,
        "创建时间": 1704067200000,
    }
    assert archive_old_records.archive_table_name(fields, "FILTERED") == "2024Q1回收站"


def test_archive_fields_drop_live_link_and_formula_fields():
    fields = {
        "标题": {"text": "A"},
        "发布时间": 1719792000000,
        "item_key": "k1",
        "关键词": "OpenAI",
        "关键词记录": ["rec_kw"],
        "24h": {"value": [0]},
        "30d": {"value": [0]},
        "创建时间": {"value": [1719792000000]},
        "摘要": "summary",
    }

    archived = archive_old_records.build_archive_fields(fields, "NEWS")

    assert archived["标题"] == {"text": "A"}
    assert archived["item_key"] == "k1"
    assert archived["关键词"] == "OpenAI"
    assert archived["摘要"] == "summary"
    assert "关键词记录" not in archived
    assert "24h" not in archived
    assert "30d" not in archived
    assert "创建时间" not in archived


def test_build_plan_groups_by_target_table_and_existing_item_key():
    source_records = [
        {"record_id": "rec1", "fields": {"item_key": "a", "30d": 0, "发布时间": 1704067200000}},
        {"record_id": "rec2", "fields": {"item_key": "b", "30d": 1, "发布时间": 1704067200000}},
        {"record_id": "rec3", "fields": {"item_key": "c", "30d": 0, "发布时间": 1719792000000}},
    ]
    existing = {"2024Q1": {"a"}}

    plan = archive_old_records.build_archive_plan(source_records, "NEWS", existing)

    assert len(plan) == 2
    assert plan[0].source_record_id == "rec1"
    assert plan[0].target_table_name == "2024Q1"
    assert plan[0].already_archived is True
    assert plan[1].source_record_id == "rec3"
    assert plan[1].target_table_name == "2024Q3"
    assert plan[1].already_archived is False


def test_apply_plan_prunes_fields_missing_from_target_table(monkeypatch):
    created = []
    deleted = []
    plan = [
        archive_old_records.ArchivePlanItem(
            source_kind="NEWS",
            source_table_id="tbl_source",
            source_record_id="rec1",
            target_table_name="2024Q1",
            item_key="k1",
            fields={
                "标题": {"text": "A", "link": "https://example.com"},
                "QA总结": [{"text": "Q\nA", "type": "text"}],
                "关键词": ["OpenAI", "Claude"],
                "item_key": "k1",
                "已读": True,
            },
        )
    ]

    def fake_create(*args):
        created.append(args[3])
        return True, "rec_new"

    def fake_delete(*args):
        deleted.extend(args[3])
        return True, {}

    monkeypatch.setattr(archive_old_records, "create_bitable_record_with_id", fake_create)
    monkeypatch.setattr(archive_old_records, "batch_delete_bitable_records", fake_delete)

    stats, failed = archive_old_records.apply_plan(
        "tenant",
        plan,
        {"2024Q1": "tbl_archive"},
        {
            "2024Q1": {
                "标题": {"ui_type": "Url"},
                "QA总结": {"ui_type": "Text"},
                "关键词": {"ui_type": "Text"},
                "item_key": {"ui_type": "Text"},
            }
        },
        delete_batch_size=500,
    )

    assert created == [
        {
            "标题": {"text": "A", "link": "https://example.com"},
            "QA总结": "Q\nA",
            "关键词": "OpenAI, Claude",
            "item_key": "k1",
        }
    ]
    assert deleted == ["rec1"]
    assert stats["created"] == 1
    assert stats["deleted"] == 1
    assert failed == []


def test_apply_plan_refreshes_token_when_archive_create_token_expires(monkeypatch):
    create_tokens = []
    delete_tokens = []
    plan = [
        archive_old_records.ArchivePlanItem(
            source_kind="NEWS",
            source_table_id="tbl_source",
            source_record_id="rec1",
            target_table_name="2024Q1",
            item_key="k1",
            fields={"标题": "A", "item_key": "k1"},
        )
    ]

    def fake_create(_app_token, _table_id, tenant_token, _fields, *_args):
        create_tokens.append(tenant_token)
        if len(create_tokens) == 1:
            return False, {"code": archive_old_records.FEISHU_INVALID_TOKEN_CODE}
        return True, "rec_new"

    def fake_delete(_app_token, _table_id, tenant_token, record_ids, *_args):
        delete_tokens.append(tenant_token)
        return True, {"records": record_ids}

    monkeypatch.setattr(archive_old_records, "create_bitable_record_with_id", fake_create)
    monkeypatch.setattr(archive_old_records, "batch_delete_bitable_records", fake_delete)

    stats, failed = archive_old_records.apply_plan(
        "tenant_old",
        plan,
        {"2024Q1": "tbl_archive"},
        {"2024Q1": {"标题": {"ui_type": "Text"}, "item_key": {"ui_type": "Text"}}},
        delete_batch_size=500,
        token_refresher=lambda: "tenant_new",
    )

    assert create_tokens == ["tenant_old", "tenant_new"]
    assert delete_tokens == ["tenant_new"]
    assert stats["created"] == 1
    assert stats["deleted"] == 1
    assert failed == []


def test_apply_plan_refreshes_token_when_source_delete_token_expires(monkeypatch):
    delete_tokens = []
    plan = [
        archive_old_records.ArchivePlanItem(
            source_kind="NEWS",
            source_table_id="tbl_source",
            source_record_id="rec1",
            target_table_name="2024Q1",
            item_key="k1",
            fields={"item_key": "k1"},
            already_archived=True,
        )
    ]

    def fake_delete(_app_token, _table_id, tenant_token, record_ids, *_args):
        delete_tokens.append(tenant_token)
        if len(delete_tokens) == 1:
            return False, {"code": archive_old_records.FEISHU_INVALID_TOKEN_CODE}
        return True, {"records": record_ids}

    monkeypatch.setattr(archive_old_records, "batch_delete_bitable_records", fake_delete)

    stats, failed = archive_old_records.apply_plan(
        "tenant_old",
        plan,
        {"2024Q1": "tbl_archive"},
        {"2024Q1": {"item_key": {"ui_type": "Text"}}},
        delete_batch_size=500,
        token_refresher=lambda: "tenant_new",
    )

    assert delete_tokens == ["tenant_old", "tenant_new"]
    assert stats["created"] == 0
    assert stats["deleted"] == 1
    assert stats["already_archived_deleted"] == 1
    assert failed == []


def test_fetch_source_records_filters_30d_zero_and_sorts_oldest_first(monkeypatch):
    captured = {}

    def fake_list_records(*args, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(archive_old_records, "list_bitable_records", fake_list_records)

    archive_old_records.fetch_source_records("tenant", "NEWS", "tbl_news", "发布时间", 500, 3, scan_all=False)

    assert captured["filter_obj"] == {
        "conjunction": "and",
        "conditions": [{"field_name": "30d", "operator": "is", "value": [0]}],
    }
    assert captured["sort"] == [{"field_name": "发布时间", "desc": False}]


def test_fetch_source_records_can_scan_all_for_diagnostics(monkeypatch):
    captured = {}

    def fake_list_records(*args, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(archive_old_records, "list_bitable_records", fake_list_records)

    archive_old_records.fetch_source_records("tenant", "NEWS", "tbl_news", "发布时间", 500, 3, scan_all=True)

    assert captured["filter_obj"] is None
    assert captured["sort"] == [{"field_name": "发布时间", "desc": False}]
