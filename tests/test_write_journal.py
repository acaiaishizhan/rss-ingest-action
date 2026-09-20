import json
import threading
import uuid
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import config
import feishu_client as client
import rss_ingest
from write_journal import SourceWriteJournal, held_write, reconcile_held_write


def setup_journal(tmp_path, monkeypatch, key="item", state=None, persist=None):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config, "FEISHU_RSS_TABLE_ID", "source-table")
    state = state if state is not None else {"updated_failed_items": [], "now_ms": 1000}
    item = {"source_id": "source", "item_key": key, "entry_ts_ms": 1, "article": {"title": "fixture", "link": "https://example.test"}}
    return SourceWriteJournal(state, item, "test-tenant", threading.RLock(), threading.RLock(), persist or (lambda *a: True))


def create(journal, fields):
    token = client.CREATE_JOURNAL.set(journal)
    try:
        return client.create_bitable_record_with_id("app", "news", "tenant", fields, 1, 1)
    finally:
        client.CREATE_JOURNAL.reset(token)


def test_source_intent_precedes_create_and_ack_precedes_retirement(tmp_path, monkeypatch):
    events = []
    def persist(*args):
        entries = json.loads(args[4][config.RSS_FIELD_FAILED_ITEMS])
        events.append(entries[0]["write"]["phase"] if entries else "retired")
        return True
    journal = setup_journal(tmp_path, monkeypatch, persist=persist)
    def remote(*args, **kwargs):
        assert events == ["intent"]
        events.append("remote-create")
        return True, {"data": {"records": [{"record_id": "recOne"}]}}
    monkeypatch.setattr(client, "batch_create_bitable_records", remote)
    assert create(journal, {"item_key": "item"}) == (True, "recOne")
    journal.finish()
    assert events == ["intent", "remote-create", "confirmed", "retired"]
    evidence = list((tmp_path/"out/write-journal").glob("*.json"))
    assert len(evidence) == 1
    assert json.loads(evidence[0].read_text())["write"]["record_id"] == "recOne"


def test_accepted_then_eof_remains_held_across_a_new_journal_and_is_not_recreated(tmp_path, monkeypatch):
    source = {}
    def persist(*args):
        source["items"] = json.loads(args[4][config.RSS_FIELD_FAILED_ITEMS])
        return True
    journal = setup_journal(tmp_path, monkeypatch, persist=persist)
    committed = []
    def remote(*args, **kwargs):
        committed.append(kwargs["client_token"])
        raise RuntimeError("EOF after remote commit")
    monkeypatch.setattr(client, "batch_create_bitable_records", remote)
    with pytest.raises(RuntimeError, match="EOF"):
        create(journal, {"item_key": "item"})
    restored = {"updated_failed_items": rss_ingest.parse_failed_items(json.dumps(source["items"])), "now_ms": 10000}
    later = setup_journal(tmp_path, monkeypatch, state=restored, persist=persist)
    with pytest.raises(RuntimeError, match="WRITE_UNCERTAIN"):
        create(later, {"item_key": "item"})
    assert len(committed) == 1
    assert held_write(restored["updated_failed_items"][0])
    assert len(rss_ingest.prune_failed_items(restored["updated_failed_items"], 9999999999999)) == 1


def test_failed_intent_write_proves_no_create_and_allows_a_later_attempt(tmp_path, monkeypatch):
    journal = setup_journal(tmp_path, monkeypatch, persist=lambda *args: False)
    calls = []
    monkeypatch.setattr(client, "batch_create_bitable_records", lambda *a, **k: calls.append(k) or (True, {"data": {"records": [{"record_id": "recOne"}]}}))
    with pytest.raises(RuntimeError, match="could not be persisted"):
        create(journal, {"item_key": "item"})
    assert calls == []
    assert journal.state["updated_failed_items"][0]["write"]["phase"] == "not_submitted"
    journal.persist = lambda *args: True
    assert create(journal, {"item_key": "item"}) == (True, "recOne")
    assert len(calls) == 1


