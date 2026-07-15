param(
    [int]$DurationHours = 24,
    [int]$PollMilliseconds = 80,
    [string]$LogPath = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
if (-not $LogPath) {
    $LogPath = Join-Path $ProjectRoot "data\visible-console-flash-watch.jsonl"
}

$LogDir = Split-Path -Parent $LogPath
if ($LogDir -and -not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class Win32WindowProbe {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc enumProc, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);

    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);

    [StructLayout(LayoutKind.Sequential)]
    public struct RECT {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }
}
"@

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
        [int]$Depth = 6
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

function Get-WindowSnapshot {
    param([IntPtr]$Handle)
    $titleBuilder = New-Object System.Text.StringBuilder 512
    [void][Win32WindowProbe]::GetWindowText($Handle, $titleBuilder, $titleBuilder.Capacity)
    $rect = New-Object Win32WindowProbe+RECT
    [void][Win32WindowProbe]::GetWindowRect($Handle, [ref]$rect)
    return [ordered]@{
        handle = ("0x{0:x}" -f $Handle.ToInt64())
        visible = [Win32WindowProbe]::IsWindowVisible($Handle)
        minimized = [Win32WindowProbe]::IsIconic($Handle)
        title = $titleBuilder.ToString()
        rect = [ordered]@{
            left = $rect.Left
            top = $rect.Top
            right = $rect.Right
            bottom = $rect.Bottom
            width = $rect.Right - $rect.Left
            height = $rect.Bottom - $rect.Top
        }
    }
}

function Get-VisibleConhostWindows {
    $windows = New-Object System.Collections.Generic.List[Object]
    $callback = [Win32WindowProbe+EnumWindowsProc]{
        param([IntPtr]$hWnd, [IntPtr]$lParam)
        if (-not [Win32WindowProbe]::IsWindowVisible($hWnd)) {
            return $true
        }
        $processId = 0
        [void][Win32WindowProbe]::GetWindowThreadProcessId($hWnd, [ref]$processId)
        if ($processId -le 0) {
            return $true
        }
        try {
            $p = Get-Process -Id ([int]$processId) -ErrorAction Stop
            if ($p.ProcessName -eq "conhost") {
                $windows.Add([pscustomobject]@{
                    processId = [int]$processId
                    handle = $hWnd
                })
            }
        } catch {
        }
        return $true
    }
    [void][Win32WindowProbe]::EnumWindows($callback, [IntPtr]::Zero)
    return $windows
}

function Write-JsonLog {
    param([hashtable]$Payload)
    $json = $Payload | ConvertTo-Json -Depth 10 -Compress
    Add-Content -LiteralPath $LogPath -Value $json -Encoding UTF8
}

$startedAt = Get-Date
Write-JsonLog -Payload @{
    type = "watch_start"
    time = $startedAt.ToString("o")
    pid = $PID
    durationHours = $DurationHours
    pollMilliseconds = $PollMilliseconds
    logPath = $LogPath
}

$deadline = $startedAt.AddHours($DurationHours)
$seen = New-Object 'System.Collections.Generic.HashSet[String]'

try {
    while ((Get-Date) -lt $deadline) {
        $windows = Get-VisibleConhostWindows
        foreach ($windowInfo in $windows) {
            $handle = [IntPtr]$windowInfo.handle
            $processId = [int]$windowInfo.processId
            $key = "{0}:{1}" -f $processId, $handle.ToInt64()
            if (-not $seen.Add($key)) {
                continue
            }

            $procInfo = Get-ProcessInfoById -ProcessId $processId
            $parentPid = 0
            if ($procInfo) {
                $parentPid = [int]$procInfo.ppid
            }
            Write-JsonLog -Payload @{
                type = "visible_conhost"
                time = (Get-Date).ToString("o")
                process = $procInfo
                parent = Get-ProcessInfoById -ProcessId $parentPid
                parentChain = Get-ProcessChain -StartPid $parentPid
                window = Get-WindowSnapshot -Handle $handle
            }
        }
        Start-Sleep -Milliseconds ([Math]::Max(20, $PollMilliseconds))
    }
} finally {
    Write-JsonLog -Payload @{
        type = "watch_stop"
        time = (Get-Date).ToString("o")
        pid = $PID
    }
}
