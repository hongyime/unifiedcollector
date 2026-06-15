' run_hidden.vbs - launch a console command with NO visible window.
'
' The UnifiedCollectorBackup scheduled task must run with an Interactive logon
' (Docker Desktop's named pipe is only reachable from the logged-on user's
' session, not S4U) -- but an Interactive .bat flashes a cmd.exe window. wscript
' runs in that same interactive session yet Shell.Run with intWindowStyle=0
' starts the command hidden, so the backup runs invisibly.
'
' Usage:  wscript.exe run_hidden.vbs "<full command line to run>"
Option Explicit
Dim sh, cmd
If WScript.Arguments.Count < 1 Then WScript.Quit 2
cmd = WScript.Arguments(0)
Set sh = CreateObject("WScript.Shell")
' 0 = hidden window, True = wait for completion so the task captures the exit code
WScript.Quit sh.Run("cmd /c """ & cmd & """", 0, True)
