[CmdletBinding()]
param(
    [string]$Distro = "Ubuntu-22.04"
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

if ($Publisher -notmatch '^([A-Za-z]):\\(.*)$') {
    throw "Publisher path is not a Windows drive path: $Publisher"
}
$Drive = $Matches[1].ToLowerInvariant()
$RelativePath = $Matches[2] -replace '\\', '/'
$PublisherWsl = "/mnt/$Drive/$RelativePath"

& $WslExe -d $Distro -- /usr/bin/python3 $PublisherWsl --once
exit $LASTEXITCODE
