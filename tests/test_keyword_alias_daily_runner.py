from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_keyword_alias_workflow_is_manual_only():
    # 公开 Action 仓库可以不发布关键词 workflow；若保留，它必须 manual-only，避免与本机双跑。
    workflow_path = ROOT / ".github" / "workflows" / "keyword-alias-daily.yml"
    if not workflow_path.exists():
        return
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "schedule:" not in workflow
    assert "cron:" not in workflow


def test_local_keyword_alias_runner_uses_volcengine_pro_lane():
    script = (ROOT / "tools" / "run_keyword_alias_daily_local.ps1").read_text(encoding="utf-8")

    assert '$env:LLM_PROVIDER = "ark"' in script
    assert '$env:ARK_MODEL = "deepseek-v4-pro"' in script
    assert '"--provider", "ark"' in script
    assert "localhost:11434" not in script
    assert "OLLAMA_" not in script


def test_local_keyword_alias_runner_alerts_on_failure():
    script = (ROOT / "tools" / "run_keyword_alias_daily_local.ps1").read_text(encoding="utf-8")

    assert "task_alerts.py" in script
    assert '--task "keyword-alias-daily"' in script
