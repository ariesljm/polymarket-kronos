@echo off
REM ============================================================
REM 动量策略(momentum)paper test 启动脚本
REM 研究依据: docs/reports/momentum_strategy.md (2026-09-04)
REM  5m/15m 窗口内 BTC 动量:穿越阈值后方向延续 72-97% (8000+ K线大样本)
REM 用法:
REM   start_momentum.bat          启动 bot(dry-run) + TUI(终端面板)
REM   start_momentum.bat --bot    只启动 bot(后台,日志 logs/momentum_paper.log)
REM   start_momentum.bat --tui    只启动 TUI(只读面板,需 bot 已在运行)
REM ============================================================
setlocal
cd /d %~dp0

if "%1"=="--bot" goto bot
if "%1"=="--tui" goto tui

echo [1/2] 启动 momentum bot (dry-run, 轮询2s, 后台)...
goto bot

:bot
if exist data\bot.pids (echo 已有一个 bot 实例在运行, 请先停止或阅读 data\bot.pids) else (
  start "pmbot-momentum" /min cmd /c "uv run python -m pmbot.run --dry-run --poll 2 >> logs\momentum_paper.log 2>&1"
  echo bot 已启动, 日志: logs\momentum_paper.log
)
if "%1"=="--bot" goto end

echo [2/2] 启动 TUI 终端面板 (Ctrl-C 退出面板, 不影响 bot)...
:tui
uv run python -m pmbot.monitor --data-dir data --config config.yaml
:end
endlocal