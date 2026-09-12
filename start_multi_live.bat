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
rem Bot and panel share this console (/b). Console output to a file: startup crashes
rem stay visible without corrupting the TUI. 注：Ctrl-C 只可靠地停掉前台面板，
rem bot 的停机走下面的 :stop 路径（曾观察到仅 Ctrl-C 时 bot 静默死亡/变孤儿）。
start /b cmd /c "uv run python -m pmbot.run_multi --live --symbols BTC,ETH,SOL,XRP,DOGE,BNB --data-dirs data_live/btc,data_live/eth,data_live/sol,data_live/xrp,data_live/doge,data_live/bnb --poll 1 >>logs\multi_live_console.log 2>&1"
echo.
echo Entering LIVE panel (refresh 2s)... Ctrl-C exits panel and stops the bot
uv run python scripts\multi_panel.py --live --data-dir data_live/btc,data_live/eth,data_live/sol,data_live/xrp,data_live/doge,data_live/bnb
echo.
rem 面板退出（Ctrl-C/异常）：走确定性停机，不依赖共享 console 的 Ctrl-C 传播。
rem 之前只 echo "Bot stopped." —— bot 可能还在后台成为孤儿继续实盘下单
rem （从已有终端启动 bat 时 console 不关闭，子进程不被连带杀死）。
goto stop

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
rem 兜底：优雅停机超时仍存活的残留进程整树强杀（pid 文件记录）
uv run python -c "import json;from pathlib import Path;from pmbot.single_instance import InstanceGuard;from pmbot.entry_support import kill_tree;pf=Path('data_live/bot.pids');d=json.loads(pf.read_text()) if pf.is_file() else {};left=[p for p in d.values() if InstanceGuard.alive(p)];print('leftover:',left);[kill_tree(p) for p in left]"
echo Bot stopped.
pause

:end
endlocal
