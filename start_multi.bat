@echo off
REM ============================================================
REM Multi-symbol momentum paper test launcher (BTC + ETH + SOL)
REM Each symbol: separate process + separate data dir under data_multi/
REM Usage: start_multi.bat           start 3 bots + enter TUI
REM        start_multi.bat stop     gracefully stop all bots
REM NOTE: keep this file ASCII-only (cmd parses bat as GBK; UTF-8
REM       Chinese bytes would corrupt commands on double-click).
REM ============================================================
setlocal
cd /d %~dp0
title PMBOT Multi-TUI

if "%1"=="stop" goto stop

mkdir data_multi logs 2>nul

echo Starting BTC...
start "pmbot-BTC" /min cmd /c "uv run python -m pmbot.run --dry-run --symbol BTC --data-dir data_multi/btc --poll 2 >> logs\btc.log 2>&1"
echo Starting ETH...
start "pmbot-ETH" /min cmd /c "uv run python -m pmbot.run --dry-run --symbol ETH --data-dir data_multi/eth --poll 2 >> logs\eth.log 2>&1"
echo Starting SOL...
start "pmbot-SOL" /min cmd /c "uv run python -m pmbot.run --dry-run --symbol SOL --data-dir data_multi/sol --poll 2 >> logs\sol.log 2>&1"
echo 3 bots started, logs: logs\btc.log logs\eth.log logs\sol.log
echo.
echo Entering TUI (refresh 2s)... Ctrl-C exits panel, bots keep running
uv run python scripts\multi_panel.py
echo.
echo Panel exited. Bots still running in background.
echo Re-open panel:  uv run python scripts\multi_panel.py
echo Stop all bots:  start_multi.bat stop
pause
goto end

:stop
uv run python -c "from pmbot.control import write_control; [write_control('stop', f'data_multi/{s}/control.json') for s in ['btc','eth','sol']]; print('stop sent to 3 bots')"
echo Waiting for graceful shutdown...
timeout /t 12 /nobreak >nul
echo Done.

:end
endlocal
