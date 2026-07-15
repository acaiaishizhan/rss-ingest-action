# -*- coding: utf-8 -*-
"""Run the daily keyword alias normalization pipeline.

This script writes alias metadata, keyword-record links, and maintenance reports:
1. discover aliases with the three-stage LLM workflow;
2. append accepted aliases to KEYWORD records;
3. relink historical NEWS/FILTERED keyword records to canonical KEYWORD records.
4. refresh parent links and expand NEWS/FILTERED links with parent/owner keywords.
5. sync core fields and audit keyword health.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
from tools.keyword_snapshot import apply_alias_preview_to_entries, load_snapshot_entries, write_snapshot_entries

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_REQUIRED_ENV = [
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_APP_TOKEN",
    "FEISHU_KEYWORD_TABLE_ID",
    "FEISHU_NEWS_TABLE_ID",
    "FEISHU_FILTERED_TABLE_ID",
]
FULL_NOISE_AUDIT_BATCH_SIZE = 500


@dataclass
class DailyPaths:
    archive: Path
    alias_discovery: Path
    cleanup: Path
    noise_audit: Path
    preview: Path
    apply: Path
    link: Path
    parent: Path
    expanded_link: Path
    audit: Path
    repair: Path
    audit_post_repair: Path
    run_snapshot: Path

    @classmethod
    def from_out_dir(cls, out_dir: Path) -> "DailyPaths":
        return cls(
            archive=out_dir / "00-archive-old-records.json",
            alias_discovery=out_dir / "01-alias-discovery.json",
            cleanup=out_dir / "00-keyword-cleanup.json",
            noise_audit=out_dir / "00b-keyword-noise-audit.json",
            preview=out_dir / "02-alias-update-preview.json",
            apply=out_dir / "03-alias-update-apply.json",
            link=out_dir / "04-keyword-alias-links.json",
            parent=out_dir / "05-keyword-parent-rollup.json",
            expanded_link=out_dir / "06-keyword-expanded-links.json",
            audit=out_dir / "07-keyword-audit.json",
            repair=out_dir / "08-keyword-audit-repair.json",
            audit_post_repair=out_dir / "09-keyword-audit-post-repair.json",
            run_snapshot=out_dir / "keyword_snapshot.next.json",
        )


@dataclass
class StepSpec:
    name: str
    command: List[str]
    writes_feishu: bool = False
    dry_run_args: List[str] = field(default_factory=list)
    apply_args: List[str] = field(default_factory=list)
    skip_in_dry_run: bool = False

    def command_for_mode(self, dry_run: bool) -> List[str]:
        if dry_run:
            return [*self.command, *self.dry_run_args]
        return [*self.command, *self.apply_args]


def _clean(value: Any) -> str:
    return str(value or "").strip()


def validate_env(provider: str) -> List[str]:
    missing: List[str] = []
    required = list(BASE_REQUIRED_ENV)
    normalized_provider = _clean(provider).lower()
    if normalized_provider == "gemini":
        if _clean(getattr(config, "GEMINI_BACKEND", "")) == "vertex":
            required.append("GOOGLE_CLOUD_PROJECT")
        else:
            required.append("GEMINI_API_KEY")
    for name in required:
        if not _clean(os.getenv(name)):
            missing.append(name)
    return missing


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_run_outputs(paths: DailyPaths, out_dir: Path) -> None:
    for path in [
        paths.archive,
        paths.cleanup,
        paths.noise_audit,
        paths.alias_discovery,
        paths.preview,
        paths.apply,
        paths.link,
        paths.parent,
        paths.expanded_link,
        paths.audit,
        paths.repair,
        paths.audit_post_repair,
        paths.run_snapshot,
        out_dir / "summary.json",
        out_dir / "summary.md",
    ]:
        path.unlink(missing_ok=True)


def run_step(name: str, command: List[str], log_path: Path) -> int:
    print(f"\n[daily-alias] {name}")
    print("[daily-alias] " + " ".join(command))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log_file:
        proc = subprocess.run(
            command,
            cwd=ROOT_DIR,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        log_file.write(proc.stdout or "")
    if proc.stdout:
        print(proc.stdout)
    print(f"[daily-alias] {name} exit={proc.returncode} log={log_path}")
    return proc.returncode


def summarize_outputs(
    archive_path: Path,
    cleanup_path: Path,
    noise_audit_path: Path,
    alias_discovery_path: Path,
    preview_path: Path,
    apply_path: Path,
    link_path: Path,
    parent_path: Path,
    expanded_link_path: Path,
    audit_path: Path,
    dry_run: bool,
) -> Dict[str, Any]:
    archive_payload = load_json(archive_path) if archive_path.exists() else {}
    cleanup_payload = load_json(cleanup_path) if cleanup_path.exists() else {}
    noise_payload = load_json(noise_audit_path) if noise_audit_path.exists() else {}
    alias_payload = load_json(alias_discovery_path) if alias_discovery_path.exists() else {}
    preview_payload = load_json(preview_path) if preview_path.exists() else {}
    apply_payload = load_json(apply_path) if apply_path.exists() else {}
    link_payload = load_json(link_path) if link_path.exists() else {}
    parent_payload = load_json(parent_path) if parent_path.exists() else {}
    expanded_link_payload = load_json(expanded_link_path) if expanded_link_path.exists() else {}
    audit_payload = load_json(audit_path) if audit_path.exists() else {}

    alias_summary = alias_payload.get("summary") or {}
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "archive": {
            "source_query": archive_payload.get("source_query", ""),
            "source_scanned": archive_payload.get("source_scanned", {}),
            "planned": (archive_payload.get("plan") or {}).get("count", 0),
            "needs_create": (archive_payload.get("plan") or {}).get("needs_create", 0),
            "already_archived": (archive_payload.get("plan") or {}).get("already_archived", 0),
            "created": (archive_payload.get("applied") or {}).get("created", 0),
            "deleted": (archive_payload.get("applied") or {}).get("deleted", 0),
            "missing_tables": archive_payload.get("missing_tables", []),
            "failed_count": len(archive_payload.get("failed") or []),
        },
        "cleanup": {
            "keyword_scanned": cleanup_payload.get("keyword_scanned", 0),
            "delete_count": cleanup_payload.get("delete_count", 0),
            "deleted": cleanup_payload.get("deleted", 0),
            "failed_count": len(cleanup_payload.get("failed") or []),
            "missing_first_seen_count": cleanup_payload.get("missing_first_seen_count", 0),
        },
        "noise_audit": {
            "keyword_scanned": noise_payload.get("keyword_scanned", 0),
            "recent_keyword_count": noise_payload.get("recent_keyword_count", 0),
            "candidate_count": noise_payload.get("candidate_count", 0),
            "decision_counts": noise_payload.get("decision_counts", {}),
            "error_count": len(noise_payload.get("errors") or []),
        },
        "alias_discovery": {
            "keyword_count": alias_payload.get("keyword_count", 0),
            "types_scanned": alias_payload.get("types_scanned", []),
            "stage1_candidate_groups": alias_summary.get("stage1_candidate_groups", 0),
            "stage2_vetoed_pairs": alias_summary.get("stage2_vetoed_pairs", 0),
            "stage3_accepted": alias_summary.get("stage3_accepted", 0),
            "stage3_rejected": alias_summary.get("stage3_rejected", 0),
        },
        "alias_update": {
            "accepted_count": preview_payload.get("accepted_count", 0),
            "update_count": preview_payload.get("update_count", 0),
            "skipped_count": preview_payload.get("skipped_count", 0),
            "target_count": apply_payload.get("target_count", 0),
            "updated": apply_payload.get("updated", 0),
            "failed_count": len(apply_payload.get("failed") or []),
        },
        "alias_link": {
            "alias_record_plan_count": link_payload.get("alias_record_plan_count", 0),
            "conflict_count": link_payload.get("conflict_count", 0),
            "news_update_count": link_payload.get("news_update_count", 0),
            "filtered_update_count": link_payload.get("filtered_update_count", 0),
            "keyword_note_update_count": link_payload.get("keyword_note_update_count", 0),
            "updated": link_payload.get("updated", {}),
            "failed_count": len(link_payload.get("failed") or []),
        },
        "parent_rollup": {
            "parent_link_count": parent_payload.get("parent_link_count", 0),
            "field_error_count": len((parent_payload.get("field_result") or {}).get("errors") or []),
            "failed": (parent_payload.get("apply_result") or {}).get("failed", 0),
        },
        "expanded_link": {
            "news_update_count": expanded_link_payload.get("news_update_count", 0),
            "filtered_update_count": expanded_link_payload.get("filtered_update_count", 0),
            "updated": expanded_link_payload.get("updated", {}),
            "failed_count": len(expanded_link_payload.get("failed") or []),
        },
        "audit": {
            "healthy": audit_payload.get("healthy"),
            "merged_linked_count": audit_payload.get("merged_linked_count", 0),
            "generic_linked_count": audit_payload.get("generic_linked_count", 0),
            "exact_generic_keyword_count": audit_payload.get("exact_generic_keyword_count", 0),
            "compact_duplicate_groups": audit_payload.get("compact_duplicate_groups", 0),
            "zero_link_keyword_count": audit_payload.get("zero_link_keyword_count", 0),
        },
    }
    return summary


def write_markdown_summary(path: Path, summary: Dict[str, Any]) -> None:
    archive = summary["archive"]
    cleanup = summary["cleanup"]
    noise = summary["noise_audit"]
    alias = summary["alias_discovery"]
    update = summary["alias_update"]
    link = summary["alias_link"]
    parent = summary["parent_rollup"]
    expanded = summary["expanded_link"]
    audit = summary["audit"]
    text = f"""# Keyword Alias Daily Summary

