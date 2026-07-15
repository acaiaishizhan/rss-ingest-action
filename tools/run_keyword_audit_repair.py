# -*- coding: utf-8 -*-
"""Run a focused KEYWORD duplicate and zero-link repair pass."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence, Set


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config  # noqa: E402,F401  # Load local env defaults before validate_env().
import rss_ingest  # noqa: E402
from feishu_client import get_tenant_access_token, list_bitable_records  # noqa: E402

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


@dataclass
class RepairPaths:
    audit_before: Path
    duplicate_audit: Path
    duplicate_links: Path
    zero_link_cleanup: Path
    parent_rollup: Path
    audit_after: Path

    @classmethod
    def from_out_dir(cls, out_dir: Path) -> "RepairPaths":
        return cls(
            audit_before=out_dir / "00-keyword-audit-before.json",
            duplicate_audit=out_dir / "01-keyword-duplicate-audit.json",
            duplicate_links=out_dir / "02-keyword-duplicate-links.json",
            zero_link_cleanup=out_dir / "03-zero-link-keyword-cleanup.json",
            parent_rollup=out_dir / "04-keyword-parent-rollup.json",
            audit_after=out_dir / "05-keyword-audit-after.json",
        )


@dataclass
class StepSpec:
    name: str
    command: List[str]
    allow_unhealthy_audit: bool = False


def _clean(value: Any) -> str:
    return str(value or "").strip()


def validate_env() -> List[str]:
    return [name for name in BASE_REQUIRED_ENV if not _clean(os.getenv(name))]


def refresh_runtime_keyword_snapshot(page_size: int, max_pages: int) -> int:
    tenant_token = get_tenant_access_token(
        config.FEISHU_APP_ID,
        config.FEISHU_APP_SECRET,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
    )
    records = list_bitable_records(
        config.FEISHU_APP_TOKEN,
        config.FEISHU_KEYWORD_TABLE_ID,
        tenant_token,
        config.HTTP_TIMEOUT,
        config.HTTP_RETRIES,
        page_size=page_size,
        max_pages=max_pages,
    )
    payload = rss_ingest.keyword_snapshot_payload_from_records(records, source="keyword-audit-repair")
    rss_ingest._write_json_payload_atomic(
        rss_ingest._resolve_path(getattr(config, "KEYWORD_RUNTIME_SNAPSHOT_PATH", "")),
        payload,
    )
    return int(payload.get("entry_count") or 0)


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_step(name: str, command: List[str], log_path: Path) -> int:
    print(f"\n[keyword-repair] {name}")
    print("[keyword-repair] " + " ".join(command))
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
    print(f"[keyword-repair] {name} exit={proc.returncode} log={log_path}")
    return proc.returncode


def step_output_path(step: StepSpec) -> Path | None:
    if "--output" not in step.command:
        return None
    index = step.command.index("--output")
    if index + 1 >= len(step.command):
        return None
    return Path(step.command[index + 1])


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repair duplicate KEYWORD links and stale zero-link keywords.")
    parser.add_argument("--out-dir", default="out/keyword-audit-repair")
    parser.add_argument("--apply", action="store_true", help="Write Feishu changes. Without this flag, dry-run only.")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--record-max-pages", type=int, default=200)
    parser.add_argument("--update-limit", type=int, default=0)
    parser.add_argument("--link-update-sleep", type=float, default=0.05)
    parser.add_argument("--zero-link-min-age-hours", type=float, default=48)
    parser.add_argument("--zero-link-delete-limit", type=int, default=0)
    return parser.parse_args(argv)


def build_steps(args: argparse.Namespace, paths: RepairPaths) -> List[StepSpec]:
    duplicate_link_cmd = [
        sys.executable,
        "tools/apply_keyword_duplicate_links.py",
        "--audit-path",
        str(paths.duplicate_audit),
        "--output",
        str(paths.duplicate_links),
        "--page-size",
        str(args.page_size),
        "--max-pages",
        str(args.max_pages),
        "--record-max-pages",
        str(args.record_max_pages),
        "--update-limit",
        str(args.update_limit),
        "--link-update-sleep-seconds",
        str(args.link_update_sleep),
    ]
    zero_link_cmd = [
        sys.executable,
        "tools/cleanup_stale_keywords.py",
        "--output",
        str(paths.zero_link_cleanup),
        "--page-size",
        str(args.page_size),
        "--max-pages",
        str(args.max_pages),
        "--min-age-hours",
        str(args.zero_link_min_age_hours),
        "--delete-limit",
        str(args.zero_link_delete_limit),
    ]
    parent_cmd = [
        sys.executable,
        "tools/keyword_parent_rollup.py",
        "--output",
        str(paths.parent_rollup),
        "--max-pages",
        str(args.max_pages),
    ]
    if args.apply:
        duplicate_link_cmd.append("--apply")
        zero_link_cmd.append("--apply")
        parent_cmd.append("--apply")
    else:
        parent_cmd.append("--dry-run")

    return [
        StepSpec(
            name="keyword-audit-before",
            command=[sys.executable, "tools/audit_keywords.py", "--output", str(paths.audit_before), "--max-pages", str(args.max_pages)],
            allow_unhealthy_audit=True,
        ),
        StepSpec(
            name="keyword-duplicate-audit",
            command=[
                sys.executable,
                "tools/audit_keyword_duplicates.py",
                "--output",
                str(paths.duplicate_audit),
                "--page-size",
                str(args.page_size),
                "--max-pages",
                str(args.max_pages),
                "--record-max-pages",
                str(args.record_max_pages),
            ],
        ),
        StepSpec(name="keyword-duplicate-links", command=duplicate_link_cmd),
        StepSpec(name="zero-link-keyword-cleanup", command=zero_link_cmd),
        StepSpec(name="keyword-parent-rollup", command=parent_cmd),
        StepSpec(
            name="keyword-audit-after",
            command=[sys.executable, "tools/audit_keywords.py", "--output", str(paths.audit_after), "--max-pages", str(args.max_pages)],
            allow_unhealthy_audit=True,
        ),
    ]


def _add_record_id(record_ids: Set[str], value: Any) -> None:
    record_id = _clean(value)
    if record_id:
        record_ids.add(record_id)


def collect_cleanup_exclusions(paths: RepairPaths) -> Set[str]:
    """Protect duplicate/merged targets from same-run zero-link cleanup."""
    exclusions: Set[str] = set()
    before = load_json(paths.audit_before)
    duplicate_audit = load_json(paths.duplicate_audit)

    for candidate in duplicate_audit.get("candidates") or []:
        if isinstance(candidate, dict):
            _add_record_id(exclusions, candidate.get("target_record_id"))

    for detail in before.get("merged_linked_details") or []:
        if not isinstance(detail, dict):
            continue
        for target in detail.get("merged_keyword_targets") or []:
            if not isinstance(target, dict):
                continue
            for record_id in target.get("target_record_ids") or []:
                _add_record_id(exclusions, record_id)

    return exclusions


def summarize(paths: RepairPaths, args: argparse.Namespace, failures: List[Dict[str, Any]]) -> Dict[str, Any]:
    before = load_json(paths.audit_before)
    duplicate_audit = load_json(paths.duplicate_audit)
    duplicate_links = load_json(paths.duplicate_links)
    cleanup = load_json(paths.zero_link_cleanup)
    parent = load_json(paths.parent_rollup)
    after = load_json(paths.audit_after)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "apply": bool(args.apply),
        "failures": failures,
        "audit_before": {
            "healthy": before.get("healthy"),
            "compact_duplicate_groups": before.get("compact_duplicate_groups", 0),
            "zero_link_keyword_count": before.get("zero_link_keyword_count", 0),
            "merged_linked_count": before.get("merged_linked_count", 0),
        },
        "duplicate_audit": {
            "candidate_count": duplicate_audit.get("candidate_count", 0),
            "needs_relink_count": duplicate_audit.get("needs_relink_count", 0),
            "duplicate_group_count": duplicate_audit.get("duplicate_group_count", 0),
        },
        "duplicate_links": {
            "plan_count": duplicate_links.get("plan_count", 0),
            "news_update_count": duplicate_links.get("news_update_count", 0),
            "filtered_update_count": duplicate_links.get("filtered_update_count", 0),
            "keyword_note_update_count": duplicate_links.get("keyword_note_update_count", 0),
            "updated": duplicate_links.get("updated", {}),
            "failed": duplicate_links.get("failed", {}),
        },
        "zero_link_cleanup": {
            "delete_count": cleanup.get("delete_count", 0),
            "deleted": cleanup.get("deleted", 0),
            "missing_first_seen_count": cleanup.get("missing_first_seen_count", 0),
            "failed_count": len(cleanup.get("failed") or []),
        },
        "parent_rollup": {
            "parent_link_count": parent.get("parent_link_count", 0),
            "field_error_count": len((parent.get("field_result") or {}).get("errors") or []),
            "failed": (parent.get("apply_result") or {}).get("failed", 0),
        },
        "audit_after": {
            "healthy": after.get("healthy"),
            "compact_duplicate_groups": after.get("compact_duplicate_groups", 0),
            "zero_link_keyword_count": after.get("zero_link_keyword_count", 0),
            "merged_linked_count": after.get("merged_linked_count", 0),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = ROOT_DIR / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    missing = validate_env()
    if missing:
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "error": "missing required environment variables",
            "missing": missing,
        }
        write_json(out_dir / "summary.json", payload)
        print(f"[keyword-repair] missing required env: {', '.join(missing)}")
        return 2

    paths = RepairPaths.from_out_dir(out_dir)
    failures: List[Dict[str, Any]] = []
    for step in build_steps(args, paths):
        if step.name == "zero-link-keyword-cleanup":
            exclusions = collect_cleanup_exclusions(paths)
            if exclusions:
                step.command.extend(["--exclude-record-ids", ",".join(sorted(exclusions))])
        output_path = step_output_path(step)
        if output_path and output_path.exists():
            try:
                output_path.unlink()
            except OSError as exc:
                failures.append(
                    {"step": step.name, "exit_code": 1, "error": f"cannot clear stale output: {exc}"}
                )
                break
        code = run_step(step.name, step.command, out_dir / f"{step.name}.log")
        if code == 0:
            continue
        if step.allow_unhealthy_audit and output_path and output_path.exists():
            continue
        failures.append({"step": step.name, "exit_code": code})
        break

    summary = summarize(paths, args, failures)
    runtime_snapshot = {"refreshed": False, "entry_count": 0, "error": ""}
    if args.apply and not failures and summary["audit_after"]["healthy"] is not False:
        try:
            runtime_snapshot["entry_count"] = refresh_runtime_keyword_snapshot(args.page_size, args.max_pages)
            runtime_snapshot["refreshed"] = True
        except Exception as exc:
            runtime_snapshot["error"] = str(exc)
            failures.append({"step": "keyword-runtime-snapshot-refresh", "exit_code": 1})
            summary["failures"] = failures
    summary["runtime_snapshot"] = runtime_snapshot
    write_json(out_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if failures:
        return 1
    failed = summary["duplicate_links"].get("failed") or {}
    if any(failed.get(key) for key in failed):
        return 1
    if summary["zero_link_cleanup"]["failed_count"]:
        return 1
    if summary["parent_rollup"]["field_error_count"] or summary["parent_rollup"]["failed"]:
        return 1
    if args.apply and summary["audit_after"]["healthy"] is False:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
