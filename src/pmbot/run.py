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
    parser = argparse.ArgumentParser(description="Polymarket 策略交易主循环")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认按模式派生：dry-run=data/，live=data_live/）")
    parser.add_argument(
        "--dry-run", dest="dry_run", action="store_true", help="模拟运行（默认，不真下单）"
    )
    parser.add_argument("--live", dest="dry_run", action="store_false", help="实盘运行（真钱）")
    parser.set_defaults(dry_run=True)  # 安全默认：忘记传参也不碰真钱
    parser.add_argument("--poll", type=int, default=1, help="轮询间隔秒数（tick 频率，1s = 止盈止损秒级响应）")
    parser.add_argument("--symbol", default=None, help="覆盖 config symbols[0]（多标的并行：各进程 --symbol BTC/ETH/SOL --data-dir data_X）")
    parser.add_argument("--once", action="store_true", help="只跑一个 tick 后退出（调试）")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # 降噪：httpx/py_clob 每 tick 刷屏的请求日志提升到 WARNING（曾占满日志 90%+）
    for noisy in ("httpx", "py_clob_client_v2.http_helpers.helpers", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # 依赖集中导入（函数内：入口模块冷启动不加载重型依赖链）
    from pmbot.book_sampler import BookSampler
    from pmbot.clob_executor import ClobExecutor, SimExecutor
    from pmbot.config import load_config
    from pmbot.main_loop import TradingLoop
    from pmbot.market_discovery import MarketDiscovery
    from pmbot.paths import paths_for
    from pmbot.single_instance import run_with_guard
    from pmbot.spot_ticker import SpotTickerThread
    from pmbot.state import StateStore, TradeState
    from pmbot.strategy import create_strategy, strategy_class
    from pmbot.user_stream import UserStream

    cfg = load_config(args.config)
    symbol = args.symbol or cfg.symbols[0]
    paths = paths_for(not args.dry_run, args.data_dir)
    data_dir = paths.data_dir

    # Binance 实时价线程提前创建：momentum 策略用它做穿越检测（WS ~1s 推送，
    # 比每 tick REST 快；更早发现穿越 → 更可能抓做市商未调价的便宜档）。
    ticker = SpotTickerThread(symbol=symbol)
    strat_kwargs = {}
    # 依赖注入按策略声明（needs_fetch_price）：新策略声明即可，run.py 不再
    # 按策略名硬编码 if（策略名单单一事实源在 registry + 策略类自身）。
    strat_cls = strategy_class(cfg.strategy)
    if getattr(strat_cls, "needs_fetch_price", False):
        strat_kwargs["fetch_price"] = ticker.latest_price
    strategy = create_strategy(
        cfg.strategy,
        symbol=symbol,
        log_dir=data_dir,
        config=cfg.to_strategy_config(),  # 策略参数窄视图（间隔/阈值）
        **strat_kwargs,
    )
    discovery = MarketDiscovery(interval=cfg.market_interval)
    executor = SimExecutor() if args.dry_run else ClobExecutor()  # 两个适配器：模拟 / 实盘

    # Polymarket WS 市场频道（REST book 兜底）；代理从环境读取（polymarket 需代理）
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    sampler = BookSampler(executor.fetch_book, interval=1.0, proxy=proxy,
                                 book_path=f"{data_dir}/book.json")
    executor.attach_sampler(sampler)
    sampler.start()

    # 认证 WS（订单/成交推送）：仅实盘使用——dry-run 不触碰真实凭证（clob_creds.json
    # 存在时也不连接认证 WS），模拟持仓与真实钱包无关，事件空转。
    user_stream = UserStream(executor.api_auth() if not args.dry_run else None, proxy=proxy)
    user_stream.start()

    # Binance 实时价线程（方向一致性过滤数据源 + momentum 穿越检测）：WS miniTicker + REST 兜底。
    # 镜像端点直连可达（data_source 同款实证），不传 proxy 优先直连。
    # ticker 已提前创建（momentum 注入用），此处启动。
    ticker.start()

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
        ticker=ticker,
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

    # 终端关闭（CTRL_CLOSE）在 Windows 触发 SIGTERM → 优雅停机（同 Ctrl-C）
    import signal as _signal

    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt

    try:
        _signal.signal(_signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        pass

    # 单实例守护：杀旧 run 实例防多开互踩状态文件；退出自动注销
    run_with_guard("run", _run, pid_file=paths.pid_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
