# -*- coding: utf-8 -*-
"""Feishu webhook alerts for scheduled task failures.

Called by the scheduled-task runner scripts (and rss_ingest itself) when a
run exits non-zero. Alerting must never break the task it reports on: every
public entry point swallows its own errors and main() always returns 0.
"""
import argparse
import sys
import time
from pathlib import Path
from typing import Optional

import config
from feishu_client import http_post

DEFAULT_COOLDOWN_SECONDS = 7200.0
DEFAULT_STATE_DIR = Path(__file__).resolve().parent / ".cache" / "task-alerts"
LOG_TAIL_MAX_LINES = 12
LOG_TAIL_MAX_CHARS = 1500


def _decode_log_bytes(raw: bytes) -> str:
    # PowerShell 5.1 Tee-Object writes UTF-16LE; our python logs are UTF-8.
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16-be", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    return raw.decode("utf-8", errors="replace")


def read_log_tail(path, max_lines: int = LOG_TAIL_MAX_LINES, max_chars: int = LOG_TAIL_MAX_CHARS) -> str:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return ""
    text = _decode_log_bytes(raw)
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    tail = "\n".join(lines[-max_lines:])
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail


def _state_path(task: str, state_dir: Path) -> Path:
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in task)
    return Path(state_dir) / f"{safe}.last-alert"


def should_alert(task: str, state_dir: Path, now: Optional[float] = None, cooldown: float = DEFAULT_COOLDOWN_SECONDS) -> bool:
    now = time.time() if now is None else now
    try:
        last = float(_state_path(task, state_dir).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return (now - last) > cooldown


def record_alert(task: str, state_dir: Path, now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    path = _state_path(task, state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{now}", encoding="utf-8")
    except OSError as exc:
        print(f"[task-alerts] failed to record alert state: {exc}")


def build_message(task: str, exit_code: int, log_tail: str = "", when: Optional[str] = None) -> str:
    when = when or time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"[定时任务失败] {task}",
        f"exit={exit_code} time={when}",
    ]
    if log_tail:
        lines.append("---- log tail ----")
        lines.append(log_tail)
    return "\n".join(lines)


def send_webhook(webhook_url: str, text: str, timeout: int = 10) -> bool:
    payload = {"msg_type": "text", "content": {"text": text}}
    headers = {"Content-Type": "application/json; charset=utf-8"}
    try:
        resp = http_post(webhook_url, headers, payload, timeout, retries=2)
    except Exception as exc:
        print(f"[task-alerts] webhook send failed: {exc}")
        return False
    if getattr(resp, "status_code", None) != 200:
        print(f"[task-alerts] webhook rejected: HTTP {getattr(resp, 'status_code', '?')} {getattr(resp, 'text', '')[:200]}")
        return False
    try:
        response_payload = resp.json()
    except Exception as exc:
        print(f"[task-alerts] webhook returned invalid JSON: {exc}")
        return False
    if not isinstance(response_payload, dict):
        print("[task-alerts] webhook returned a non-object response")
        return False
    business_code = response_payload.get("code", response_payload.get("StatusCode"))
    if str(business_code).strip() not in {"0", "0.0"}:
        print(
            "[task-alerts] webhook rejected: "
            f"code={business_code!r} msg={response_payload.get('msg') or response_payload.get('StatusMessage') or ''}"
        )
        return False
    return True


def notify_failure(
    task: str,
    exit_code: int,
    log_path: Optional[str] = None,
    webhook_url: Optional[str] = None,
    state_dir: Optional[Path] = None,
    cooldown: float = DEFAULT_COOLDOWN_SECONDS,
    now: Optional[float] = None,
) -> bool:
    webhook_url = config.FEISHU_WEBHOOK_URL if webhook_url is None else webhook_url
    state_dir = DEFAULT_STATE_DIR if state_dir is None else Path(state_dir)
    if not webhook_url:
        print("[task-alerts] FEISHU_WEBHOOK_URL not configured; skip alert")
        return False
    if not should_alert(task, state_dir, now=now, cooldown=cooldown):
        print(f"[task-alerts] within cooldown for {task}; skip alert")
        return False
    log_tail = read_log_tail(log_path) if log_path else ""
    if not send_webhook(webhook_url, build_message(task, exit_code, log_tail)):
        return False
    record_alert(task, state_dir, now=now)
    print(f"[task-alerts] alert sent for {task} exit={exit_code}")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Send a Feishu alert for a failed scheduled task")
    parser.add_argument("--task", required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--log", default=None)
    parser.add_argument("--webhook-url", default=None)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--cooldown", type=float, default=DEFAULT_COOLDOWN_SECONDS)
    args = parser.parse_args(argv)
    try:
        notify_failure(
            args.task,
            args.exit_code,
            log_path=args.log,
            webhook_url=args.webhook_url,
            state_dir=Path(args.state_dir) if args.state_dir else None,
            cooldown=args.cooldown,
        )
    except Exception as exc:
        print(f"[task-alerts] unexpected error: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
