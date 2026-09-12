"""CLI：多标的单进程主循环（每标的一个 TradingLoop 线程）。

相比多进程并行（每标的 1 进程 ×6 层 uv shim/base）：
- 1 个进程、1 份日志（logs/multi.log，按天滚动）、1 个 pid 文件
- 无重复启动/互杀/日志分散问题（多进程时代：分进程反复 kill_old、日志句柄竞争）
- 每标的独立状态/数据目录（data_multi/eth 等）与独立 WS 线程，行为与单标的完全一致

用法:
  uv run python -m pmbot.run_multi --symbols ETH,SOL --data-dirs data_multi/eth,data_multi/sol --dry-run
  （如需跑其它标的，例：--symbols BTC,ETH,SOL --data-dirs data_multi/btc,data_multi/eth,data_multi/sol）
  Ctrl-C / SIGTERM → 各标的优雅停机（撤单→结算→落盘）后退出
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from pathlib import Path

from pmbot.run import build_loop


LOG_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def _setup_multi_logging(mode: str) -> Path:
    """单文件日志:dry-run=logs/multi.log, live=logs/multi_live.log(按天滚动 14 天,不混)。"""
    log_dir = Path(__file__).resolve().parents[2] / "logs"
    fname = "multi_live.log" if mode == "live" else "multi.log"
    from pmbot.entry_support import setup_logging

    setup_logging(log_dir, fname)
    return log_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多标的单进程主循环（每标的一个线程）")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument(
        "--symbols", default="BTC,ETH,SOL,XRP,DOGE,BNB", help="逗号分隔标的列表，与 --data-dirs 一一对应"
    )
    parser.add_argument(
        "--data-dirs", default="data_multi/btc,data_multi/eth,data_multi/sol,data_multi/xrp,data_multi/doge,data_multi/bnb",
        help="逗号分隔数据目录列表（每标的一个，顺序对应 --symbols）",
    )
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="模拟运行（默认）")
    parser.add_argument("--live", dest="dry_run", action="store_false", help="实盘运行（真钱）")
    parser.set_defaults(dry_run=True)
    parser.add_argument("--poll", type=int, default=1, help="轮询间隔秒数（1s：穿越后尽早决策，抓 MM 未调价窗口）")
    args = parser.parse_args(argv)

    from pmbot.config import load_config
    from pmbot.paths import RuntimePaths, paths_for
    from pmbot.single_instance import run_with_guard

    cfg = load_config(args.config)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    data_dirs = [d.strip() for d in args.data_dirs.split(",") if d.strip()]
    for dd in data_dirs:  # 数据目录自建（live 新目录 data_live/* 不存在时会挂）
        Path(dd).mkdir(parents=True, exist_ok=True)
    if len(symbols) != len(data_dirs):
        logging.error("--symbols 与 --data-dirs 数量不一致（%d vs %d）", len(symbols), len(data_dirs))
        return 2
    mode = "dry-run" if args.dry_run else "live"

    log_dir = _setup_multi_logging(mode)

    # 实盘前置自检（真钱守门）：先写回代理环境——py_clob_client 的 httpx 客户端
    # 在首次调用时才创建，环境变量必须在此之前就位；不通过则拒绝启动。
    if not args.dry_run:
        from pmbot.entry_support import run_live_preflight

        rc = run_live_preflight(cfg)
        if rc != 0:
            return rc
    bundles: list = []
    loops: list = []
    for sym, dd in zip(symbols, data_dirs):
        paths = RuntimePaths(data_dir=dd, mode=mode)
        bundle = build_loop(cfg, symbol=sym, paths=paths,
                            dry_run=args.dry_run, poll_sec=args.poll)
        bundles.append(bundle)
        loops.append(bundle.loop)
        logging.info("标的 %s 已构建（数据目录 %s, 策略 %s）", sym, dd, cfg.strategy)

    logging.info("=" * 56)
    logging.info("pmbot multi 启动: %d 标的", len(loops))
    logging.info("模式=%s 轮询=%ds 策略=%s 窗口=%s", mode, args.poll, cfg.strategy, cfg.market_interval)
    logging.info("日志: %s", log_dir / ("multi_live.log" if mode == "live" else "multi.log"))
    logging.info("=" * 56)

    from pmbot.entry_support import on_sigterm

    on_sigterm()

    def _run() -> int:
        stop_event = threading.Event()
        errors: list = []
        threads = [
            threading.Thread(
                target=_run_loop,
                args=(loop, stop_event, errors),
                name=f"loop-{loop.symbol}",
                daemon=True,
            )
            for loop in loops
        ]
        for t in threads:
            t.start()
        try:
            while any(t.is_alive() for t in threads):
                time.sleep(1)
        except KeyboardInterrupt:
            logging.info("收到 Ctrl-C / SIGTERM，优雅停机全部标的...")
            for loop in loops:
                loop.request_stop()
            for t in threads:
                t.join(timeout=15)
        finally:
            # WS 线程生命周期收口：全部标的 sampler/ticker/user_stream 统一停止
            for b in bundles:
                b.shutdown_all()
        # 任一标的线程异常 → 非 0 退出（日志已有 traceback;退出码不再掩盖崩溃）
        return 1 if errors else 0

    pid_dir = "data_multi" if args.dry_run else "data_live"
    return run_with_guard("run-multi", _run, pid_file=f"{pid_dir}/bot.pids")


def _run_loop(loop, stop_event: threading.Event, errors: list) -> None:
    """单标的主循环线程体：run_forever 内部已处理 KeyboardInterrupt/异常隔离。

    errors: 共享列表，线程异常时记录（主线程据此返回非 0 退出码）。
    """
    try:
        from pmbot.control import read_control

        read_control(loop.control_path)  # 丢弃面板残留指令（同单标的 run.py）
        loop.run_forever()
    except Exception:
        logging.exception("标的 %s 主循环异常退出", getattr(loop, "symbol", "?"))
        errors.append(getattr(loop, "symbol", "?"))
    finally:
        logging.info("标的 %s 线程结束", getattr(loop, "symbol", "?"))


if __name__ == "__main__":
    sys.exit(main())