@echo off
setlocal
cd /d %~dp0
title PMBOT Multi-TUI
if "%1"=="stop" goto stop

mkdir data_multi logs 2>nul

echo Starting multi-symbol bot (ETH+SOL, BTC as signal source only, log: logs\multi.log)...
start /b cmd /c "uv run python -m pmbot.run_multi --symbols ETH,SOL --data-dirs data_multi/eth,data_multi/sol --dry-run --poll 1 >nul 2>&1"
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
uv run python -c "from pmbot.control import write_control; [write_control('stop', f'data_multi/{s}/control.json') for s in ['eth','sol']]; print('stop sent')"
echo Waiting for graceful shutdown...
timeout /t 12 /nobreak >nul
echo Done.

:end
endlocal