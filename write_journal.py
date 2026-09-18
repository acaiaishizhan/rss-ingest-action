"""Per-source write intents in the existing failed_items field, before row I/O.

Sources keep separate journals. A held item never prevents other source items
from progressing; absence in a later read never authorizes an unknown create.
"""
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import config
import feishu_client


def held_write(item):
    return (item.get("write") or {}).get("phase") in {"intent", "unknown"}


def archive_retired(state):
    retired = state.get("retired_failed_items", [])
    offset = state.get("_retired_archived", 0)
    if len(retired) <= offset:
        return
    target = Path(config.BASE_DIR)/"out"/"retired-failures"/(str(uuid.uuid4())+".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as handle:
        json.dump({"source_id": state["source"]["record_id"], "run_id": os.getenv("GITHUB_RUN_ID", "local"),
                   "source_snapshot_sha256": state.get("source_snapshot_sha256"), "retired": retired[offset:]},
                  handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    state["_retired_archived"] = len(retired)


def reconcile_held_write(item, tenant_token):
    proof = item.get("write") or {}
    table = proof.get("table_id")
    field = proof.get("identity_field")
    value = proof.get("identity_value")
    allowed = {config.FEISHU_NEWS_TABLE_ID: "item_key", config.FEISHU_FILTERED_TABLE_ID: "item_key",
               config.FEISHU_KEYWORD_TABLE_ID: config.KEYWORD_FIELD_CANONICAL_NAME}
    if not table or allowed.get(table) != field or not value:
        return None
    records = feishu_client.list_bitable_records(config.FEISHU_APP_TOKEN, table, tenant_token,
        config.HTTP_TIMEOUT, config.HTTP_RETRIES, page_size=10, max_pages=1, allow_partial=False,
        filter_obj={"conjunction": "and", "conditions": [{"field_name": field, "operator": "is", "value": [str(value)]}]})
    if len(records) != 1 or not records[0].get("record_id"):
        return None  # Absence/duplicates are not proof that no earlier write happened.
    return {"table_id": table, "record_id": records[0]["record_id"], "identity_field": field,
            "identity_value": value, "read_at_ms": int(time.time()*1000)}


def settle_held_writes(state, tenant_token):
    kept = []
    for item in state["updated_failed_items"]:
        if not held_write(item):
            kept.append(item)
            continue
        try:
            witness = reconcile_held_write(item, tenant_token)
        except Exception:
            witness = None
        if not witness:
            kept.append(item)
            continue
        state.setdefault("retired_failed_items", []).append({"reason": "reconciled_after_write", "item": json.loads(json.dumps(item)), "witness": witness})
        if witness["table_id"] == config.FEISHU_KEYWORD_TABLE_ID:
            item["write"].update(phase="confirmed", record_id=witness["record_id"])
            item["last_error"] = "WORKER_RECONCILED_KEYWORD"
            kept.append(item)
    state["updated_failed_items"][:] = kept


class SourceWriteJournal:
    def __init__(self, state, item, tenant_token, source_lock, state_lock, persist=None):
        self.state, self.item, self.tenant_token = state, item, tenant_token
        self.source_lock, self.state_lock = source_lock, state_lock
        self.persist = persist or feishu_client.update_bitable_record_fields
        self.current = None
        self.evidence = None
        self.path = None

    def _entry(self):
        entries = self.state["updated_failed_items"]
        for entry in entries:
            if entry["item_key"] == self.item["item_key"]:
                return entry
        article = self.item["article"]
        entry = {"item_key": self.item["item_key"], "title": article.get("title", ""),
                 "link": article.get("link", ""), "published_ms": self.item.get("entry_ts_ms", 0),
                 "fail_count": 0, "last_seen_ms": self.state["now_ms"], "miss_count": 0}
        entries.append(entry)
        return entry

    def _save_evidence(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.path.with_suffix(".new")
        with staging.open("w", encoding="utf-8") as handle:
            json.dump(self.evidence, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        staging.replace(self.path)

    def _commit(self, mutation):
        with self.source_lock:
            with self.state_lock:
                mutation()
                value = json.dumps(self.state["updated_failed_items"], ensure_ascii=False)
            if not self.persist(config.FEISHU_APP_TOKEN, config.FEISHU_RSS_TABLE_ID,
                    self.tenant_token, self.item["source_id"],
                    {config.RSS_FIELD_FAILED_ITEMS: value}, config.HTTP_TIMEOUT, config.HTTP_RETRIES):
                raise RuntimeError("Write intent/receipt could not be persisted to its source")

    def begin(self, table_id, fields, client_token):
        with self.state_lock:
            if any(entry["item_key"] == self.item["item_key"] and held_write(entry)
                   for entry in self.state["updated_failed_items"]):
                raise RuntimeError("WRITE_UNCERTAIN: reconcile retained source intent before another create")
        attempt = str(uuid.uuid4())
        payload_hash = hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.current = {"phase": "intent", "table_id": table_id, "client_token": client_token,
                        "payload_sha256": payload_hash, "attempt_id": attempt,
                        "run_id": os.getenv("GITHUB_RUN_ID", "local"), "created_ms": int(time.time()*1000)}
        identity_field = "item_key" if fields.get("item_key") else config.KEYWORD_FIELD_CANONICAL_NAME
        self.current.update(identity_field=identity_field, identity_value=fields.get(identity_field))
        self.evidence = {"source_id": self.item["source_id"], "item_key": self.item["item_key"],
                         "write": self.current, "payload": fields, "events": [{"phase": "intent"}]}
        self.path = Path(config.BASE_DIR)/"out"/"write-journal"/(attempt+".json")
        self._save_evidence()
        def prepare():
            entry = self._entry()
            entry.update(write=dict(self.current), last_error="WRITE_INTENT")
        try:
            self._commit(prepare)
        except Exception:
            # No create is called until this method returns successfully.
            self.current["phase"] = "not_submitted"
            with self.state_lock:
                self._entry().update(write=dict(self.current), last_error="WRITE_NOT_SUBMITTED")
            self.evidence["events"].append({"phase": "not_submitted"})
            self._save_evidence()
            raise

    def _outcome(self, phase, **details):
        self.current.update(phase=phase, **details)
        self.evidence["events"].append({"phase": phase, **details})
        self._save_evidence()
        def update():
            self._entry().update(write=dict(self.current), last_error="WRITE_"+phase.upper())
        self._commit(update)

    def confirm(self, record_id):
        self._outcome("confirmed", record_id=record_id)

    def reject(self, code):
        self._outcome("rejected", code=code)

    def unknown(self, error):
        # The durable intent already prevents a later job from resending, even
        # if persisting this more specific outcome fails.
        try:
            self._outcome("unknown", error=str(error)[:500])
        except Exception:
            pass

    def finish(self):
        if not self.current:
            return
        def finish():
            entries = self.state["updated_failed_items"]
            entries[:] = [entry for entry in entries if not (
                entry["item_key"] == self.item["item_key"] and entry.get("last_error") == "WRITE_CONFIRMED")]
        self._commit(finish)
