"""CLI：主循环入口。

用法:
  uv run python -m pmbot.run --dry-run      # 模拟运行（不真下单）
  uv run python -m pmbot.run --live         # 实盘（真钱！）
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kronos PM 交易主循环")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认按模式派生：dry-run=data/，live=data_live/）")
    parser.add_argument(
        "--dry-run", dest="dry_run", action="store_true", help="模拟运行（默认，不真下单）"
    )
    parser.add_argument("--live", dest="dry_run", action="store_false", help="实盘运行（真钱）")
    parser.set_defaults(dry_run=True)  # 安全默认：忘记传参也不碰真钱
    parser.add_argument("--poll", type=int, default=1, help="轮询间隔秒数（tick 频率，1s = 止盈止损秒级响应）")
    parser.add_argument("--once", action="store_true", help="只跑一个 tick 后退出（调试）")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 依赖集中导入（函数内：入口模块冷启动不加载重型依赖链）
    from pmbot.book_sampler import BookSampler
    from pmbot.clob_executor import ClobExecutor, SimExecutor
    from pmbot.config import load_config
    from pmbot.main_loop import TradingLoop
    from pmbot.market_discovery import MarketDiscovery
    from pmbot.paths import paths_for
    from pmbot.single_instance import run_with_guard
    from pmbot.state import StateStore, TradeState
    from pmbot.strategy import create_strategy
    from pmbot.user_stream import UserStream

    cfg = load_config(args.config)
    symbol = cfg.symbols[0]
    paths = paths_for(not args.dry_run, args.data_dir)
    data_dir = paths.data_dir

    strategy = create_strategy(
        cfg.strategy,
        symbol=symbol,
        sample_count=cfg.sample_count,
        max_klines=cfg.max_klines,
        variant=cfg.model_variant,
        interval=cfg.market_interval,
        log_dir=data_dir,
        # 交易阈值注入：中间带信号不记录（不进入方向准确率评估，与引擎交易口径一致）
        thresholds={"p_up_buy": cfg.p_up_buy, "p_down_buy": cfg.p_down_buy},
    )
    discovery = MarketDiscovery(interval=cfg.market_interval)
    executor = SimExecutor() if args.dry_run else ClobExecutor()  # 两个适配器：模拟 / 实盘

    # Polymarket WS 市场频道（REST book 兜底）；代理从环境读取（polymarket 需代理）
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    sampler = BookSampler(executor.fetch_book, interval=2.0, proxy=proxy,
                                 book_path=f"{data_dir}/book.json")
    executor.attach_sampler(sampler)
    sampler.start()

    # 认证 WS（订单/成交推送）：凭证从 CLOB 缓存读取（与下单客户端同源），无则仅盘口 WS 工作
    user_stream = UserStream(executor.api_auth(), proxy=proxy)
    user_stream.start()

    state = StateStore(paths.status).load()
    mode = paths.mode
    if state is not None and state.mode and state.mode != mode:
        raise SystemExit(
            f"数据目录 {data_dir} 属于 {state.mode} 模式，当前启动为 {mode}。"
            f"模拟/实盘数据必须分离：实盘用 --data-dir data_live（或 start_bot.bat --live）"
        )
    if state is None or state.symbol != symbol:
        state = TradeState(symbol=symbol, mode=mode)
        logging.info("新建状态（symbol=%s, mode=%s）", symbol, mode)

    loop = TradingLoop(
        config=cfg,
        symbol=symbol,
        strategy=strategy,
        discovery=discovery,
        executor=executor,
        state=state,
        dry_run=args.dry_run,
        poll_sec=args.poll,
        user_stream=user_stream,
        trades_path=paths.trades,
        status_path=paths.status,
        control_path=f"{data_dir}/control.json",
    )

    def _run() -> None:
        from pmbot.control import read_control

        read_control(f"{data_dir}/control.json")  # 丢弃面板残留指令（如旧实例停机前的 stop），避免启动即停机
        if args.once:
            loop.tick(now_ms=int(time.time() * 1000))
        else:
            loop.run_forever()

    # 单实例守护：杀旧 run 实例防多开互踩状态文件；退出自动注销
    run_with_guard("run", _run, pid_file=paths.pid_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
