import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tools" / "task_lock.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


def _ps_quote(value: Path) -> str:
    return str(value).replace("'", "''")


def _run_lock_probe(lock_path: Path) -> subprocess.CompletedProcess[str]:
    if not POWERSHELL:
        pytest.skip("PowerShell is unavailable")
    script = (
        f". '{_ps_quote(HELPER)}'; "
        f"$lock = Enter-TaskFileLock -Path '{_ps_quote(lock_path)}'; "
        'Write-Output ("{0}|{1}" -f $lock.Acquired, $lock.RecoveredStale); '
        "Exit-TaskFileLock -Lock $lock"
    )
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_task_lock_recovers_dead_pid_file(tmp_path):
    lock_path = tmp_path / "stale.lock"
    lock_path.write_text("pid=999999 started=2000-01-01T00:00:00Z\n", encoding="utf-8")

    result = _run_lock_probe(lock_path)

    assert result.returncode == 0, result.stderr
    assert "True|True" in result.stdout
    assert not lock_path.exists()


def test_task_lock_does_not_take_lock_owned_by_live_pid(tmp_path):
    lock_path = tmp_path / "active.lock"
    lock_path.write_text(f"pid={os.getpid()} started=2000-01-01T00:00:00Z\n", encoding="utf-8")

    result = _run_lock_probe(lock_path)

    assert result.returncode == 0, result.stderr
    assert "False|False" in result.stdout
    assert lock_path.exists()
