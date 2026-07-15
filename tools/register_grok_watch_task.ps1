param(
    [string]$TaskName = "grok-watch-hourly",
    [ValidateRange(0, 59)]
    [int]$MinuteOfHour = 3,
    [ValidateRange(1, 1440)]
    [int]$IntervalMinutes = 20
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Runner = Join-Path $ProjectRoot "tools\run_grok_watch_local.ps1"
if (-not (Test-Path $Runner)) {
    throw "Runner not found: $Runner"
}

$Launcher = Join-Path $ProjectRoot "tools\run_grok_watch_hidden.vbs"
if (-not (Test-Path $Launcher)) {
    throw "Hidden launcher not found: $Launcher"
}
$WScript = Join-Path $env:SystemRoot "System32\wscript.exe"

$Action = New-ScheduledTaskAction `
    -Execute $WScript `
    -Argument ('"{0}"' -f $Launcher) `
    -WorkingDirectory $ProjectRoot

$Now = Get-Date
$StartAt = Get-Date -Hour $Now.Hour -Minute $MinuteOfHour -Second 0
if ($StartAt -le $Now) {
    $StartAt = $StartAt.AddHours(1)
}

$Trigger = New-ScheduledTaskTrigger -Once -At $StartAt `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "grok_watch: staggered Grok social/news search feeding local RSS files for rss-ingest." `
    -Force | Out-Null

Get-ScheduledTask -TaskName $TaskName
