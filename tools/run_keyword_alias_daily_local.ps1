param(
    [switch]$DryRun,
    [switch]$FullRun,
    [string]$TypeFilter = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Python venv not found: $Python"
}

$env:LLM_PROVIDER = "ark"
$env:ARK_MODEL = "deepseek-v4-pro"

$CacheDir = Join-Path $ProjectRoot ".cache"
$LogDir = Join-Path $ProjectRoot "out\keyword-alias-daily\logs"
New-Item -ItemType Directory -Force -Path $CacheDir, $LogDir | Out-Null

$LogFile = Join-Path $LogDir ("keyword-alias-daily-local-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$LockPath = Join-Path $CacheDir "keyword-alias-daily.lock"
$TaskLock = $null
. (Join-Path $PSScriptRoot "task_lock.ps1")

try {
    $TaskLock = Enter-TaskFileLock -Path $LockPath
    if (-not $TaskLock.Acquired) {
        "Another keyword alias daily run is already active: $LockPath" | Tee-Object -FilePath $LogFile
        exit 0
    }
    if ($TaskLock.RecoveredStale) {
        "Recovered stale task lock: $LockPath" | Tee-Object -FilePath $LogFile
    }

    "keyword-alias-daily local run started: $(Get-Date -Format o)" | Tee-Object -FilePath $LogFile
    "project_root=$ProjectRoot" | Tee-Object -Append -FilePath $LogFile
    "python=$Python" | Tee-Object -Append -FilePath $LogFile
    "provider=ark model=$env:ARK_MODEL base_url=$env:ARK_BASE_URL" | Tee-Object -Append -FilePath $LogFile

    $ArgsList = @(
        "tools\run_keyword_alias_daily.py",
        "--out-dir", "out\keyword-alias-daily",
        "--provider", "ark",
        "--model", $env:ARK_MODEL,
        "--keyword-snapshot-path", "data\keyword_snapshot.json",
        "--incremental-hours", "25",
        "--record-recent-hours", "25",
        "--record-max-pages", "10",
        "--max-pages", "80",
        "--page-size", "500",
        "--noise-batch-size", "500"
    )
    if ($DryRun) {
        $ArgsList += "--dry-run"
    }
    if ($FullRun) {
        $ArgsList += @("--incremental-hours", "0", "--record-recent-hours", "0", "--record-max-pages", "80")
    }
    if ($TypeFilter.Trim()) {
        $ArgsList += @("--type-filter", $TypeFilter.Trim())
    }

    & $Python @ArgsList 2>&1 | Tee-Object -Append -FilePath $LogFile
    $ExitCode = $LASTEXITCODE
    "keyword-alias-daily local run finished: exit=$ExitCode ended=$(Get-Date -Format o)" | Tee-Object -Append -FilePath $LogFile
    if ($ExitCode -ne 0) {
        & $Python (Join-Path $ProjectRoot "task_alerts.py") --task "keyword-alias-daily" --exit-code $ExitCode --log $LogFile 2>&1 | Tee-Object -Append -FilePath $LogFile
    }
    exit $ExitCode
}
finally {
    Exit-TaskFileLock -Lock $TaskLock
}
