Set WshShell = CreateObject("WScript.Shell")
' 启动 start.bat 且不显示窗口 (0 表示隐藏)
WshShell.Run "cmd.exe /c start.bat", 0, False
