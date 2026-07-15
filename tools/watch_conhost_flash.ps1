param(
    [int]$DurationHours = 24,
    [int]$PollSeconds = 2,
    [string]$LogPath = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
if (-not $LogPath) {
    $LogPath = Join-Path $ProjectRoot "data\conhost-flash-watch.jsonl"
}

$LogDir = Split-Path -Parent $LogPath
if ($LogDir -and -not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

function Get-ProcessInfoById {
    param([int]$ProcessId)
    if ($ProcessId -le 0) {
        return $null
    }
    try {
        $p = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $ProcessId) -ErrorAction Stop
        if (-not $p) {
            return $null
        }
        return [ordered]@{
            pid = [int]$p.ProcessId
            ppid = [int]$p.ParentProcessId
            name = [string]$p.Name
            executable = [string]$p.ExecutablePath
            commandLine = [string]$p.CommandLine
            creationDate = if ($p.CreationDate) { [Management.ManagementDateTimeConverter]::ToDateTime($p.CreationDate).ToString("o") } else { "" }
        }
    } catch {
        return $null
    }
}

function Get-ProcessChain {
    param(
        [int]$StartPid,
        [int]$Depth = 5
    )
    $items = @()
    $currentPid = $StartPid
    for ($i = 0; $i -lt $Depth; $i++) {
        $info = Get-ProcessInfoById -ProcessId $currentPid
        if (-not $info) {
            break
        }
        $items += $info
        $next = [int]$info.ppid
        if ($next -le 0 -or $next -eq $currentPid) {
            break
        }
        $currentPid = $next
    }
    return $items
}

function Get-RecentSecurityProcessEvent {
    param([int]$ProcessId)
    $wantedHex = ("0x{0:x}" -f $ProcessId).ToLowerInvariant()
    $start = (Get-Date).AddSeconds(-15)
    try {
        $events = Get-WinEvent -FilterHashtable @{ LogName = "Security"; Id = 4688; StartTime = $start } -ErrorAction Stop
    } catch {
        return $null
    }
    foreach ($event in $events) {
        try {
            $xml = [xml]$event.ToXml()
            $data = @{}
            foreach ($d in $xml.Event.EventData.Data) {
                $data[$d.Name] = $d.'#text'
            }
            $newPid = ([string]$data["NewProcessId"]).ToLowerInvariant()
            if ($newPid -ne $wantedHex) {
                continue
            }
            return [ordered]@{
                time = $event.TimeCreated.ToString("o")
                subjectUser = [string]$data["SubjectUserName"]
                newProcess = [string]$data["NewProcessName"]
                commandLine = [string]$data["CommandLine"]
                parentProcess = [string]$data["ParentProcessName"]
                parentProcessId = [string]$data["ProcessId"]
            }
        } catch {
            continue
        }
    }
    return $null
}

function Convert-HexProcessId {
    param([string]$Value)
    if (-not $Value) {
        return 0
    }
    try {
        $clean = $Value.Trim()
        if ($clean.StartsWith("0x")) {
            return [Convert]::ToInt32($clean.Substring(2), 16)
        }
        return [int]$clean
    } catch {
        return 0
    }
}

function Convert-Security4688Event {
    param($Event)
    try {
        $xml = [xml]$Event.ToXml()
        $data = @{}
        foreach ($d in $xml.Event.EventData.Data) {
            $data[$d.Name] = $d.'#text'
        }
        $newPid = Convert-HexProcessId -Value ([string]$data["NewProcessId"])
        $parentPid = Convert-HexProcessId -Value ([string]$data["ProcessId"])
        return [ordered]@{
            recordId = [int64]$Event.RecordId
            time = $Event.TimeCreated.ToString("o")
            subjectUser = [string]$data["SubjectUserName"]
            newProcessId = $newPid
            newProcess = [string]$data["NewProcessName"]
            commandLine = [string]$data["CommandLine"]
            parentProcessId = $parentPid
            parentProcess = [string]$data["ParentProcessName"]
        }
    } catch {
        return $null
    }
}

function Write-JsonLog {
    param([hashtable]$Payload)
    $json = $Payload | ConvertTo-Json -Depth 8 -Compress
    Add-Content -LiteralPath $LogPath -Value $json -Encoding UTF8
}

$watchStartedAt = Get-Date
$startPayload = @{
    type = "watch_start"
    time = $watchStartedAt.ToString("o")
    pid = $PID
    durationHours = $DurationHours
    pollSeconds = $PollSeconds
    logPath = $LogPath
}
Write-JsonLog -Payload $startPayload

$deadline = (Get-Date).AddHours($DurationHours)
$seenRecordIds = New-Object 'System.Collections.Generic.HashSet[Int64]'
$lastCheck = $watchStartedAt

try {
    while ((Get-Date) -lt $deadline) {
        $now = Get-Date
        $queryStart = $lastCheck.AddSeconds(-2)
        $lastCheck = $now
        try {
            $events = Get-WinEvent -FilterHashtable @{ LogName = "Security"; Id = 4688; StartTime = $queryStart } -ErrorAction Stop |
                Sort-Object RecordId
        } catch {
            $events = @()
        }

        foreach ($event in $events) {
            if ($event.TimeCreated -lt $watchStartedAt) {
                continue
            }
            if (-not $seenRecordIds.Add([int64]$event.RecordId)) {
                continue
            }
            $security = Convert-Security4688Event -Event $event
            if (-not $security) {
                continue
            }
            if ([string]$security.newProcess -notmatch "\\conhost\.exe$") {
                continue
            }
            $childPid = [int]$security.newProcessId
            $parentPid = [int]$security.parentProcessId
            $payload = @{
                type = "conhost_start"
                time = (Get-Date).ToString("o")
                security4688 = $security
                process = Get-ProcessInfoById -ProcessId $childPid
                parent = Get-ProcessInfoById -ProcessId $parentPid
                parentChain = Get-ProcessChain -StartPid $parentPid -Depth 6
            }
            Write-JsonLog -Payload $payload
        }

        Start-Sleep -Seconds ([Math]::Max(1, $PollSeconds))
    }
} finally {
    Write-JsonLog -Payload @{
        type = "watch_stop"
        time = (Get-Date).ToString("o")
        pid = $PID
    }
}
