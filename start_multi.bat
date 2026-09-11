@echo off
setlocal
cd /d %~dp0
title PMBOT Multi-TUI
if "%1"=="stop" goto stop

mkdir data_multi logs 2>nul

echo Starting multi-symbol bot (BTC+ETH+SOL+XRP+DOGE+BNB, log: logs\multi.log)...
rem Bot and panel share this console (/b): Ctrl-C reaches both, bot shuts down gracefully.
start /b cmd /c "uv run python -m pmbot.run_multi --symbols BTC,ETH,SOL,XRP,DOGE,BNB --data-dirs data_multi/btc,data_multi/eth,data_multi/sol,data_multi/xrp,data_multi/doge,data_multi/bnb --dry-run --poll 1 >nul 2>&1"
echo.
echo Entering panel (refresh 2s)... Ctrl-C stops bot and exits panel
uv run python scripts\multi_panel.py
echo.
echo Bot stopped.
pause
goto end

:stop
uv run python -c "from pmbot.control import write_control; [write_control('stop', f'data_multi/{s}/control.json') for s in ['btc','eth','sol','xrp','doge','bnb']]; print('stop sent')"
echo Waiting for graceful shutdown...
timeout /t 12 /nobreak >nul
echo Done.

:end
endlocal
