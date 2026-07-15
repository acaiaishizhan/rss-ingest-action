import json
import os
import sys
from pathlib import Path

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

from tools import run_keyword_audit_repair  # noqa: E402


def test_build_steps_dry_run_does_not_apply_writes(tmp_path):
    args = run_keyword_audit_repair.parse_args(["--out-dir", str(tmp_path)])
    paths = run_keyword_audit_repair.RepairPaths.from_out_dir(tmp_path)

    steps = run_keyword_audit_repair.build_steps(args, paths)
    commands = {step.name: step.command for step in steps}

    assert "--apply" not in commands["keyword-duplicate-links"]
    assert "--apply" not in commands["zero-link-keyword-cleanup"]
    assert "--apply" not in commands["keyword-parent-rollup"]
    assert "keyword-audit-before" in commands
    assert "keyword-audit-after" in commands


def test_build_steps_apply_only_enables_repair_writes(tmp_path):
    args = run_keyword_audit_repair.parse_args(["--out-dir", str(tmp_path), "--apply"])
    paths = run_keyword_audit_repair.RepairPaths.from_out_dir(tmp_path)

    steps = run_keyword_audit_repair.build_steps(args, paths)
    commands = {step.name: step.command for step in steps}

    assert "--apply" in commands["keyword-duplicate-links"]
    assert "--apply" in commands["zero-link-keyword-cleanup"]
    assert "--apply" in commands["keyword-parent-rollup"]
    assert "--apply" not in commands["keyword-audit-before"]
    assert "--apply" not in commands["keyword-duplicate-audit"]
    assert "--apply" not in commands["keyword-audit-after"]


def test_main_returns_nonzero_when_final_audit_is_unhealthy(monkeypatch, tmp_path):
    monkeypatch.setattr(run_keyword_audit_repair, "validate_env", lambda: [])

    def fake_run_step(name, command, log_path):
        for index, arg in enumerate(command[:-1]):
            if arg == "--output":
                path = Path(command[index + 1])
                path.parent.mkdir(parents=True, exist_ok=True)
                if name == "keyword-audit-after":
                    path.write_text('{"healthy": false, "compact_duplicate_groups": 1, "zero_link_keyword_count": 2}', encoding="utf-8")
                elif name == "keyword-duplicate-audit":
                    path.write_text('{"candidate_count": 3, "needs_relink_count": 2, "duplicate_group_count": 1}', encoding="utf-8")
                elif name == "keyword-duplicate-links":
                    path.write_text('{"plan_count": 3, "updated": {"news": 1, "filtered": 0, "keyword_notes": 2}, "failed": {"news": [], "filtered": [], "keyword_notes": []}}', encoding="utf-8")
                elif name == "zero-link-keyword-cleanup":
                    path.write_text('{"delete_count": 2, "deleted": 2, "failed": []}', encoding="utf-8")
                else:
                    path.write_text('{"healthy": true}', encoding="utf-8")
        return 0

    monkeypatch.setattr(run_keyword_audit_repair, "run_step", fake_run_step)

    rc = run_keyword_audit_repair.main(["--out-dir", str(tmp_path), "--apply"])

    assert rc == 1
    assert (tmp_path / "summary.json").exists()


