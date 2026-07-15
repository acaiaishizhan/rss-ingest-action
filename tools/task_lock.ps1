function New-TaskLockResult {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][bool]$Acquired,
        [bool]$RecoveredStale = $false,
        $Stream = $null,
        $Writer = $null
    )

    [PSCustomObject]@{
        Path = $Path
        Acquired = $Acquired
        RecoveredStale = $RecoveredStale
        Stream = $Stream
        Writer = $Writer
    }
}

function Get-TaskLockOwnerPid {
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        $lines = [System.IO.File]::ReadAllLines($Path)
        $firstLine = if ($lines.Length -gt 0) { $lines[0] } else { "" }
    }
    catch {
        return $null
    }
    if ($firstLine -match '^pid=(\d+)\b') {
        return [int]$Matches[1]
    }
    return $null
}

function Test-TaskLockProcessAlive {
    param([Parameter(Mandatory = $true)][int]$OwnerPid)

    return $null -ne (Get-Process -Id $OwnerPid -ErrorAction SilentlyContinue)
}

function Enter-TaskFileLock {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$InvalidStaleSeconds = 60
    )

    $parent = Split-Path -Parent $Path
    if ($parent) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }

    $recoveredStale = $false
    for ($attempt = 0; $attempt -lt 2; $attempt++) {
        try {
            $stream = [System.IO.File]::Open(
                $Path,
                [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::None
            )
            $writer = New-Object System.IO.StreamWriter($stream)
            $writer.WriteLine(("pid={0} started={1:o}" -f $PID, (Get-Date)))
            $writer.Flush()
            return New-TaskLockResult -Path $Path -Acquired $true -RecoveredStale $recoveredStale -Stream $stream -Writer $writer
        }
        catch [System.IO.IOException] {
            $ownerPid = Get-TaskLockOwnerPid -Path $Path
            if ($null -ne $ownerPid -and (Test-TaskLockProcessAlive -OwnerPid $ownerPid)) {
                return New-TaskLockResult -Path $Path -Acquired $false
            }

            if ($null -eq $ownerPid) {
                try {
                    $ageSeconds = ((Get-Date) - (Get-Item -LiteralPath $Path -ErrorAction Stop).LastWriteTime).TotalSeconds
                }
                catch {
                    return New-TaskLockResult -Path $Path -Acquired $false
                }
                if ($ageSeconds -lt $InvalidStaleSeconds) {
                    return New-TaskLockResult -Path $Path -Acquired $false
                }
            }

            try {
                Remove-Item -LiteralPath $Path -Force -ErrorAction Stop
                $recoveredStale = $true
            }
            catch {
                return New-TaskLockResult -Path $Path -Acquired $false
            }
        }
    }

    return New-TaskLockResult -Path $Path -Acquired $false -RecoveredStale $recoveredStale
}

function Exit-TaskFileLock {
    param($Lock)

    if ($null -eq $Lock -or -not $Lock.Acquired) {
        return
    }
    try {
        if ($Lock.Writer) {
            $Lock.Writer.Dispose()
        }
        elseif ($Lock.Stream) {
            $Lock.Stream.Dispose()
        }
    }
    finally {
        Remove-Item -LiteralPath $Lock.Path -Force -ErrorAction SilentlyContinue
    }
}
