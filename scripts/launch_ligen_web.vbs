Option Explicit

Dim shell, fso
Dim ps1, cmd

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

ps1 = "C:\Users\logan\Desktop\PKU group\doi_harvest\scripts\launch_ligen_web.ps1"

If Not fso.FileExists(ps1) Then
    MsgBox "Missing launcher: " & ps1, vbExclamation, "Ligen Local Web"
    WScript.Quit 1
End If

cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File " & Chr(34) & ps1 & Chr(34)
shell.Run cmd, 0, False
