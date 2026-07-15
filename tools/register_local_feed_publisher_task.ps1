[CmdletBinding()]
param(
    [string]$TaskName = "rss-local-feed-publisher",
    [string]$Distro = "Ubuntu-22.04",
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Publisher = Join-Path $ProjectRoot "tools\local_feed_publisher.py"
$WorkspaceRoot = Split-Path $ProjectRoot -Parent
$HiddenRunner = Join-Path $WorkspaceRoot "solo-company\tools\run-hidden.vbs"
$WslExe = Join-Path $env:SystemRoot "System32\wsl.exe"

foreach ($required in @($Publisher, $HiddenRunner, $WslExe)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required file not found: $required"
    }
}

$PublisherWsl = (& $WslExe -d $Distro -- wslpath -a $Publisher).Trim()
if ($LASTEXITCODE -ne 0 -or -not $PublisherWsl) {
    throw "Could not resolve publisher path inside WSL distro $Distro"
}

function Quote-TaskArgument {
    param([string]$Value)
    return '"{0}"' -f ($Value -replace '"', '""')
}

$ActionArguments = @(
    $HiddenRunner,
    $WslExe,
    "-d",
    $Distro,
    "--",
    "/usr/bin/python3",
    $PublisherWsl
) | ForEach-Object { Quote-TaskArgument $_ }

$Action = New-ScheduledTaskAction `
    -Execute "wscript.exe" `
    -Argument ($ActionArguments -join " ") `
    -WorkingDirectory $ProjectRoot

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 10 `
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
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Watch local private RSS snapshots in WSL, publish changed XML to GitHub, and dispatch rss-ingest." `
    -Force | Out-Null

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $TaskName
}

Get-ScheduledTask -TaskName $TaskName
