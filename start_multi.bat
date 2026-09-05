@echo off
REM ============================================================
REM 多标的 momentum paper test 启动脚本（BTC + ETH + SOL 三个 5m 市场并行）
REM 每个标的独立进程 + 独立数据目录（status/trades/pid 互不干扰）
REM 数据统一放 data_multi/ 下（每标的一个子目录: btc eth sol）
REM 用法: start_multi.bat          启动三个标的
REM        start_multi.bat stop    优雅停止全部
REM ============================================================
setlocal
cd /d %~dp0

if "%1"=="stop" goto stop

mkdir data_multi logs 2>nul

echo 启动 BTC...
start "pmbot-BTC" /min cmd /c "uv run python -m pmbot.run --dry-run --symbol BTC --data-dir data_multi/btc --poll 2 >> logs\btc.log 2>&1"
echo 启动 ETH...
start "pmbot-ETH" /min cmd /c "uv run python -m pmbot.run --dry-run --symbol ETH --data-dir data_multi/eth --poll 2 >> logs\eth.log 2>&1"
echo 启动 SOL...
start "pmbot-SOL" /min cmd /c "uv run python -m pmbot.run --dry-run --symbol SOL --data-dir data_multi/sol --poll 2 >> logs\sol.log 2>&1"
echo 三个标的已启动, 日志: logs\btc.log logs\eth.log logs\sol.log
echo TUI 聚合面板: uv run python scripts\multi_panel.py
goto end

:stop
uv run python -c "from pmbot.control import write_control; [write_control('stop', f'data_multi/{s}/control.json') for s in ['btc','eth','sol']]; print('stop 已发送三标的')"
echo 等待优雅停机...
timeout /t 12 /nobreak >nul
echo 停止完成

:end
endlocal