def test_main_refreshes_runtime_snapshot_after_successful_apply(monkeypatch, tmp_path):
    monkeypatch.setattr(run_keyword_audit_repair, "validate_env", lambda: [])
    refreshed = []

    def fake_run_step(name, command, log_path):
        for index, arg in enumerate(command[:-1]):
            if arg == "--output":
                path = Path(command[index + 1])
                path.parent.mkdir(parents=True, exist_ok=True)
                if name in {"keyword-audit-before", "keyword-audit-after"}:
                    path.write_text(
                        '{"healthy": true, "compact_duplicate_groups": 0, "zero_link_keyword_count": 0}',
                        encoding="utf-8",
                    )
                elif name == "keyword-duplicate-audit":
                    path.write_text(
                        '{"candidate_count": 0, "needs_relink_count": 0, "duplicate_group_count": 0}',
                        encoding="utf-8",
                    )
                elif name == "keyword-duplicate-links":
                    path.write_text(
                        '{"plan_count": 0, "updated": {"news": 0, "filtered": 0, "keyword_notes": 0}, "failed": {"news": [], "filtered": [], "keyword_notes": []}}',
                        encoding="utf-8",
                    )
                elif name == "zero-link-keyword-cleanup":
                    path.write_text('{"delete_count": 0, "deleted": 0, "failed": []}', encoding="utf-8")
                elif name == "keyword-parent-rollup":
                    path.write_text('{"parent_link_count": 0, "field_result": {"errors": []}, "apply_result": {"failed": 0}}', encoding="utf-8")
        return 0

    monkeypatch.setattr(run_keyword_audit_repair, "run_step", fake_run_step)
    monkeypatch.setattr(
        run_keyword_audit_repair,
        "refresh_runtime_keyword_snapshot",
        lambda page_size, max_pages: refreshed.append((page_size, max_pages)) or 12,
        raising=False,
    )

    rc = run_keyword_audit_repair.main(["--out-dir", str(tmp_path), "--apply"])

    assert rc == 0
    assert refreshed == [(500, 200)]
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["runtime_snapshot"]["refreshed"] is True
    assert summary["runtime_snapshot"]["entry_count"] == 12


def test_collect_cleanup_exclusions_from_duplicate_and_merged_targets(tmp_path):
    paths = run_keyword_audit_repair.RepairPaths.from_out_dir(tmp_path)
    paths.audit_before.write_text(
        """
{
  "merged_linked_details": [
    {
      "merged_keyword_targets": [
        {"target_record_ids": ["rec_from_note", ""]},
        {"target_record_ids": ["rec_second_note"]}
      ]
    }
  ]
}
""".strip(),
        encoding="utf-8",
    )
    paths.duplicate_audit.write_text(
        """
{
  "candidates": [
    {"old_record_id": "rec_old", "target_record_id": "rec_from_duplicate"},
    {"old_record_id": "rec_missing", "target_record_id": ""}
  ]
}
""".strip(),
        encoding="utf-8",
    )

    assert run_keyword_audit_repair.collect_cleanup_exclusions(paths) == {
        "rec_from_duplicate",
        "rec_from_note",
        "rec_second_note",
    }


def test_stale_final_audit_cannot_hide_current_audit_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(run_keyword_audit_repair, "validate_env", lambda: [])
    stale_after = tmp_path / "05-keyword-audit-after.json"
    stale_after.write_text('{"healthy": true}', encoding="utf-8")
    refreshed = []

    def fake_run_step(name, command, log_path):
        output_path = None
        for index, arg in enumerate(command[:-1]):
            if arg == "--output":
                output_path = Path(command[index + 1])
                break
        if name == "keyword-audit-after":
            return 2
        if output_path is not None:
            payloads = {
                "keyword-audit-before": {"healthy": True},
                "keyword-duplicate-audit": {"candidates": []},
                "keyword-duplicate-links": {"updated": {}, "failed": {}},
                "zero-link-keyword-cleanup": {"failed": []},
                "keyword-parent-rollup": {"field_result": {"errors": []}, "apply_result": {"failed": 0}},
            }
            output_path.write_text(json.dumps(payloads.get(name, {})), encoding="utf-8")
        return 0

    monkeypatch.setattr(run_keyword_audit_repair, "run_step", fake_run_step)
    monkeypatch.setattr(
        run_keyword_audit_repair,
        "refresh_runtime_keyword_snapshot",
        lambda *args: refreshed.append(True) or 1,
    )

    rc = run_keyword_audit_repair.main(["--out-dir", str(tmp_path), "--apply"])

    assert rc == 1
    assert refreshed == []
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["failures"] == [{"step": "keyword-audit-after", "exit_code": 2}]
