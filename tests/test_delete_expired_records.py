import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import delete_expired_records


def test_is_expired_candidate_uses_existing_30d_flag():
    assert delete_expired_records.is_expired_candidate({"30d": 0}) is True
    assert delete_expired_records.is_expired_candidate({"30d": {"value": [0]}}) is True
    assert delete_expired_records.is_expired_candidate({"30d": 1}) is False
    assert delete_expired_records.is_expired_candidate({"30d": {"value": [1]}}) is False
    assert delete_expired_records.is_expired_candidate({}) is False


def test_build_delete_plan_does_not_require_item_key_or_archive_target():
    source_records = [
        {"record_id": "rec1", "fields": {"item_key": "a", "30d": 0}},
        {"record_id": "rec2", "fields": {"30d": 0}},
        {"record_id": "rec3", "fields": {"item_key": "c", "30d": 1}},
        {"record_id": "", "fields": {"30d": 0}},
    ]

    plan = delete_expired_records.build_delete_plan(source_records, "NEWS", "tbl_news")

    assert [item.source_record_id for item in plan] == ["rec1", "rec2"]
    assert [item.item_key for item in plan] == ["a", ""]
    assert all(item.source_table_id == "tbl_news" for item in plan)


def test_apply_plan_deletes_in_batches_without_copying_records(monkeypatch):
    calls = []
    plan = [
        delete_expired_records.DeletePlanItem("NEWS", "tbl_news", "rec1"),
        delete_expired_records.DeletePlanItem("NEWS", "tbl_news", "rec2"),
        delete_expired_records.DeletePlanItem("FILTERED", "tbl_filtered", "rec3"),
    ]

    def fake_delete(_app_token, table_id, tenant_token, record_ids, *_args):
        calls.append((table_id, tenant_token, list(record_ids)))
        return True, {}

    monkeypatch.setattr(delete_expired_records, "batch_delete_bitable_records", fake_delete)

    stats, failed = delete_expired_records.apply_plan("tenant", plan, delete_batch_size=1)

    assert calls == [
        ("tbl_news", "tenant", ["rec1"]),
        ("tbl_news", "tenant", ["rec2"]),
        ("tbl_filtered", "tenant", ["rec3"]),
    ]
    assert stats == {"deleted": 3}
    assert failed == []


def test_apply_plan_refreshes_token_when_delete_token_expires(monkeypatch):
    delete_tokens = []
    plan = [delete_expired_records.DeletePlanItem("NEWS", "tbl_news", "rec1")]

    def fake_delete(_app_token, _table_id, tenant_token, record_ids, *_args):
        delete_tokens.append(tenant_token)
        if len(delete_tokens) == 1:
            return False, {"code": delete_expired_records.FEISHU_INVALID_TOKEN_CODE}
        return True, {"records": record_ids}

    monkeypatch.setattr(delete_expired_records, "batch_delete_bitable_records", fake_delete)

    stats, failed = delete_expired_records.apply_plan(
        "tenant_old",
        plan,
        delete_batch_size=500,
        token_refresher=lambda: "tenant_new",
    )

    assert delete_tokens == ["tenant_old", "tenant_new"]
    assert stats == {"deleted": 1}
    assert failed == []


def test_fetch_source_records_filters_30d_zero_and_sorts_oldest_first(monkeypatch):
    captured = {}

    def fake_list_records(*args, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(delete_expired_records, "list_bitable_records", fake_list_records)

    delete_expired_records.fetch_source_records("tenant", "NEWS", "tbl_news", "发布时间", 500, 3, scan_all=False)

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

    monkeypatch.setattr(delete_expired_records, "list_bitable_records", fake_list_records)

    delete_expired_records.fetch_source_records("tenant", "NEWS", "tbl_news", "发布时间", 500, 3, scan_all=True)

    assert captured["filter_obj"] is None
    assert captured["sort"] == [{"field_name": "发布时间", "desc": False}]


def test_run_dry_run_only_builds_delete_plan(monkeypatch, tmp_path):
    monkeypatch.setattr(delete_expired_records, "get_tenant_access_token", lambda *_args: "tenant")
    monkeypatch.setattr(
        delete_expired_records,
        "_source_specs",
        lambda: [("NEWS", "tbl_news", "发布时间")],
    )
    monkeypatch.setattr(
        delete_expired_records,
        "fetch_source_records",
        lambda *_args, **_kwargs: [{"record_id": "rec1", "fields": {"30d": 0}}],
    )

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("dry-run must not delete records")

    monkeypatch.setattr(delete_expired_records, "apply_plan", fail_if_called)
    output = tmp_path / "dry-run.json"

    result = delete_expired_records.run(
        dry_run=True,
        output_path=output,
        page_size=500,
        max_pages=80,
        apply_limit=0,
        delete_batch_size=500,
    )

    assert result["mode"] == "delete-expired-records-dry-run"
    assert result["plan"]["count"] == 1
    assert result["applied"]["deleted"] == 0
    assert "missing_tables" not in result
    assert "created" not in result["applied"]
