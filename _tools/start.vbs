' DropToWin - manual start
' Double-click this file to start the service if it stopped.
' Works from any folder: it locates watchdog.py next to itself.
' NOTE 1: keep this file pure ASCII - WSH loads .vbs as ANSI (GBK on
'         Chinese Windows); non-ASCII bytes can swallow quotes and break
'         compilation with an "unterminated string" error.
' NOTE 2: keep CRLF line endings (.gitattributes pins them).
' NOTE 3: no hardcoded paths - pythonw.exe is looked up on any account.

Dim fso, sh, base, scr, py
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
scr  = base & "\watchdog.py"

py = FindPython()

If py = "pythonw.exe" Then
    MsgBox "Python not found on PATH either." & vbCrLf & vbCrLf & _
           "Install Python 3.10+ and tick ""Add Python to PATH""" & vbCrLf & _
           "(https://www.python.org/downloads/), then double-click again.", _
           16, "DropToWin"
    WScript.Quit
End If

If Not fso.FileExists(scr) Then
    MsgBox "watchdog.py not found next to this file:" & vbCrLf & scr, 16, "DropToWin"
    WScript.Quit
End If

On Error Resume Next
sh.Run """" & py & """ """ & scr & """", 0, False
If Err.Number <> 0 Then
    MsgBox "Could not start:" & vbCrLf & py & vbCrLf & vbCrLf & _
           Err.Description & vbCrLf & vbCrLf & _
           "Try running install.bat again.", 16, "DropToWin"
End If


' Look for pythonw.exe: per-user install first, then machine-wide folders,
' then PATH. Highest version number wins (Python314 beats Python39).
Function FindPython()
    Dim roots, i, best, bestPath
    best = -1
    bestPath = ""

    roots = Array( _
        sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python", _
        sh.ExpandEnvironmentStrings("%PROGRAMFILES%"), _
        sh.ExpandEnvironmentStrings("%PROGRAMFILES(X86)%"), _
        "C:\" _
    )
    For i = 0 To UBound(roots)
        ScanDir roots(i), best, bestPath
    Next

    If bestPath <> "" Then
        FindPython = bestPath
    Else
        FindPython = "pythonw.exe"
    End If
End Function


Sub ScanDir(folder, best, bestPath)
    Dim f, n, v
    On Error Resume Next
    If folder = "" Then Exit Sub
    If Not fso.FolderExists(folder) Then Exit Sub
    For Each f In fso.GetFolder(folder).SubFolders
        If LCase(Left(f.Name, 6)) = "python" Then
            If fso.FileExists(f.Path & "\pythonw.exe") Then
                n = Mid(f.Name, 7)
                If IsNumeric(n) Then
                    v = CLng(n)
                    If v > best Then
                        best = v
                        bestPath = f.Path & "\pythonw.exe"
                    End If
                End If
            End If
        End If
    Next
End Sub
