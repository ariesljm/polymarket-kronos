@echo off
setlocal
cd /d %~dp0
title PMBOT Multi-TUI LIVE
if "%1"=="stop" goto stop

echo **************************************************************
echo *   WARNING: LIVE TRADING - REAL USDC AT RISK!               *
echo *   Requires: funded proxy wallet + v2rayN proxy running.    *
echo *   Simulation: use start_multi.bat (dry-run).               *
echo **************************************************************
echo.
mkdir data_live logs 2>nul

rem ---- Live preflight gate: refuse to start if trading cannot work ----
echo Running live preflight (balance / allowance / min order size)...
uv run python -c "import sys;from pmbot.config import load_config;from pmbot.clob_executor import ClobExecutor;from pmbot.preflight import live_preflight;from pmbot.run import resolve_proxy;resolve_proxy();p=live_preflight(load_config('config.yaml'),ClobExecutor());[print('  [FAIL] '+x) for x in p];sys.exit(1 if p else 0)"
if errorlevel 1 goto preflight_fail
echo Preflight OK.
echo.

echo Starting LIVE bot (BTC+ETH+SOL+XRP+DOGE+BNB, log: logs\multi_live.log)...
rem Bot and panel share this console (/b): Ctrl-C reaches both, bot shuts down gracefully.
rem Console output to a file: startup crashes stay visible without corrupting the TUI.
start /b cmd /c "uv run python -m pmbot.run_multi --live --symbols BTC,ETH,SOL,XRP,DOGE,BNB --data-dirs data_live/btc,data_live/eth,data_live/sol,data_live/xrp,data_live/doge,data_live/bnb --poll 1 >>logs\multi_live_console.log 2>&1"
echo.
echo Entering LIVE panel (refresh 2s)... Ctrl-C stops bot and exits panel
uv run python scripts\multi_panel.py --live --data-dir data_live/btc,data_live/eth,data_live/sol,data_live/xrp,data_live/doge,data_live/bnb
echo.
echo Bot stopped.
pause
goto end

:preflight_fail
echo.
echo **************************************************************
echo *  PREFLIGHT FAILED - live bot NOT started.                  *
echo *  Fix the [FAIL] items above, then run this file again.     *
echo **************************************************************
pause
goto end

:stop
uv run python -c "from pmbot.control import write_control; [write_control('stop', f'data_live/{s}/control.json') for s in ['btc','eth','sol','xrp','doge','bnb']]; print('stop sent')"
echo Waiting for graceful shutdown...
timeout /t 12 /nobreak >nul
echo Done.

:end
endlocal
