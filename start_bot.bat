@echo off
rem 一键启动：主循环（后台）+ 监控面板（前台）。Ctrl-C 退出面板时自动停主循环。
rem 实盘模式：start_bot.bat --live   （真钱！默认 dry-run 模拟）
cd /d %~dp0
set HTTPS_PROXY=http://127.0.0.1:10808
set HTTP_PROXY=http://127.0.0.1:10808
uv run python -m pmbot.start_bot %*
