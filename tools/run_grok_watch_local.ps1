$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$LogPath = Join-Path $ProjectRoot "grok_watch_runs.log"

# Keep this file ASCII-only: Task Scheduler runs powershell.exe (Windows
# PowerShell 5.1), which decodes BOM-less scripts as the system ANSI codepage
# (GBK on zh-CN). Non-ASCII comments corrupt parsing and python never runs
# (exit 0, no output) -- this silently broke the task from 2026-06-12 (956d2d8)
# until 2026-06-15. Do not add Chinese here; keep notes in English.
# Headless runs occasionally hit 0xC000013A (a console signal kills the whole
# process group). The due-check is idempotent, so one retry only reprocesses
# unfinished topics and will not double-spend grok calls.
$MaxAttempts = 2
$code = 0
for ($i = 1; $i -le $MaxAttempts; $i++) {
    & $Python (Join-Path $ProjectRoot "grok_watch.py") 2>&1 | Out-File -FilePath $LogPath -Append -Encoding utf8
    $code = $LASTEXITCODE
    if ($code -eq 0) { break }
    "[runner] python exit $code at attempt $i/$MaxAttempts $(Get-Date -Format s)" | Out-File -FilePath $LogPath -Append -Encoding utf8
}
if ($code -ne 0) {
    & $Python (Join-Path $ProjectRoot "task_alerts.py") --task "grok-watch-hourly" --exit-code $code --log $LogPath 2>&1 | Out-File -FilePath $LogPath -Append -Encoding utf8
}
exit $code
