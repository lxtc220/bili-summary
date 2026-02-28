@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

echo ==========================================
echo    Bilibili 视频总结助手
echo ==========================================
echo.

where python >nul 2>&1
if not errorlevel 1 (
    python -m streamlit run web_ui.py
    goto end
)

where py >nul 2>&1
if not errorlevel 1 (
    py -m streamlit run web_ui.py
    goto end
)

echo [错误] 未找到 python 或 py，请确认已安装 Python 3

:end
echo.
echo ------------------------------------------
echo 程序运行结束，按任意键关闭窗口...
pause >nul
