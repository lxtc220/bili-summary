@echo off
chcp 65001 >nul
echo 正在停止 Bilibili 视频总结助手...
taskkill /f /im python.exe /t 2>nul
taskkill /f /im streamlit.exe /t 2>nul
echo.
echo 已停止所有相关后台进程。
pause
