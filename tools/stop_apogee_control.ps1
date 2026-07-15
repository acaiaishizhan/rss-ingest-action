$ErrorActionPreference = 'Continue'

Stop-Service -Name 'ApogeeGlue' -Force -ErrorAction SilentlyContinue

Get-Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Path -like 'C:\Program Files\Apogee\*' -or
        $_.ProcessName -match '^Apogee|dfu-util'
    } |
    ForEach-Object {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }

Get-Service -Name 'ApogeeGlue' -ErrorAction SilentlyContinue |
    Select-Object Name, Status, StartType |
    Format-Table -AutoSize

