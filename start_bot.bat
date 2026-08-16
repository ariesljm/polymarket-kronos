@echo off
rem 一键启动：主循环（后台）+ 监控面板（前台）。Ctrl-C 退出面板时自动停主循环。
rem 实盘模式：start_bot.bat --live   （真钱！默认 dry-run 模拟）
cd /d %~dp0
set HTTPS_PROXY=http://127.0.0.1:10808
set HTTP_PROXY=http://127.0.0.1:10808
set VIRTUAL_ENV=%CD%\.venv
set PYTHONPATH=%CD%\src;%CD%\.venv\Lib\site-packages
".venv\Scripts\python.exe" -c "import sys;print(sys._base_executable)" > "%TEMP%\pmbot_base_py.tmp" 2>nul
set /p BASE_PY=<"%TEMP%\pmbot_base_py.tmp"
del "%TEMP%\pmbot_base_py.tmp" 2>nul
"%BASE_PY%" -m pmbot.start_bot %*
