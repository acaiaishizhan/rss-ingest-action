param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Python venv not found: $Python"
}

$CacheDir = Join-Path $ProjectRoot ".cache"
$LogDir = Join-Path $ProjectRoot "out\keyword-audit-repair\logs"
New-Item -ItemType Directory -Force -Path $CacheDir, $LogDir | Out-Null

$LogFile = Join-Path $LogDir ("keyword-audit-repair-local-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$LockPath = Join-Path $CacheDir "keyword-audit-repair.lock"
$TaskLock = $null
. (Join-Path $PSScriptRoot "task_lock.ps1")

try {
    $TaskLock = Enter-TaskFileLock -Path $LockPath
    if (-not $TaskLock.Acquired) {
        "Another keyword audit repair run is already active: $LockPath" | Tee-Object -FilePath $LogFile
        exit 0
    }
    if ($TaskLock.RecoveredStale) {
        "Recovered stale task lock: $LockPath" | Tee-Object -FilePath $LogFile
    }

    "keyword-audit-repair local run started: $(Get-Date -Format o)" | Tee-Object -FilePath $LogFile
    "project_root=$ProjectRoot" | Tee-Object -Append -FilePath $LogFile
    "python=$Python" | Tee-Object -Append -FilePath $LogFile
    "dry_run=$DryRun" | Tee-Object -Append -FilePath $LogFile

    $ArgsList = @(
        "tools\run_keyword_audit_repair.py",
        "--out-dir", "out\keyword-audit-repair",
        "--page-size", "500",
        "--max-pages", "200",
        "--record-max-pages", "200",
        "--link-update-sleep", "0.05",
        "--zero-link-min-age-hours", "48"
    )
    if (-not $DryRun) {
        $ArgsList += "--apply"
    }

    & $Python @ArgsList 2>&1 | Tee-Object -Append -FilePath $LogFile
    $ExitCode = $LASTEXITCODE
    "keyword-audit-repair local run finished: exit=$ExitCode ended=$(Get-Date -Format o)" | Tee-Object -Append -FilePath $LogFile
    if ($ExitCode -ne 0 -and -not $DryRun) {
        & $Python (Join-Path $ProjectRoot "task_alerts.py") --task "keyword-audit-repair-daily" --exit-code $ExitCode --log $LogFile 2>&1 | Tee-Object -Append -FilePath $LogFile
    }
    exit $ExitCode
}
finally {
    Exit-TaskFileLock -Lock $TaskLock
}
