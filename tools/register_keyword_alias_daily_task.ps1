param(
    [string]$TaskName = "keyword-alias-daily",
    [string]$At = "04:00"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Runner = Join-Path $ProjectRoot "tools\run_keyword_alias_daily_local.ps1"
if (-not (Test-Path $Runner)) {
    throw "Runner not found: $Runner"
}

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ("-NoProfile -ExecutionPolicy Bypass -File `"{0}`"" -f $Runner) `
    -WorkingDirectory $ProjectRoot

$Trigger = New-ScheduledTaskTrigger -Daily -At $At
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Run rss-ingest KEYWORD alias maintenance locally with Volcengine Ark deepseek-v4-pro." `
    -Force | Out-Null

Get-ScheduledTask -TaskName $TaskName