- generated_at: {summary["generated_at"]}
- dry_run: {summary["dry_run"]}

## Old Record Archive

- source query: {archive["source_query"] or "-"}
- source scanned: {json.dumps(archive["source_scanned"], ensure_ascii=False)}
- planned: {archive["planned"]}
- needs create: {archive["needs_create"]}
- already archived: {archive["already_archived"]}
- created: {archive["created"]}
- deleted: {archive["deleted"]}
- missing tables: {", ".join(archive["missing_tables"]) if archive["missing_tables"] else "-"}
- failed: {archive["failed_count"]}

## Stale Keyword Cleanup

- keywords scanned: {cleanup["keyword_scanned"]}
- delete candidates: {cleanup["delete_count"]}
- deleted: {cleanup["deleted"]}
- failed: {cleanup["failed_count"]}
- missing first_seen: {cleanup["missing_first_seen_count"]}

## Keyword Noise LLM Audit

- keywords scanned: {noise["keyword_scanned"]}
- recent keywords: {noise["recent_keyword_count"]}
- candidates reviewed: {noise["candidate_count"]}
- decisions: {json.dumps(noise["decision_counts"], ensure_ascii=False)}
- errors: {noise["error_count"]}

## Alias Discovery

- keywords: {alias["keyword_count"]}
- types: {", ".join(alias["types_scanned"]) if alias["types_scanned"] else "-"}
- recall groups: {alias["stage1_candidate_groups"]}
- vetoed pairs: {alias["stage2_vetoed_pairs"]}
- accepted pairs: {alias["stage3_accepted"]}
- rejected pairs: {alias["stage3_rejected"]}

