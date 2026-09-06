@echo off
setlocal
cd /d %~dp0
title PMBOT Multi-TUI
if "%1"=="stop" goto stop

mkdir data_multi logs 2>nul

echo Starting BTC (background, log via logging -> logs\btc.log)...
start /b cmd /c "uv run python -m pmbot.run --dry-run --symbol BTC --data-dir data_multi/btc --poll 2 >nul 2>&1"
echo Starting ETH (background, log via logging -> logs\eth.log)...
start /b cmd /c "uv run python -m pmbot.run --dry-run --symbol ETH --data-dir data_multi/eth --poll 2 >nul 2>&1"
echo Starting SOL (background, log via logging -> logs\sol.log)...
start /b cmd /c "uv run python -m pmbot.run --dry-run --symbol SOL --data-dir data_multi/sol --poll 2 >nul 2>&1"
echo.
echo Entering panel (refresh 2s)... Ctrl-C exits panel only
uv run python scripts\multi_panel.py
echo.
echo Panel closed. Bots keep running in background.
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
