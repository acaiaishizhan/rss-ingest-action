import json
import os
import sys
from pathlib import Path

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("RSS_INGEST_SKIP_LOCAL_ENV", "true")

from tools import run_keyword_alias_daily  # noqa: E402


WRITE_STEP_DRY_RUN_FLAGS = {
    "archive-old-records": "",
    "keyword-cleanup": "",
    "alias-update-apply": "--alias-update-apply-dry-run",
    "keyword-alias-links": "",
    "keyword-parent-rollup": "--dry-run",
    "keyword-core-sync": None,
    "keyword-expanded-links": "",
}

APPLY_STEPS = {
    "archive-old-records",
    "keyword-cleanup",
    "keyword-alias-links",
    "keyword-parent-rollup",
    "keyword-expanded-links",
}


def test_summarize_outputs_includes_parent_rollup_and_audit(tmp_path):
    archive_path = tmp_path / "archive.json"
    cleanup_path = tmp_path / "cleanup.json"
    noise_path = tmp_path / "noise.json"
    alias_path = tmp_path / "alias.json"
    preview_path = tmp_path / "preview.json"
    apply_path = tmp_path / "apply.json"
    link_path = tmp_path / "links.json"
    parent_path = tmp_path / "parent.json"
    expanded_path = tmp_path / "expanded.json"
    audit_path = tmp_path / "audit.json"
    for path in [alias_path, preview_path, apply_path, link_path]:
        path.write_text("{}", encoding="utf-8")
    archive_path.write_text(
        json.dumps(
            {
                "source_query": "filtered-30d-zero",
                "source_scanned": {"NEWS": 2},
                "plan": {"count": 2, "needs_create": 1, "already_archived": 1},
                "applied": {"created": 1, "deleted": 2},
                "missing_tables": [],
                "failed": [],
            }
        ),
        encoding="utf-8",
    )
    cleanup_path.write_text(json.dumps({"delete_count": 4, "deleted": 4, "failed": []}), encoding="utf-8")
    noise_path.write_text(
        json.dumps({"recent_keyword_count": 5, "candidate_count": 3, "decision_counts": {"block_auto": 2}, "errors": []}),
        encoding="utf-8",
    )
    parent_path.write_text(json.dumps({"parent_link_count": 3, "field_result": {"errors": []}}), encoding="utf-8")
    expanded_path.write_text(json.dumps({"news_update_count": 2, "failed": []}), encoding="utf-8")
    audit_path.write_text(json.dumps({"healthy": True, "compact_duplicate_groups": 0}), encoding="utf-8")

    summary = run_keyword_alias_daily.summarize_outputs(
        archive_path,
        cleanup_path,
        noise_path,
        alias_path,
        preview_path,
        apply_path,
        link_path,
        parent_path,
        expanded_path,
        audit_path,
        dry_run=False,
    )

    assert summary["cleanup"]["delete_count"] == 4
    assert summary["archive"]["planned"] == 2
    assert summary["archive"]["deleted"] == 2
    assert summary["cleanup"]["deleted"] == 4
    assert summary["noise_audit"]["recent_keyword_count"] == 5
    assert summary["noise_audit"]["candidate_count"] == 3
    assert summary["parent_rollup"]["parent_link_count"] == 3
    assert summary["expanded_link"]["news_update_count"] == 2
    assert summary["audit"]["healthy"] is True


def test_main_passes_model_and_incremental_options_to_alias_discovery(monkeypatch, tmp_path):
    snapshot_path = tmp_path / "keyword_snapshot.json"
    snapshot_path.write_text("[]", encoding="utf-8")
    out_dir = tmp_path / "daily"
    commands = []

    monkeypatch.setattr(run_keyword_alias_daily, "validate_env", lambda provider: [])

    def fake_run_step(name, command, log_path):
        commands.append((name, command))
        output_arg_names = {"--output", "--snapshot-output"}
        for index, arg in enumerate(command[:-1]):
            if arg in output_arg_names:
                path = Path(command[index + 1])
                path.parent.mkdir(parents=True, exist_ok=True)
                if arg == "--snapshot-output":
                    path.write_text(json.dumps({"entries": []}), encoding="utf-8")
                else:
                    path.write_text("{}", encoding="utf-8")
        return 0

    monkeypatch.setattr(run_keyword_alias_daily, "run_step", fake_run_step)

    rc = run_keyword_alias_daily.main(
        [
            "--out-dir",
            str(out_dir),
            "--provider",
            "gemini",
            "--model",
            "gemini-3-flash-preview",
            "--keyword-snapshot-path",
            str(snapshot_path),
            "--type-filter",
            "model,product",
            "--dry-run",
        ]
    )

    assert rc == 0
    cleanup_command = dict(commands)["keyword-cleanup"]
    noise_command = dict(commands)["keyword-noise-audit"]
    alias_command = dict(commands)["alias-discovery"]
    assert "--model" not in cleanup_command
    assert "--type-filter" not in cleanup_command
    assert "--keyword-snapshot-path" not in cleanup_command
    assert noise_command[noise_command.index("--model") + 1] == "gemini-3-flash-preview"
    assert noise_command[noise_command.index("--incremental-since-ms") + 1].isdigit()
    assert noise_command[noise_command.index("--limit") + 1] == "0"
    assert noise_command[noise_command.index("--batch-size") + 1] == "0"
    assert "--actionable-only" in noise_command
    assert "--type-filter" not in noise_command
    assert alias_command[alias_command.index("--model") + 1] == "gemini-3-flash-preview"
    assert alias_command[alias_command.index("--type-filter") + 1] == "model,product"
    assert alias_command[alias_command.index("--keyword-snapshot-path") + 1] == str(snapshot_path)