## Alias Update

- accepted: {update["accepted_count"]}
- keyword records to update: {update["target_count"]}
- keyword records updated: {update["updated"]}
- skipped: {update["skipped_count"]}
- failed: {update["failed_count"]}

## Historical Link Relink

- alias plans: {link["alias_record_plan_count"]}
- conflicts skipped: {link["conflict_count"]}
- NEWS updates planned: {link["news_update_count"]}
- FILTERED updates planned: {link["filtered_update_count"]}
- KEYWORD note updates planned: {link["keyword_note_update_count"]}
- updated: {json.dumps(link["updated"], ensure_ascii=False)}
- failed: {link["failed_count"]}

## Parent Rollup

- parent links: {parent["parent_link_count"]}
- field errors: {parent["field_error_count"]}
- failed: {parent["failed"]}

## Expanded Keyword Links

- NEWS updates planned: {expanded["news_update_count"]}
- FILTERED updates planned: {expanded["filtered_update_count"]}
- updated: {json.dumps(expanded["updated"], ensure_ascii=False)}
- failed: {expanded["failed_count"]}

## Keyword Audit

- healthy: {audit["healthy"]}
- merged linked: {audit["merged_linked_count"]}
- generic linked: {audit["generic_linked_count"]}
- exact generic keywords: {audit["exact_generic_keyword_count"]}
- compact duplicate groups: {audit["compact_duplicate_groups"]}
- zero-link keywords: {audit["zero_link_keyword_count"]}
"""
    repair = summary.get("audit_repair")
    if repair and repair.get("triggered"):
        text += f"""
