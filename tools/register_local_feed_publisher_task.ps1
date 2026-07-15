[CmdletBinding()]
param(
    [string]$TaskName = "rss-local-feed-publisher",
    [string]$Distro = "Ubuntu-22.04",
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Publisher = Join-Path $ProjectRoot "tools\local_feed_publisher.py"
$WslExe = Join-Path $env:SystemRoot "System32\wsl.exe"

foreach ($required in @($Publisher, $WslExe)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required file not found: $required"
    }
}

$PublisherWsl = $null
if ($Publisher -match '^([A-Za-z]):\\(.*)$') {
    $Drive = $Matches[1].ToLowerInvariant()
    $RelativePath = $Matches[2] -replace '\\', '/'
    $PublisherWsl = "/mnt/$Drive/$RelativePath"
}
if (-not $PublisherWsl) {
    throw "Publisher path is not a Windows drive path: $Publisher"
}

function Quote-TaskArgument {
    param([string]$Value)
    return '"{0}"' -f ($Value -replace '"', '""')
}

$EscapedWslExe = $WslExe -replace "'", "''"
$EscapedDistro = $Distro -replace "'", "''"
$EscapedPublisher = $PublisherWsl -replace "'", "''"
$WslCommand = "& '$EscapedWslExe' -d '$EscapedDistro' -- /usr/bin/python3 '$EscapedPublisher'; exit `$LASTEXITCODE"
$ActionArguments = @(
    "-NoProfile",
    "-NonInteractive",
    "-WindowStyle",
    "Hidden",
    "-Command",
    (Quote-TaskArgument $WslCommand)
)

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
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