def test_default_batch_sizes_run_incremental_inputs_as_full_batches():
    args = run_keyword_alias_daily.parse_args([])

    assert args.batch_size == 9999
    assert args.noise_batch_size == 0


def test_build_steps_batches_noise_audit_for_full_runs(tmp_path):
    args = run_keyword_alias_daily.parse_args([])
    paths = run_keyword_alias_daily.DailyPaths.from_out_dir(tmp_path)

    steps = run_keyword_alias_daily.build_steps(
        args,
        paths,
        snapshot_path=tmp_path / "keyword_snapshot.json",
        incremental_since_ms=0,
    )
    noise_command = {step.name: step.command for step in steps}["keyword-noise-audit"]

    assert noise_command[noise_command.index("--batch-size") + 1] == "500"


def test_validate_env_accepts_vertex_gemini_without_api_key(monkeypatch):
    for name in run_keyword_alias_daily.BASE_REQUIRED_ENV:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("GEMINI_BACKEND", "vertex")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "project-93af2405-25ba-44b8-a3d")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(run_keyword_alias_daily.config, "GEMINI_BACKEND", "vertex", raising=False)

    assert run_keyword_alias_daily.validate_env("gemini") == []


def test_build_steps_dry_run_protects_or_skips_every_write_step(tmp_path):
    args = run_keyword_alias_daily.parse_args(["--dry-run"])
    paths = run_keyword_alias_daily.DailyPaths.from_out_dir(tmp_path)

    steps = run_keyword_alias_daily.build_steps(
        args,
        paths,
        snapshot_path=tmp_path / "keyword_snapshot.json",
        incremental_since_ms=0,
    )
    commands = {step.name: step.command for step in steps}

    for step_name, dry_run_flag in WRITE_STEP_DRY_RUN_FLAGS.items():
        if dry_run_flag is None:
            assert step_name not in commands
        elif dry_run_flag:
            assert dry_run_flag in commands[step_name]
            assert "--apply" not in commands[step_name]
        else:
            assert step_name in commands
            assert "--apply" not in commands[step_name]
    for step_name, command in commands.items():
        if step_name not in WRITE_STEP_DRY_RUN_FLAGS:
            assert "--apply" not in command


def test_build_steps_apply_adds_apply_only_to_expected_steps(tmp_path):
    args = run_keyword_alias_daily.parse_args([])
    paths = run_keyword_alias_daily.DailyPaths.from_out_dir(tmp_path)

    steps = run_keyword_alias_daily.build_steps(
        args,
        paths,
        snapshot_path=tmp_path / "keyword_snapshot.json",
        incremental_since_ms=0,
    )
    commands = {step.name: step.command for step in steps}

    for step_name in APPLY_STEPS:
        assert "--apply" in commands[step_name]
    for step_name, command in commands.items():
        if step_name not in APPLY_STEPS:
            assert "--apply" not in command
        assert "--alias-update-apply-dry-run" not in command
        assert "--dry-run" not in command
    assert "keyword-core-sync" in commands


def test_archive_step_runs_before_keyword_cleanup(tmp_path):
    args = run_keyword_alias_daily.parse_args([])
    paths = run_keyword_alias_daily.DailyPaths.from_out_dir(tmp_path)

    steps = run_keyword_alias_daily.build_steps(
        args,
        paths,
        snapshot_path=tmp_path / "keyword_snapshot.json",
        incremental_since_ms=0,
    )
    names = [step.name for step in steps]

    assert names.index("archive-old-records") < names.index("keyword-cleanup")
    archive_command = {step.name: step.command for step in steps}["archive-old-records"]
    assert "tools/archive_old_records.py" in archive_command
    assert "--scan-all" not in archive_command


