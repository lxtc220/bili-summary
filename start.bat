@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo    Bilibili 视频总结助手
echo ==========================================
echo.

rem 端口 8080 已在监听说明服务已启动（start.bat / 后台启动.vbs 共用此端口），
rem 直接打开页面即可，避免重复启动出双实例。
netstat -ano | findstr ":8080" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo 服务已在运行，正在打开页面...
    start http://localhost:8080
    goto end
)

where python >nul 2>&1
if not errorlevel 1 (
    python web_ui.py
    goto end
)

echo [错误] 未找到 python，请确认已安装 Python 3.10+ 并加入 PATH。

:end
echo.
echo ------------------------------------------
echo 程序运行结束，按任意键关闭窗口...
pause >nul