def test_same_item_has_same_v4_operation_across_independent_producers(tmp_path, monkeypatch):
    rows = {}
    def remote(*args, **kwargs):
        token = kwargs["client_token"]
        rows.setdefault(token, "rec"+str(len(rows)+1))
        return True, {"data": {"records": [{"record_id": rows[token]}]}}
    monkeypatch.setattr(client, "batch_create_bitable_records", remote)
    first = setup_journal(tmp_path, monkeypatch)
    second = setup_journal(tmp_path, monkeypatch)
    assert create(first, {"item_key": "item", "summary": "producer A"}) == create(second, {"item_key": "item", "summary": "producer B"})
    assert len(rows) == 1
    assert uuid.UUID(next(iter(rows))).version == 4


def test_ordinary_create_uses_a_fresh_operation_token_on_next_attempt(monkeypatch):
    tokens = []

    def remote(*args, **kwargs):
        tokens.append(kwargs["client_token"])
        return False, {"code": 1254291, "msg": "write conflict"}

    monkeypatch.setattr(client, "batch_create_bitable_records", remote)

    assert client.create_bitable_record_with_id("app", "news", "tenant", {"item_key": "item"}, 1, 1)[0] is False
    assert client.create_bitable_record_with_id("app", "news", "tenant", {"item_key": "item"}, 1, 1)[0] is False
    assert len(tokens) == 2
    assert tokens[0] != tokens[1]
    assert all(uuid.UUID(token).version == 4 for token in tokens)


def test_ack_without_record_id_is_unknown_and_never_green(tmp_path, monkeypatch):
    journal = setup_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(client, "batch_create_bitable_records", lambda *a, **k: (True, {"data": {"records": []}}))
    with pytest.raises(RuntimeError, match="no record_id"):
        create(journal, {"item_key": "item"})
    assert held_write(journal.state["updated_failed_items"][0])


def test_reconciliation_uses_exact_table_and_only_positive_unique_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FEISHU_NEWS_TABLE_ID", "news")
    item = {"write": {"phase": "unknown", "table_id": "news", "identity_field": "item_key", "identity_value": "item"}}
    calls = []
    def read(*args, **kwargs):
        calls.append((args, kwargs))
        return []
    monkeypatch.setattr(client, "list_bitable_records", read)
    assert reconcile_held_write(item, "tenant") is None
    assert calls[0][0][1] == "news"
    assert calls[0][1]["filter_obj"]["conditions"][0]["value"] == ["item"]
    monkeypatch.setattr(client, "list_bitable_records", lambda *a, **k: [{"record_id": "recObserved"}])
    assert reconcile_held_write(item, "tenant")["record_id"] == "recObserved"
    monkeypatch.setattr(client, "list_bitable_records", lambda *a, **k: [{"record_id": "recA"}, {"record_id": "recB"}])
    assert reconcile_held_write(item, "tenant") is None
    assert item["write"]["phase"] == "unknown"


def test_concurrent_success_does_not_erase_another_items_unknown_intent(tmp_path, monkeypatch):
    state = {"updated_failed_items": [], "now_ms": 1000}
    remote_source = {}
    def persist(*args):
        remote_source["items"] = json.loads(args[4][config.RSS_FIELD_FAILED_ITEMS])
        return True
    first = setup_journal(tmp_path, monkeypatch, "unknown", state, persist)
    second = setup_journal(tmp_path, monkeypatch, "success", state, persist)
    second.source_lock, second.state_lock = first.source_lock, first.state_lock
    barrier = threading.Barrier(2)
    def remote(*args, **kwargs):
        barrier.wait(timeout=5)
        if args[3][0]["fields"]["item_key"] == "unknown":
            raise RuntimeError("accepted then EOF")
        return True, {"data": {"records": [{"record_id": "recGood"}]}}
    monkeypatch.setattr(client, "batch_create_bitable_records", remote)
    def run(journal, key):
        try:
            create(journal, {"item_key": key})
            journal.finish()
        except RuntimeError:
            pass
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(run, first, "unknown"), pool.submit(run, second, "success")]
        for future in futures:
            future.result()
    assert [item["item_key"] for item in remote_source["items"]] == ["unknown"]
    assert held_write(remote_source["items"][0])