def test_main_refreshes_run_snapshot_with_alias_preview_before_link_step(monkeypatch, tmp_path):
    snapshot_path = tmp_path / "keyword_snapshot.json"
    snapshot_path.write_text("[]", encoding="utf-8")
    out_dir = tmp_path / "daily"
    link_snapshot_entries = []

    monkeypatch.setattr(run_keyword_alias_daily, "validate_env", lambda provider: [])
    monkeypatch.setattr(run_keyword_alias_daily, "load_snapshot_entries", lambda path: ["before-preview"])

    def fake_load_json(path):
        return {"updates": [{"canonical_record_id": "rec_main", "new_aliases": ["Alias"]}]}

    def fake_apply_alias_preview_to_entries(entries, preview):
        assert entries == ["before-preview"]
        assert preview["updates"]
        return ["after-preview"]

    def fake_write_snapshot_entries(path, entries, source=""):
        assert source == "daily-alias-preview"
        link_snapshot_entries[:] = list(entries)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("preview-applied", encoding="utf-8")

    monkeypatch.setattr(run_keyword_alias_daily, "load_json", fake_load_json)
    monkeypatch.setattr(run_keyword_alias_daily, "apply_alias_preview_to_entries", fake_apply_alias_preview_to_entries)
    monkeypatch.setattr(run_keyword_alias_daily, "write_snapshot_entries", fake_write_snapshot_entries)

    def fake_run_step(name, command, log_path):
        output_arg_names = {"--output", "--snapshot-output"}
        for index, arg in enumerate(command[:-1]):
            if arg in output_arg_names:
                path = Path(command[index + 1])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
        if name == "keyword-alias-links":
            assert link_snapshot_entries == ["after-preview"]
        return 0

    monkeypatch.setattr(run_keyword_alias_daily, "run_step", fake_run_step)

    rc = run_keyword_alias_daily.main(
        [
            "--out-dir",
            str(out_dir),
            "--provider",
            "gemini",
            "--keyword-snapshot-path",
            str(snapshot_path),
            "--dry-run",
        ]
    )

    assert rc == 0


def test_main_allows_unhealthy_keyword_audit_in_dry_run(monkeypatch, tmp_path):
    out_dir = tmp_path / "daily"

    monkeypatch.setattr(run_keyword_alias_daily, "validate_env", lambda provider: [])

    def fake_run_step(name, command, log_path):
        output_arg_names = {"--output", "--snapshot-output"}
        for index, arg in enumerate(command[:-1]):
            if arg in output_arg_names:
                path = Path(command[index + 1])
                path.parent.mkdir(parents=True, exist_ok=True)
                if name == "keyword-audit":
                    path.write_text(
                        json.dumps(
                            {
                                "healthy": False,
                                "merged_linked_count": 2,
                                "compact_duplicate_groups": 3,
                            }
                        ),
                        encoding="utf-8",
                    )
                elif arg == "--snapshot-output":
                    path.write_text(json.dumps({"entries": []}), encoding="utf-8")
                else:
                    path.write_text("{}", encoding="utf-8")
        return 1 if name == "keyword-audit" else 0

    monkeypatch.setattr(run_keyword_alias_daily, "run_step", fake_run_step)

    rc = run_keyword_alias_daily.main(["--out-dir", str(out_dir), "--provider", "gemini", "--dry-run"])

    assert rc == 0
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["audit"]["healthy"] is False
    assert summary["failures"] == []


def test_main_ignores_stale_post_repair_audit_when_repair_not_triggered(monkeypatch, tmp_path):
    out_dir = tmp_path / "daily"
    out_dir.mkdir()
    stale_post_repair = out_dir / "09-keyword-audit-post-repair.json"
    stale_post_repair.write_text(
        json.dumps(
            {
                "healthy": False,
                "merged_linked_count": 6,
                "compact_duplicate_groups": 14,
                "zero_link_keyword_count": 41,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(run_keyword_alias_daily, "validate_env", lambda provider: [])

    def fake_run_step(name, command, log_path):
        output_arg_names = {"--output", "--snapshot-output"}
        for index, arg in enumerate(command[:-1]):
            if arg in output_arg_names:
                path = Path(command[index + 1])
                path.parent.mkdir(parents=True, exist_ok=True)
                if name == "keyword-audit":
                    path.write_text(
                        json.dumps(
                            {
                                "healthy": True,
                                "merged_linked_count": 0,
                                "compact_duplicate_groups": 0,
                                "zero_link_keyword_count": 38,
                            }
                        ),
                        encoding="utf-8",
                    )
                elif arg == "--snapshot-output":
                    path.write_text(json.dumps({"entries": []}), encoding="utf-8")
                else:
                    path.write_text("{}", encoding="utf-8")
        return 0

    monkeypatch.setattr(run_keyword_alias_daily, "run_step", fake_run_step)

    rc = run_keyword_alias_daily.main(["--out-dir", str(out_dir), "--provider", "gemini", "--dry-run"])

    assert rc == 0
    assert not stale_post_repair.exists()
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["audit"]["healthy"] is True
    assert summary["audit"]["compact_duplicate_groups"] == 0


def test_run_step_forces_utf8_output_decoding(monkeypatch, tmp_path):
    captured = {}

    class Result:
        returncode = 0
        stdout = "ok"

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return Result()

    monkeypatch.setattr(run_keyword_alias_daily.subprocess, "run", fake_run)

    rc = run_keyword_alias_daily.run_step("demo", ["python", "--version"], tmp_path / "demo.log")

    assert rc == 0
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"
