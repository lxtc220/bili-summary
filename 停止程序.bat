@echo off
chcp 65001 >nul
setlocal

echo 正在停止 B站视频总结助手...
taskkill /F /IM desktop_app.exe /T >nul 2>&1
taskkill /F /IM desktop_engine.exe /T >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -Command "$root = [IO.Path]::GetFullPath('%~dp0'); Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -and $_.CommandLine.Contains($root) -and ($_.CommandLine.Contains('desktop_app.py') -or $_.CommandLine.Contains('desktop_engine.py')) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo 已发送停止命令。
pause
