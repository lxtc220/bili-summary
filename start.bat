@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo    Bilibili 视频总结助手
echo ==========================================
echo.

rem 端口 8501/8502 已在监听说明服务已启动（start.bat / 后台启动.vbs 共用此逻辑），
rem 直接打开页面即可，避免重复启动出双实例。
rem 注：streamlit 不带 --server.port 启动时，若 8501 被占用会自动改用 8502，
rem 所以这里 8501、8502 都要检查。
netstat -ano | findstr ":8501" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo 服务已在运行（端口 8501），正在打开页面...
    start http://localhost:8501
    goto end
)
netstat -ano | findstr ":8502" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo 服务已在运行（端口 8502），正在打开页面...
    start http://localhost:8502
    goto end
)

rem 优先用 py（本机 py 默认指向 Python312，是该应用实测稳定的环境；
rem python 命令可能指向其它版本，如 Python310 上 funasr 预加载不稳定）
where py >nul 2>&1
if not errorlevel 1 (
    py -m streamlit run web_ui.py
    goto end
)

where python >nul 2>&1
if not errorlevel 1 (
    python -m streamlit run web_ui.py
    goto end
)

echo [错误] 未找到 python 或 py，请确认已安装 Python 3

:end
echo.
echo ------------------------------------------
echo 程序运行结束，按任意键关闭窗口...
pause >nul
