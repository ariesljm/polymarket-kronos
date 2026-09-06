@echo off
setlocal
cd /d %~dp0
title PMBOT Multi-TUI LIVE
if "%1"=="stop" goto stop

echo **************************************************************
echo *   WARNING: LIVE TRADING - REAL USDC AT RISK!               *
echo *   Check wallet balance (pUSD) before continuing.           *
echo *   Simulation: use start_multi.bat (dry-run).               *
echo **************************************************************
echo.
mkdir data_live logs 2>nul

echo Starting LIVE multi-symbol bot (BTC+ETH+SOL, real money)...
echo Log: logs\multi_live.log  (NOTE: currently shared log file)
start /b cmd /c "uv run python -m pmbot.run_multi --live --symbols BTC,ETH,SOL --data-dirs data_live/btc,data_live/eth,data_live/sol --poll 1 >nul 2>&1"
echo.
echo Entering LIVE panel... Ctrl-C exits panel only
uv run python scripts\multi_panel.py --data-dir data_live/btc,data_live/eth,data_live/sol --live
echo.
echo Panel closed. LIVE bots keep running in background.
echo Re-open LIVE panel: uv run python scripts\multi_panel.py --data-dir data_live/btc,data_live/eth,data_live/sol --live
echo Stop all LIVE bots:  start_multi_live.bat stop
pause
goto end

:stop
uv run python -c "from pmbot.control import write_control; [write_control('stop', f'data_live/{s}/control.json') for s in ['btc','eth','sol']]; print('stop sent')"
echo Waiting for graceful shutdown...
timeout /t 12 /nobreak >nul
echo Done.

:end
endlocal
