$ErrorActionPreference = 'Stop'

$control2 = 'C:\Program Files\Apogee\Control 2\Apogee Control 2.exe'
$messenger = 'C:\Program Files\Apogee\Control 2\ApogeeMessenger.exe'
$usbPanel = 'C:\Program Files\Apogee\ApogeeUSBAudio_Driver\W10_x64\ApogeeUSBAudioCpl.exe'

$service = Get-Service -Name 'ApogeeGlue' -ErrorAction SilentlyContinue
if ($service) {
    if ($service.Status -ne 'Running') {
        Start-Service -Name 'ApogeeGlue'
    }
}

if (Test-Path -LiteralPath $messenger) {
    $runningMessenger = Get-Process -Name 'ApogeeMessenger' -ErrorAction SilentlyContinue
    if (-not $runningMessenger) {
        Start-Process -FilePath $messenger
    }
}

if (Test-Path -LiteralPath $usbPanel) {
    $runningPanel = Get-Process -Name 'ApogeeUSBAudioCpl' -ErrorAction SilentlyContinue
    if (-not $runningPanel) {
        Start-Process -FilePath $usbPanel -ArgumentList '-hide'
    }
}

if (Test-Path -LiteralPath $control2) {
    Start-Process -FilePath $control2
} else {
    Write-Warning "Apogee Control 2 was not found at: $control2"
}

