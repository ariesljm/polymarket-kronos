@echo off
cd /d %~dp0
rem 实盘一键启动：双击即启动实盘主循环（后台）+ 监控面板（前台）。Ctrl-C 退出面板时自动停主循环。
rem 使用前提：本文件必须在项目目录 E:\code\polymarket-kronos 中双击（依赖同目录 pyproject.toml/.venv）
if not exist pyproject.toml (
    echo [错误] 未找到项目文件 pyproject.toml
    echo        请把 start_bot_live.bat 放回项目目录 E:\code\polymarket-kronos 再双击
    pause
    exit /b 1
)
if not exist .venv (
    echo [错误] 未找到虚拟环境 .venv，请先执行: uv sync
    pause
    exit /b 1
)
echo.
echo  ==================================================
echo   实盘模式启动（真钱交易！）
echo   换代理节点后请先运行 check_proxy.bat 确认已放行
echo  ==================================================
echo.
choice /C YN /N /T 15 /D N /M "确认启动实盘？Y=启动  N=取消（15 秒无操作自动取消）... "
if errorlevel 2 exit /b
set HTTPS_PROXY=http://127.0.0.1:10808
set HTTP_PROXY=http://127.0.0.1:10808
set VIRTUAL_ENV=%CD%\.venv
set PYTHONPATH=%CD%\src;%CD%\.venv\Lib\site-packages
".venv\Scripts\python.exe" -c "import sys;print(sys._base_executable)" > "%TEMP%\pmbot_base_py.tmp" 2>nul
set /p BASE_PY=<"%TEMP%\pmbot_base_py.tmp"
del "%TEMP%\pmbot_base_py.tmp" 2>nul
"%BASE_PY%" -m pmbot.start_bot --live %*
echo.
echo [退出] 实盘已停止（错误代码 %errorlevel%）
pause