## Audit Repair

- input records: {repair.get("input_count", 0)}
- updated: {json.dumps(repair.get("updated", {}), ensure_ascii=False)}
- failed: {repair.get("failed_count", 0)}
"""
    path.write_text(text, encoding="utf-8")


def apply_alias_preview_to_snapshot(snapshot_path: Path, preview_path: Path, source: str) -> bool:
    if not snapshot_path.exists() or not preview_path.exists():
        return False
    entries = load_snapshot_entries(snapshot_path)
    preview = load_json(preview_path)
    entries = apply_alias_preview_to_entries(entries, preview)
    write_snapshot_entries(snapshot_path, entries, source=source)
    return True


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run daily keyword alias normalization.")
    parser.add_argument("--out-dir", default="out/keyword-alias-daily")
    parser.add_argument("--provider", default="gemini")
    parser.add_argument("--model", default="")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--batch-size", type=int, default=9999, help="Alias discovery batch size; default means each keyword type runs as one full batch.")
    parser.add_argument("--noise-batch-size", type=int, default=0, help="Noise audit LLM batch size; 0 means all recent candidates in one request.")
    parser.add_argument("--type-filter", default="")
    parser.add_argument("--keyword-update-limit", type=int, default=0)
    parser.add_argument("--link-update-limit", type=int, default=0)
    parser.add_argument("--link-update-sleep", type=float, default=0.05)
    parser.add_argument("--keyword-snapshot-path", default="data/keyword_snapshot.json")
    parser.add_argument("--incremental-hours", type=float, default=25)
    parser.add_argument("--record-recent-hours", type=float, default=25)
    parser.add_argument("--record-max-pages", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def build_step_specs(
    args: argparse.Namespace,
    paths: DailyPaths,
    snapshot_path: Path,
    incremental_since_ms: int,
) -> List[StepSpec]:
    noise_batch_size = args.noise_batch_size
    if incremental_since_ms <= 0 and noise_batch_size <= 0:
        noise_batch_size = FULL_NOISE_AUDIT_BATCH_SIZE

    steps = [
        StepSpec(
            name="archive-old-records",
            command=[
                sys.executable,
                "tools/archive_old_records.py",
                "--output",
                str(paths.archive),
                "--max-pages",
                str(args.max_pages),
                "--page-size",
                str(args.page_size),
            ],
            writes_feishu=True,
            apply_args=["--apply"],
        ),
        StepSpec(
            name="keyword-cleanup",
            command=[
                sys.executable,
                "tools/cleanup_stale_keywords.py",
                "--output",
                str(paths.cleanup),
                "--max-pages",
                str(args.max_pages),
                "--page-size",
                str(args.page_size),
            ],
            writes_feishu=True,
            apply_args=["--apply"],
        ),
        StepSpec(
            name="keyword-noise-audit",
            command=[
                sys.executable,
                "tools/audit_keyword_noise_llm.py",
                "--provider",
                args.provider,
                "--page-size",
                str(args.page_size),
                "--max-pages",
                str(args.max_pages),
                "--output",
                str(paths.noise_audit),
                "--limit",
                "0",
                "--batch-size",
                str(noise_batch_size),
                "--actionable-only",
            ],
        ),
        StepSpec(
            name="alias-discovery",
            command=[
                sys.executable,
                "alias_discovery.py",
                "--three-stage",
                "--provider",
                args.provider,
                "--page-size",
                str(args.page_size),
                "--max-pages",
                str(args.max_pages),
                "--batch-size",
                str(args.batch_size),
                "--output",
                str(paths.alias_discovery),
                "--snapshot-output",
                str(paths.run_snapshot),
            ],
        ),
        StepSpec(
            name="alias-update-preview",
            command=[
                sys.executable,
                "merge_keywords.py",
                "--alias-update-preview",
                "--alias-discovery-path",
                str(paths.alias_discovery),
                "--page-size",
                str(args.page_size),
                "--max-pages",
                str(args.max_pages),
                "--output",
                str(paths.preview),
                "--keyword-snapshot-path",
                str(paths.run_snapshot),
            ],
        ),
        StepSpec(
            name="alias-update-apply",
            command=[
                sys.executable,
                "merge_keywords.py",
                "--alias-update-apply",
                str(paths.preview),
                "--page-size",
                str(args.page_size),
                "--max-pages",
                str(args.max_pages),
                "--update-limit",
                str(args.keyword_update_limit),
                "--output",
                str(paths.apply),
            ],
            writes_feishu=True,
            dry_run_args=["--alias-update-apply-dry-run"],
        ),
        StepSpec(
            name="keyword-alias-links",
            command=[
                sys.executable,
                "tools/apply_keyword_alias_links.py",
                "--page-size",
                str(args.page_size),
                "--max-pages",
                str(args.max_pages),
                "--update-limit",
                str(args.link_update_limit),
                "--link-update-sleep",
                str(args.link_update_sleep),
                "--output",
                str(paths.link),
                "--keyword-snapshot-path",
                str(paths.run_snapshot),
                "--recent-hours",
                str(args.record_recent_hours),
                "--record-max-pages",
                str(args.record_max_pages),
            ],
            writes_feishu=True,
            apply_args=["--apply"],
        ),
        StepSpec(
            name="keyword-parent-rollup",
            command=[
                sys.executable,
                "tools/keyword_parent_rollup.py",
                "--output",
                str(paths.parent),
                "--max-pages",
                str(args.max_pages),
            ],
            writes_feishu=True,
            dry_run_args=["--dry-run"],
            apply_args=["--apply"],
        ),
        StepSpec(
            name="keyword-core-sync",
            command=[
                sys.executable,
                "merge_keywords.py",
                "--sync-core-fields",
                "--max-pages",
                str(args.max_pages),
                "--usage-max-pages",
                str(args.max_pages),
                "--page-size",
                str(args.page_size),
            ],
            writes_feishu=True,
            skip_in_dry_run=True,
        ),
        StepSpec(
            name="keyword-expanded-links",
            command=[
                sys.executable,
                "tools/sync_keyword_expanded_links.py",
                "--output",
                str(paths.expanded_link),
                "--max-pages",
                str(args.max_pages),
                "--page-size",
                str(args.page_size),
                "--recent-hours",
                "0",
                "--record-max-pages",
                str(args.max_pages),
                "--link-update-sleep",
                str(args.link_update_sleep),
            ],
            writes_feishu=True,
            apply_args=["--apply"],
        ),
        StepSpec(
            name="keyword-audit",
            command=[
                sys.executable,
                "tools/audit_keywords.py",
                "--output",
                str(paths.audit),
                "--max-pages",
                str(args.max_pages),
            ],
        ),
    ]

    steps_by_name = {step.name: step for step in steps}
    noise_step = steps_by_name["keyword-noise-audit"]
    alias_step = steps_by_name["alias-discovery"]

    if snapshot_path and snapshot_path.exists() and incremental_since_ms > 0:
        noise_step.command.extend([
            "--incremental-since-ms",
            str(incremental_since_ms),
        ])
        alias_step.command.extend([
            "--keyword-snapshot-path",
            str(snapshot_path),
            "--incremental-since-ms",
            str(incremental_since_ms),
        ])
    if args.model:
        noise_step.command.extend(["--model", args.model])
        alias_step.command.extend(["--model", args.model])
    if args.type_filter:
        alias_step.command.extend(["--type-filter", args.type_filter])
    return steps


def build_steps(
    args: argparse.Namespace,
    paths: DailyPaths,
    snapshot_path: Path,
    incremental_since_ms: int,
) -> List[StepSpec]:
    steps: List[StepSpec] = []
    for spec in build_step_specs(args, paths, snapshot_path, incremental_since_ms):
        if args.dry_run and spec.skip_in_dry_run:
            continue
        steps.append(
            StepSpec(
                name=spec.name,
                command=spec.command_for_mode(args.dry_run),
                writes_feishu=spec.writes_feishu,
            )
        )
    return steps


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv)
    missing = validate_env(args.provider)
    out_dir = ROOT_DIR / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    if missing:
        summary = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "error": "missing required environment variables",
            "missing": missing,
        }
        write_json(out_dir / "00-env-error.json", summary)
        print(f"[daily-alias] missing required env: {', '.join(missing)}")
        return 2

    paths = DailyPaths.from_out_dir(out_dir)
    clear_run_outputs(paths, out_dir)
    snapshot_path = ROOT_DIR / args.keyword_snapshot_path if args.keyword_snapshot_path else Path("")
    incremental_since_ms = 0
    if snapshot_path and snapshot_path.exists() and args.incremental_hours > 0:
        incremental_since_ms = int((datetime.now().timestamp() - args.incremental_hours * 3600) * 1000)

    steps = build_steps(args, paths, snapshot_path, incremental_since_ms)

    failures: List[Dict[str, Any]] = []
    run_snapshot_preview_applied = False
    audit_failed_repairable = False
    for step in steps:
        code = run_step(step.name, step.command, out_dir / f"{step.name}.log")
        if code != 0:
            if args.dry_run and step.name == "keyword-audit" and paths.audit.exists():
                continue
            if step.name == "keyword-audit" and paths.audit.exists():
                audit_payload = load_json(paths.audit)
                if int(audit_payload.get("merged_linked_count") or 0) > 0:
                    audit_failed_repairable = True
                    continue
            failures.append({"step": step.name, "exit_code": code})
            break
        if step.name == "alias-update-preview":
            run_snapshot_preview_applied = apply_alias_preview_to_snapshot(
                paths.run_snapshot,
                paths.preview,
                source="daily-alias-preview",
            )

    if audit_failed_repairable and not args.dry_run:
        repair_cmd = [
            sys.executable,
            "tools/apply_keyword_alias_links.py",
            "--repair-audit-path",
            str(paths.audit),
            "--apply",
            "--page-size",
            str(args.page_size),
            "--max-pages",
            str(args.max_pages),
            "--link-update-sleep",
            str(args.link_update_sleep),
            "--output",
            str(paths.repair),
        ]
        if paths.run_snapshot.exists():
            repair_cmd.extend(["--keyword-snapshot-path", str(paths.run_snapshot)])
        repair_code = run_step("keyword-audit-repair", repair_cmd, out_dir / "keyword-audit-repair.log")
        if repair_code != 0:
            failures.append({"step": "keyword-audit-repair", "exit_code": repair_code})
        else:
            reaudit_cmd = [
                sys.executable,
                "tools/audit_keywords.py",
                "--output",
                str(paths.audit_post_repair),
                "--max-pages",
                str(args.max_pages),
            ]
            reaudit_code = run_step("keyword-audit-post-repair", reaudit_cmd, out_dir / "keyword-audit-post-repair.log")
            if reaudit_code != 0 and paths.audit_post_repair.exists():
                failures.append({"step": "keyword-audit-post-repair", "exit_code": reaudit_code})

    final_audit_path = paths.audit_post_repair if audit_failed_repairable and paths.audit_post_repair.exists() else paths.audit
    summary = summarize_outputs(
        paths.archive,
        paths.cleanup,
        paths.noise_audit,
        paths.alias_discovery,
        paths.preview,
        paths.apply,
        paths.link,
        paths.parent,
        paths.expanded_link,
        final_audit_path,
        args.dry_run,
    )
    summary["failures"] = failures
    if audit_failed_repairable:
        repair_payload = load_json(paths.repair) if paths.repair.exists() else {}
        summary["audit_repair"] = {
            "triggered": True,
            "input_count": repair_payload.get("input_count", 0),
            "updated": repair_payload.get("updated", {}),
            "failed_count": len(repair_payload.get("failed") or []),
        }
    summary["snapshot"] = {
        "path": str(snapshot_path) if snapshot_path else "",
        "run_snapshot_path": str(paths.run_snapshot),
        "incremental_since_ms": incremental_since_ms,
        "run_snapshot_preview_applied": run_snapshot_preview_applied,
        "updated": False,
    }

    if not failures and not args.dry_run and snapshot_path and paths.run_snapshot.exists() and paths.preview.exists():
        entries = load_snapshot_entries(paths.run_snapshot)
        write_snapshot_entries(snapshot_path, entries, source="daily-alias-apply")
        summary["snapshot"]["updated"] = True

    write_json(out_dir / "summary.json", summary)
    write_markdown_summary(out_dir / "summary.md", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if failures:
        return 1
    if summary["cleanup"]["failed_count"]:
        return 1
    if summary["noise_audit"]["error_count"]:
        return 1
    if summary["alias_update"]["failed_count"] or summary["alias_link"]["failed_count"]:
        return 1
    if summary["parent_rollup"]["field_error_count"] or summary["parent_rollup"]["failed"]:
        return 1
    if summary["expanded_link"]["failed_count"]:
        return 1
    if summary["audit"]["healthy"] is False and not args.dry_run:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
