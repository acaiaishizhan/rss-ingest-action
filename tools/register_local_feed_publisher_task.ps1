[CmdletBinding()]
param(
    [string]$TaskName = "rss-local-feed-publisher",
    [string]$Distro = "Ubuntu-22.04",
    [ValidateRange(1, 1440)]
    [int]$IntervalMinutes = 10,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Publisher = Join-Path $ProjectRoot "tools\local_feed_publisher.py"
$WslExe = Join-Path $env:SystemRoot "System32\wsl.exe"
$HiddenLauncher = Join-Path $ProjectRoot "tools\run_local_feed_publisher_hidden.vbs"
$HiddenRunner = Join-Path $ProjectRoot "tools\run_local_feed_publisher_local.ps1"
$WScriptExe = Join-Path $env:SystemRoot "System32\wscript.exe"

foreach ($required in @($Publisher, $WslExe, $HiddenLauncher, $HiddenRunner, $WScriptExe)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required file not found: $required"
    }
}

$ActionArguments = '//B //NoLogo "{0}" "{1}"' -f $HiddenLauncher, $Distro

$Action = New-ScheduledTaskAction `
    -Execute $WScriptExe `
    -Argument $ActionArguments `
    -WorkingDirectory $ProjectRoot

$LogonTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$PeriodicTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger @($LogonTrigger, $PeriodicTrigger) `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Publish local/private/Grok RSS snapshots every $IntervalMinutes minutes, then dispatch rss-ingest when data changed." `
    -Force | Out-Null

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $TaskName
}

Get-ScheduledTask -TaskName $TaskName
