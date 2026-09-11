"""CLI：主循环入口。

用法:
  uv run python -m pmbot.run --dry-run      # 模拟运行（不真下单）
  uv run python -m pmbot.run --live         # 实盘（真钱！）
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from pmbot.paths import RuntimePaths
from pmbot.state import TradeState


def _setup_logging(log_dir: Path, symbol: str) -> None:
    """日志：按天滚动文件 logs/{symbol}.log（保留 14 天，UTF-8）
    + 交互终端（stderr 为 TTY）时附加控制台输出。
    后台启动（nohup/start /b 重定向到 nul）只写文件——日志唯一来源。
    """
    root = logging.getLogger()
    if root.handlers:  # 重复启动防御（同一进程多次调用 main）
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = logging.handlers.TimedRotatingFileHandler(
        log_dir / f"{symbol}.log", when="midnight", backupCount=14, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    handlers: list[logging.Handler] = [fh]
    if sys.stderr.isatty():
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        handlers.append(sh)
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    # 降噪：httpx/py_clob 每 tick 刷屏的请求日志提升到 WARNING（曾占满日志 90%+）
    for noisy in ("httpx", "py_clob_client_v2.http_helpers.helpers", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@dataclass
class LoopBundle:
    """单标的主循环全套件（run.py 单标的 与 run_multi.py 多标的多线程共用）。

    持有三条 WS 线程引用并负责 shutdown（stop 谁启动谁）：启动分散在
    build_loop，停机集中在此——消除『谁都能 start、没人 stop』的生命周期分裂。
    """

    loop: TradingLoop
    state: TradeState
    paths: RuntimePaths
    symbol: str
    ticker: "SpotTickerThread" = None  # type: ignore[assignment]
    sampler: "BookSampler" = None  # type: ignore[assignment]
    user_stream: "UserStream" = None  # type: ignore[assignment]

    def shutdown_all(self) -> None:
        """停掉本标的全部 WS 线程（幂等；异常只记日志不阻断其它标的）。"""
        import logging as _lg

        for c in (self.user_stream, self.sampler, self.ticker):
            try:
                if c is not None:
                    c.stop()
            except Exception:
                _lg.exception("stop %s 失败", type(c).__name__)


DEFAULT_PROXY = "http://127.0.0.1:10808"


def resolve_proxy() -> str:
    """解析本机代理并**写回环境变量**（WS / REST / CLOB 三条链路的单一事实源）。

    为什么要写回 os.environ：py_clob_client_v2 没有代理参数，只靠 httpx 的
    trust_env 读 HTTP(S)_PROXY（或 Windows 系统代理）。cmd/bat 启动时环境里没
    这个变量，一旦系统代理被关（v2rayN 切"不改变系统代理"），实盘下单会静默
    直连被墙——把兜底值显式写进环境，使下单链路不再依赖系统代理开关。

    另：websockets 17 的 proxy=None 是强制直连（不读环境变量），故返回值仍须
    显式传给三条 WS；data_source 传 {"https": None} 显式覆盖，不受此影响。
    """
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or DEFAULT_PROXY
    os.environ.setdefault("HTTPS_PROXY", proxy)
    os.environ.setdefault("HTTP_PROXY", proxy)
    return proxy


def build_loop(cfg, *, symbol: str, paths: RuntimePaths, dry_run: bool, poll_sec: int) -> LoopBundle:
    """构造单标的主循环全栈（共享 Config/代理; 独立状态/数据目录/WS 线程）。

    多标的并行时每标的调一次：各自的 BookSampler/SpotTicker/StateStore，
    数据目录独立。显式参数（不接 argparse Namespace）：run.py 与 run_multi.py
    共享构造，入口契约静态可见，两入口 parser 无需同步 flag。
    """
    from pmbot.book_sampler import BookSampler
    from pmbot.clob_executor import ClobExecutor, SimExecutor
    from pmbot.main_loop import TradingLoop
    from pmbot.market_discovery import MarketDiscovery
    from pmbot.spot_ticker import SpotTickerThread
    from pmbot.state import StateStore, TradeState
    from pmbot.strategy import create_strategy, strategy_class
    from pmbot.user_stream import UserStream

    data_dir = paths.data_dir
    # 代理：兜底本机默认值并写回环境（理由见 resolve_proxy）
    proxy = resolve_proxy()

    # Binance 实时价线程提前创建：momentum 策略用它做穿越检测（WS ~1s 推送，
    # 比每 tick REST 快；更早发现穿越 → 更可能抓做市商未调价的便宜档）。
    # 直连墙内不稳（实测超时），与 BookSampler 一致走代理。
    ticker = SpotTickerThread(symbol=symbol, proxy=proxy)
    strat_kwargs = {}
    # 依赖注入按策略声明（needs_fetch_price）：新策略声明即可，run 不按策略名硬编码
    strat_cls = strategy_class(cfg.strategy)
    if getattr(strat_cls, "needs_fetch_price", False):
        strat_kwargs["fetch_price"] = ticker.latest_price
    strategy = create_strategy(
        cfg.strategy,
        symbol=symbol,
        log_dir=data_dir,
        config=cfg.to_strategy_config(),
        **strat_kwargs,
    )
    discovery = MarketDiscovery(interval=cfg.market_interval)
    executor = SimExecutor() if dry_run else ClobExecutor()

    # Polymarket WS 市场频道（REST book 兜底，常态 2s 轮询——WS 数据流在代理下
    # 不稳定（活跃连接 RST，实测 30s 诊断），REST 兜底是数据新鲜度主保障：
    # book_age ≤2s；WS 能连上时盘口近实时，断时兜底即时接管。
    sampler = BookSampler(executor.fetch_book, interval=2.0, proxy=proxy,
                          book_path=f"{data_dir}/book.json")
    executor.attach_sampler(sampler)
    sampler.start()

    # 认证 WS（订单/成交推送）：仅实盘使用——dry-run 不触碰真实凭证
    user_stream = UserStream(executor.api_auth() if not dry_run else None, proxy=proxy)
    user_stream.start()

    # Binance 实时价线程
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
        dry_run=dry_run,
        poll_sec=poll_sec,
        user_stream=user_stream,
        ticker=ticker,
        trades_path=paths.trades,
        status_path=paths.status,
        control_path=f"{data_dir}/control.json",
    )
    return LoopBundle(loop=loop, state=state, paths=paths, symbol=symbol,
                      ticker=ticker, sampler=sampler, user_stream=user_stream)


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

    _setup_logging(Path(__file__).resolve().parents[2] / "logs",
                   (args.symbol or "pmbot"))

    # 依赖集中导入（函数内：入口模块冷启动不加载重型依赖链）
    from pmbot.config import load_config
    from pmbot.paths import paths_for
    from pmbot.single_instance import run_with_guard

    cfg = load_config(args.config)
    symbol = args.symbol or cfg.symbols[0]
    paths = paths_for(not args.dry_run, args.data_dir)
    data_dir = paths.data_dir

    # 代理从环境读取并写回（墙内访问 Polymarket 需代理；记录在启动横幅）。
    proxy = resolve_proxy()

    # 实盘前置自检：不通过则拒绝启动（真钱守门；干跑发现不了的问题在此拦下）
    if not args.dry_run:
        from pmbot.clob_executor import ClobExecutor
        from pmbot.preflight import live_advisories, live_preflight

        problems = live_preflight(cfg, ClobExecutor())
        if problems:
            for p in problems:
                logging.error("实盘自检未通过：%s", p)
            logging.error("拒绝以实盘模式启动（修正后重试；先干跑请用 start_multi.bat）")
            return 3
        for note in live_advisories(cfg):
            logging.warning("实盘提醒：%s", note)
        logging.info("实盘自检通过：凭证 / 余额 / 授权 均正常")

    bundle = build_loop(cfg, symbol=symbol, paths=paths,
                        dry_run=args.dry_run, poll_sec=args.poll)
    loop = bundle.loop
    mode = paths.mode

    # 启动横幅：一键复盘需要的全部上下文（时间/模式/标的/数据目录/策略/网络）
    logging.info("=" * 56)
    logging.info("pmbot 启动")
    logging.info("模式=%s 标的=%s 数据目录=%s", mode, symbol, data_dir)
    logging.info("策略=%s 窗口=%s 轮询=%ds 每注=%s USDC",
                 cfg.strategy, cfg.market_interval, args.poll, cfg.amount_per_trade)
    logging.info("代理=%s（None=直连）", proxy)
    logging.info("WS: BookSampler=Polymarket盘口 SpotTicker=Binance实时价 断线自动重连（退避30-300s）")
    logging.info("=" * 56)

    def _run() -> None:
        from pmbot.control import read_control

        read_control(f"{data_dir}/control.json")  # 丢弃面板残留指令（如旧实例停机前的 stop），避免启动即停机
        try:
            if args.once:
                loop.tick(now_ms=int(time.time() * 1000))
            else:
                loop.run_forever()
        finally:
            bundle.shutdown_all()  # 停本标的全部 WS 线程（生命周期收口于 LoopBundle）

    # 终端关闭（CTRL_CLOSE）在 Windows 触发 SIGTERM → 优雅停机（同 Ctrl-C）
    import signal as _signal

    def _on_sigterm(signum: int, frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    try:
        _signal.signal(_signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        pass

    # 单实例守护：杀旧 run 实例防多开互踩状态文件；退出自动注销
    try:
        run_with_guard("run", _run, pid_file=paths.pid_file)
    except Exception:
        logging.exception("主循环异常退出（崩溃兜底，完整 traceback 已记录）")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
