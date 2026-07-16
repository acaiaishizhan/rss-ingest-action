' Hidden-window launcher for the rss-local-feed-publisher scheduled task.
' Keep this file ASCII-only: wscript decodes .vbs using the system ANSI codepage.
' Argument 0 = WSL distro.
Option Explicit

Dim sh, fso, scriptDir, runner, distro, command, rc
If WScript.Arguments.Count <> 1 Then
    WScript.Quit 2
End If

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
runner = fso.BuildPath(scriptDir, "run_local_feed_publisher_local.ps1")
distro = WScript.Arguments(0)

command = "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden " & _
    "-ExecutionPolicy Bypass -File " & QuoteArg(runner) & _
    " -Distro " & QuoteArg(distro)
rc = sh.Run(command, 0, True)
WScript.Quit rc

Function QuoteArg(value)
    QuoteArg = Chr(34) & CStr(value) & Chr(34)
End Function
