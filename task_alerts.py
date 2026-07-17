# -*- coding: utf-8 -*-
"""Feishu webhook alerts for scheduled task failures.

Called by the scheduled-task runner scripts (and rss_ingest itself) when a
run exits non-zero. Alerting must never break the task it reports on: every
public entry point swallows its own errors and main() always returns 0.
"""
import argparse
import hashlib
import html
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import config
from feishu_client import http_post

DEFAULT_COOLDOWN_SECONDS = 7200.0
DEFAULT_STATE_DIR = Path(__file__).resolve().parent / ".cache" / "task-alerts"
LOG_TAIL_MAX_LINES = 12
LOG_TAIL_MAX_CHARS = 1500
LOG_CONTEXT_MAX_BYTES = 2_000_000
TASK_DISPLAY_NAMES = {
    "rss-ingest-fetch": "资讯抓取",
    "grok-watch-hourly": "Grok 热点抓取",
    "keyword-alias-daily": "每日关键词整理",
    "keyword-audit-repair-daily": "关键词巡检修复",
}


@dataclass(frozen=True)
class FailureDiagnosis:
    status: str
    cause: str
    impact: str
    action: str
    detail: str = ""


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


def read_log_context(path, max_bytes: int = LOG_CONTEXT_MAX_BYTES) -> str:
    """Read enough of the current run to explain the failure, not just its footer."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return ""
    if len(raw) > max_bytes:
        raw = raw[-max_bytes:]
    text = _decode_log_bytes(raw)
    # rss-ingest uses one file per day locally. Ignore failures from earlier runs.
    marker = "[runner] rss-ingest started"
    marker_at = text.rfind(marker)
    if marker_at >= 0:
        text = text[marker_at:]
    return text


def _latest_summary_counts(log_text: str) -> dict:
    summary_line = ""
    for line in log_text.splitlines():
        if "[Summary]" in line:
            summary_line = line
    return {
        key: int(value)
        for key, value in re.findall(r"\b([a-z][a-z0-9_]*)=(-?\d+)\b", summary_line)
    }


def _latest_json_count(log_text: str, field: str) -> Optional[int]:
    matches = re.findall(rf'"{re.escape(field)}"\s*:\s*(\d+)', log_text)
    return int(matches[-1]) if matches else None


def _topic_name(log_text: str) -> str:
    matches = re.findall(r"topic=([^\s]+)", log_text)
    return matches[-1] if matches else "当前话题"


def _clean_error_excerpt(log_text: str, limit: int = 240) -> str:
    lines = [line.strip() for line in log_text.splitlines() if line.strip()]
    candidates = [
        line
        for line in lines
        if any(token in line.lower() for token in ("fatal", "error", "failed", "traceback"))
        and "finished exit=" not in line.lower()
    ]
    text = candidates[-1] if candidates else (lines[-1] if lines else "")
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(
        r"(?i)\b(api[_-]?key|app[_-]?secret|access[_-]?token|authorization)\s*[:=]\s*\S+",
        r"\1=[已隐藏]",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _retry_impact(task: str) -> str:
    if task == "rss-ingest-fetch":
        return "本轮没有完整收尾；已经写入的记录保留，未完成项会在下一班重试。"
    if task == "grok-watch-hourly":
        return "本轮对应话题没有更新，其他已成功话题不受影响。"
    if task in {"keyword-alias-daily", "keyword-audit-repair-daily"}:
        return "本轮维护没有完整完成；已成功步骤不会回滚，失败步骤需要下次再跑。"
    return "本轮没有完整完成；已成功部分保留，未完成部分需要下次重试。"


def _queued_impact(queued: int) -> str:
    if queued:
        return f"本轮 {queued} 条候选资讯暂未处理，程序已保留，后续班次会重试。"
    return "本轮候选资讯暂未完成 AI 处理，程序会在后续班次重试。"


def diagnose_failure(task: str, log_text: str, exit_code: int = 1) -> FailureDiagnosis:
    """Classify scheduled-task failures and explain them without dumping raw logs."""
    counts = _latest_summary_counts(log_text)
    queued = counts.get("queue_total", 0)
    failed = counts.get("llm_failed", 0)
    lower = log_text.lower()
    detail = _clean_error_excerpt(log_text)

    if exit_code == 3 or "another rss-ingest run is already active" in lower or "single-instance lock" in lower:
        return FailureDiagnosis(
            "已跳过",
            "上一班任务还没结束，这一班为避免重复写入主动退出。",
            "没有新增故障，也不会重复入库；资讯会由上一班或下一班继续处理。",
            "不用处理；只有连续多班都提示占用时，才检查是否有僵死进程。",
            detail,
        )

    if "AccountQuotaExceeded" in log_text or "5-hour usage quota" in log_text:
        reset_match = re.search(
            r"reset at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\+0800 CST",
            log_text,
        )
        reset_text = (
            f"额度预计 {reset_match.group(1)} 自动重置，之后的班次会继续处理。"
            if reset_match
            else "等额度自动恢复后，后续班次会继续处理。"
        )
        return FailureDiagnosis(
            "自动恢复中",
            "Ark 的 5 小时调用额度用完了，不是 RSS 源集体故障。",
            _queued_impact(queued),
            reset_text,
            detail,
        )

    feishu_error = "[feishu]" in lower or "open.feishu.cn" in lower or "bitable" in lower
    if feishu_error:
        if re.search(r"http (502|503|504)\b", lower) or "bad gateway" in lower:
            status_match = re.search(r"http (502|503|504)\b", lower)
            status = status_match.group(1) if status_match else "5xx"
            return FailureDiagnosis(
                "自动重试",
                f"飞书前面的网关连续返回 HTTP {status}，多次重试后仍没恢复。",
                _retry_impact(task),
                "先不用处理；下一班成功就算恢复，连续两班仍失败再检查飞书服务和网络。",
                detail,
            )
        if "ssleoferror" in lower or "connectionerror" in lower or "connection reset" in lower:
            return FailureDiagnosis(
                "自动重试",
                "连接飞书时网络被中途断开，多次重试后仍失败。",
                _retry_impact(task),
                "先不用处理；连续两班仍失败再检查本机网络和飞书服务。",
                detail,
            )
        if "readtimeout" in lower or "connecttimeout" in lower or "timed out" in lower:
            return FailureDiagnosis(
                "自动重试",
                "飞书接口响应超时，多次重试后仍没有返回。",
                _retry_impact(task),
                "先等下一班；连续两班超时再检查网络和飞书接口状态。",
                detail,
            )
        if re.search(r"http 429\b", lower) or "125429" in lower or "rate limit" in lower:
            return FailureDiagnosis(
                "自动重试",
                "飞书接口触发了限流，当前请求暂时被拒绝。",
                _retry_impact(task),
                "不用改配置；等限流窗口过去后由下一班继续。",
                detail,
            )
        if "toolargecell" in lower or "too large cell" in lower:
            return FailureDiagnosis(
                "需要处理",
                "有一条记录超过了飞书单元格大小限制。",
                "该记录没有写入，其他记录是否成功以本轮摘要为准。",
                "检查全文截断保护是否生效，并定位超长记录。",
                detail,
            )
        if any(token in lower for token in ("permission denied", "forbidden", "no permission", "http 403")):
            return FailureDiagnosis(
                "需要处理",
                "飞书拒绝了请求，应用权限或数据表权限不足。",
                _retry_impact(task),
                "检查飞书应用权限、可见范围，以及目标多维表格的协作者权限。",
                detail,
            )
        if any(token in lower for token in ("invalid token", "token expired", "http 401", "99991663")):
            return FailureDiagnosis(
                "需要处理",
                "飞书登录凭证无效或已过期，自动刷新也没有成功。",
                _retry_impact(task),
                "检查 FEISHU_APP_ID / FEISHU_APP_SECRET，并确认应用仍处于启用状态。",
                detail,
            )
        if any(token in lower for token in ("fieldnamenotfound", "field not found", "invalid field", "1254045")):
            return FailureDiagnosis(
                "需要处理",
                "代码要写的飞书字段不存在，或字段名/类型和配置不一致。",
                _retry_impact(task),
                "对照 config.py 检查目标表字段名和字段类型，不要直接重跑覆盖。",
                detail,
            )
        if "non-json response" in lower:
            return FailureDiagnosis(
                "自动重试",
                "飞书返回了异常网页而不是接口数据，通常是临时网关故障。",
                _retry_impact(task),
                "先等下一班；连续两班仍出现再检查飞书服务和网络。",
                detail,
            )
        return FailureDiagnosis(
            "需要排查",
            "任务在访问飞书时失败，但当前规则没识别出更具体的飞书错误。",
            _retry_impact(task),
            "查看下方一行线索；如果下一班恢复可忽略，否则按线索检查飞书配置。",
            detail,
        )

    llm_error = any(token in lower for token in ("[llm]", "ark", "deepseek", "model_not_found"))
    if llm_error:
        if any(token in lower for token in ("http 401", "http 403", "invalid api key", "unauthorized")):
            return FailureDiagnosis(
                "需要处理",
                "AI 服务拒绝了凭证，API Key 无效、过期或没有模型权限。",
                _queued_impact(queued),
                "检查对应 provider 的 Key、Base URL 和模型权限；不要把 Key 发到群里。",
                detail,
            )
        if any(token in lower for token in ("model_not_found", "model not found", "invalid model")):
            return FailureDiagnosis(
                "需要处理",
                "配置的 AI 模型不存在或当前账号无权调用。",
                _queued_impact(queued),
                "检查 ARK_MODEL / provider 配置是否和当前账号可用模型一致。",
                detail,
            )
        if re.search(r"http 429\b", lower) or "rate_limit" in lower:
            return FailureDiagnosis(
                "自动重试",
                "AI 服务触发临时限流，但不是五小时总额度耗尽。",
                _queued_impact(queued),
                "先等下一班；如果连续出现，再降低并发或检查双 Key 是否都生效。",
                detail,
            )
        if re.search(r"http (500|502|503|504)\b", lower):
            return FailureDiagnosis(
                "自动重试",
                "AI 服务返回临时服务器错误，多次重试后仍失败。",
                _queued_impact(queued),
                "先等下一班；连续两班失败再检查 provider 服务状态和备用 Key。",
                detail,
            )
        if any(token in lower for token in ("timeout", "timed out", "connectionerror", "connection reset")):
            return FailureDiagnosis(
                "自动重试",
                "连接 AI 服务超时或中断，多次重试后仍失败。",
                _queued_impact(queued),
                "先等下一班；连续两班失败再检查网络和 provider 地址。",
                detail,
            )

    if queued and failed >= queued and "every queued item failed" in log_text:
        return FailureDiagnosis(
            "需要排查",
            "AI 处理整批失败，但日志里没识别出额度、认证、限流或服务错误。",
            _queued_impact(queued),
            "查看完整日志里第一条“[LLM] analysis failed”，那一条最接近根因。",
            detail,
        )

    if "GROK_TEXTBOX_NOT_FOUND" in log_text:
        return FailureDiagnosis(
            "需要处理",
            f"Grok 网页上找不到输入框，{_topic_name(log_text)} 话题重试后仍失败。",
            "本轮该话题没有更新，其他话题不受影响。",
            "检查 Grok 登录是否失效，或网页结构是否变化。",
            detail,
        )

    if "ECONNREFUSED 127.0.0.1" in log_text:
        endpoint_match = re.search(r"ECONNREFUSED (127\.0\.0\.1:\d+)", log_text)
        endpoint = endpoint_match.group(1) if endpoint_match else "本机浏览器端口"
        return FailureDiagnosis(
            "需要处理",
            f"Grok 浏览器服务没连上（{endpoint}）。",
            f"{_topic_name(log_text)} 话题重试后仍未抓到数据。",
            "检查 gpt-browser Chrome endpoint 和 Grok 登录页是否仍在运行。",
            detail,
        )

    if task == "grok-watch-hourly" and any(token in lower for token in ("rate limit", "http 429", "usage limit")):
        return FailureDiagnosis(
            "自动重试",
            "Grok 暂时限流，本轮查询被拒绝。",
            _retry_impact(task),
            "等下一排期自动重试；连续出现再降低话题频率。",
            detail,
        )

    if task == "grok-watch-hourly" and any(token in lower for token in ("login", "unauthorized", "session expired")):
        return FailureDiagnosis(
            "需要处理",
            "Grok 网页登录态失效，任务无法继续查询。",
            _retry_impact(task),
            "重新登录 grok.com，并验证 Expert 模式能正常发送一次。",
            detail,
        )

    if task in {"keyword-alias-daily", "keyword-audit-repair-daily"}:
        duplicates = _latest_json_count(log_text, "compact_duplicate_groups")
        zero_links = _latest_json_count(log_text, "zero_link_keyword_count")
        if duplicates is not None or zero_links is not None:
            parts = []
            if duplicates is not None:
                parts.append(f"{duplicates} 组重复关键词")
            if zero_links is not None:
                parts.append(f"{zero_links} 个零关联关键词")
            return FailureDiagnosis(
                "待清理",
                "任务主体已跑完，但最后的数据巡检没有通过。",
                f"当前还剩：{'；'.join(parts)}。",
                "这是数据清理项，不是程序崩溃；按巡检明细处理即可。",
                detail,
            )

    if task == "rss-ingest-fetch" and counts.get("sources_failed", 0):
        source_failed = counts["sources_failed"]
        source_done = counts.get("sources_done", 0)
        return FailureDiagnosis(
            "需要排查",
            f"有 {source_failed} 个 RSS 源失败，并超过了允许的降级范围。",
            f"其余 {source_done} 个源已完成；失败源本轮没有更新。",
            "查看完整日志里的“[RSS] fetch failed”，优先处理重复失败的源。",
            detail,
        )

    if any(token in lower for token in ("modulenotfounderror", "no module named", "python venv not found")):
        return FailureDiagnosis(
            "需要处理",
            "运行环境缺少 Python 依赖或虚拟环境。",
            "任务在启动阶段就退出，本轮没有处理数据。",
            "恢复 .venv 并重新安装 requirements.txt。",
            detail,
        )

    if any(token in lower for token in ("permissionerror", "access is denied", "permission denied")):
        return FailureDiagnosis(
            "需要处理",
            "任务没有权限访问所需文件、目录或外部资源。",
            "本轮在出错位置停止，后续步骤没有执行。",
            "按线索检查任务运行账号和目标路径权限。",
            detail,
        )

    if "no space left on device" in lower or "disk full" in lower:
        return FailureDiagnosis(
            "需要处理",
            "运行环境磁盘空间不足。",
            "日志、缓存或输出文件无法继续写入，本轮已停止。",
            "清理安全可删的缓存/产物后再运行；不要删除真实 env。",
            detail,
        )

    if any(token in lower for token in ("not configured", "missing required", "environment variable")):
        return FailureDiagnosis(
            "需要处理",
            "任务缺少必需配置或环境变量。",
            "本轮在启动或连接外部服务前停止。",
            "按线索补齐配置；密钥只放本地 env 或 GitHub Secrets。",
            detail,
        )

    if any(token in lower for token in ("connectionerror", "connection reset", "timed out", "timeout")):
        return FailureDiagnosis(
            "自动重试",
            "任务访问外部服务时网络连接失败或超时。",
            _retry_impact(task),
            "先等下一班；连续两班失败再按线索检查对应服务和网络。",
            detail,
        )

    return FailureDiagnosis(
        "需要排查",
        "任务异常退出，但当前规则还不能可靠判断根因。",
        "本轮没有完整完成；已成功部分不会因为告警自动回滚。",
        "查看下方一行线索；如果下一班恢复可忽略，否则需要打开完整日志。",
        detail,
    )


def summarize_failure(task: str, log_text: str, exit_code: int = 1) -> list[str]:
    diagnosis = diagnose_failure(task, log_text, exit_code)
    lines = [
        f"发生了什么：{diagnosis.cause}",
        f"影响：{diagnosis.impact}",
        f"你要做什么：{diagnosis.action}",
    ]
    if diagnosis.detail:
        lines.append(f"线索：{diagnosis.detail}")
    return lines


def _diagnosis_fingerprint(diagnosis: FailureDiagnosis) -> str:
    raw = f"{diagnosis.status}\n{diagnosis.cause}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def _state_path(task: str, state_dir: Path, fingerprint: str = "") -> Path:
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in task)
    suffix = f".{fingerprint}" if fingerprint else ""
    return Path(state_dir) / f"{safe}{suffix}.last-alert"


def should_alert(
    task: str,
    state_dir: Path,
    now: Optional[float] = None,
    cooldown: float = DEFAULT_COOLDOWN_SECONDS,
    fingerprint: str = "",
) -> bool:
    now = time.time() if now is None else now
    if fingerprint:
        # Respect the pre-fingerprint task-level state during rolling upgrades.
        paths = [
            _state_path(task, state_dir, fingerprint),
            _state_path(task, state_dir),
        ]
    else:
        base_path = _state_path(task, state_dir)
        paths = [base_path]
        try:
            paths.extend(base_path.parent.glob(f"{base_path.stem}.*.last-alert"))
        except OSError:
            pass
    timestamps = []
    for path in paths:
        try:
            timestamps.append(float(path.read_text(encoding="utf-8").strip()))
        except (OSError, ValueError):
            continue
    if not timestamps:
        return True
    last = max(timestamps)
    return (now - last) > cooldown


def record_alert(
    task: str,
    state_dir: Path,
    now: Optional[float] = None,
    fingerprint: str = "",
) -> None:
    now = time.time() if now is None else now
    path = _state_path(task, state_dir, fingerprint)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{now}", encoding="utf-8")
    except OSError as exc:
        print(f"[task-alerts] failed to record alert state: {exc}")


def build_message(
    task: str,
    exit_code: int,
    log_tail: str = "",
    when: Optional[str] = None,
    log_text: Optional[str] = None,
) -> str:
    when = when or time.strftime("%Y-%m-%d %H:%M:%S")
    diagnostic_text = log_text if log_text is not None else log_tail
    diagnosis = diagnose_failure(task, diagnostic_text, exit_code)
    explanation = summarize_failure(task, diagnostic_text, exit_code)
    display_name = TASK_DISPLAY_NAMES.get(task, task)
    lines = [
        f"[{diagnosis.status}] {display_name}",
        *explanation,
        f"时间：{when}（退出码 {exit_code}）",
    ]
    if TASK_DISPLAY_NAMES.get(task):
        lines.append(f"任务：{task}")
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
    log_text = read_log_context(log_path) if log_path else ""
    log_tail = read_log_tail(log_path) if log_path and not log_text else ""
    diagnosis = diagnose_failure(task, log_text or log_tail, exit_code)
    fingerprint = _diagnosis_fingerprint(diagnosis)
    if not should_alert(
        task,
        state_dir,
        now=now,
        cooldown=cooldown,
        fingerprint=fingerprint,
    ):
        print(f"[task-alerts] within cooldown for {task}; skip alert")
        return False
    if not send_webhook(
        webhook_url,
        build_message(task, exit_code, log_tail, log_text=log_text or None),
    ):
        return False
    record_alert(task, state_dir, now=now, fingerprint=fingerprint)
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
