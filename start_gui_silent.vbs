' Zero-window launcher for the SC monitor GUI.
' If the venv is missing, falls back to start_gui.bat (console shown once for bootstrap).
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
If fso.FileExists(dir & "\.venv\Scripts\pythonw.exe") Then
    shell.CurrentDirectory = dir
    shell.Run """" & dir & "\.venv\Scripts\pythonw.exe"" """ & dir & "\gui.py""", 0, False
Else
    shell.CurrentDirectory = dir
    shell.Run """" & dir & "\start_gui.bat""", 1, False
End If
