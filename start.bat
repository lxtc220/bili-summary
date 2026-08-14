@echo off
setlocal
cd /d "%~dp0"

where pythonw >nul 2>&1
if not errorlevel 1 (
    start "" pythonw "%~dp0desktop_app.py"
    exit /b 0
)

where python >nul 2>&1
if not errorlevel 1 (
    start "" python "%~dp0desktop_app.py"
    exit /b 0
)

echo 未找到 Python，请先安装 Python 3.10 或更高版本并加入 PATH。
pause
exit /b 1
