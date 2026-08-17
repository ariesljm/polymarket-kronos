"""主循环：15m 窗口编排。

每个轮询 tick：结算 → 熔断/暂停检查 → 窗口切换 → 市场发现 →
信号（窗口首次）→ 挂单成交检查 → 决策引擎 → 执行 → 状态落盘。
任何外部调用失败都跳过本 tick，不崩溃。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from pmbot.clob_executor import MarketBook, OrderPlacer, TradeExecutor, WalletView
from pmbot.config import Config
from pmbot.constants import window_end_sec, window_start_sec
from pmbot.control import read_control
from pmbot.engine import decide, circuit_breaker
from pmbot.market_discovery import MarketDiscovery, MarketInfo
from pmbot.market_lifecycle import MarketLifecycle, Phase
from pmbot.settler import Settler
from pmbot.state import StateStore, TradeState
from pmbot.strategy import Strategy
from pmbot.trade_history import TradeHistorySource
from pmbot.types import (
    Action,
    ActionType,
    Direction,
    MarketView,
    PendingOrder,
    Position,
    StateView,
    rebuilt_position,
    token_for,
)
from pmbot.user_stream import UserStream
from pmbot.wallet import WalletReconciler

logger = logging.getLogger(__name__)


class TradingLoop:
    def __init__(
        self,
        config: Config,
        symbol: str,
        strategy: Strategy,
        discovery: MarketDiscovery,
        executor: OrderPlacer,
        state: TradeState,
        trades_path: str | Path = "data/trades.csv",
        status_path: str | Path = "data/status.json",
        control_path: str | Path = "data/control.json",
        dry_run: bool = True,
        settle_timeout_sec: int | None = None,
        poll_sec: int = 10,
        user_stream: UserStream | None = None,
    ):
        self.config = config
        self.symbol = symbol
        self.strategy = strategy
        self.discovery = discovery
        # 执行器组合面按角色拆窄使用：盘口 / 下单 / 钱包（消费面=需要面）
        self.book: MarketBook = executor
        self.trade: TradeExecutor = executor
        self.wallet: WalletView = executor
        self.executor = executor  # 组合面别名：LifecycleDeps（CancelExecutor 面）与测试兼容
        self.state = state
        self.trades_path = Path(trades_path)
        self.status_path = Path(status_path)
        self.control_path = Path(control_path)
        self._store = StateStore(status_path, trades_path)
        self.dry_run = dry_run
        # 结算超时按窗口步长自适应：5m 窗口 10 分钟，15m 窗口 30 分钟。
        # 固定 1800s 对 5m 市场过长（gamma 结算几分钟即出结果）——残留持仓
        # 最多卡 6 个窗口不交易，不符合常理；下限 300s 防小步长配置过激。
        if settle_timeout_sec is None:
            settle_timeout_sec = Settler.default_timeout_sec(self.step_sec)
        self.settle_timeout_sec = settle_timeout_sec
        self.poll_sec = poll_sec
        self.user_stream = user_stream
        # 结算状态机（持仓窗口结束后的结算等待/兑付）：深模块，规则独立可测。
        # 窄接口注入：市场查询（find_window/invalidate）+ 兑付查询 + 平仓/丢弃回调。
        self.settler = Settler(
            symbol=symbol,
            source=discovery,
            settle_proceeds=self.trade.settle_proceeds,
            on_settle=lambda pos, exit_price, proceeds: self._close_position(
                pos, exit_price, "settle", proceeds=proceeds
            ),
            on_abandon=self._abandon_position,
            step_sec=self.step_sec,
            settle_timeout_sec=settle_timeout_sec,
            dry_run=dry_run,
        )
        # 钱包核对（余额/实时持仓/幽灵持仓）：外部世界同步关注点，独立可测
        self.wallet_sync = WalletReconciler(
            self.wallet, self.save_status, dry_run=dry_run,
            step_sec=self.step_sec,  # 幽灵判定/接管计时需知持仓窗口何时结束
        )
        # 交易历史同步（live only）：data-api 真实流水增量缓存 api_trades.csv，
        # 后台线程不阻塞 tick；dry-run 无真实流水不启用
        self.history_sync = None
        if not dry_run and isinstance(executor, TradeHistorySource):
            from pmbot.trade_history import TradeHistorySyncer

            self.history_sync = TradeHistorySyncer(
                Path(status_path).parent / "api_trades.csv",
                executor.fetch_trade_page,
                executor.fetch_redeem_page,
            )
        self._lifecycle: MarketLifecycle | None = None
        self._skip_window_until = 0  # 启动跳过窗口终点（秒；0=不跳过，run_forever 启动时设置）

    # ---- 对外入口 ----

    def run_forever(self) -> None:
        """阻塞运行：对齐窗口边界轮询；SIGINT/SIGTERM 优雅停机。"""
        import signal
        import time

        self._shutdown = False

        def _on_signal(signum, frame):
            logger.info("收到信号 %s，优雅停机中...", signum)
            self._shutdown = True

        try:
            signal.signal(signal.SIGINT, _on_signal)
            signal.signal(signal.SIGTERM, _on_signal)
        except (ValueError, OSError):
            pass  # 非主线程/平台不支持时退化为 KeyboardInterrupt

        logger.info("主循环启动（symbol=%s, dry_run=%s, 轮询 %ds）", self.symbol, self.dry_run, self.poll_sec)
        # 启动重置瞬态推理状态：旧进程崩溃/强杀可能残留 predicting=true 与旧计时，
        # 新进程不可能“正在推理”（推理随旧进程消亡）——面板不再显示虚假的推理计时
        if self.state.predicting or self.state.predict_start_sec is not None:
            self.state.predicting = False
            self.state.predict_start_sec = None
            self.save_status()
            logger.info("启动重置残留推理状态（predicting/predict_start_sec）")
        # 启动立即核对链上持仓（live）：意外退出（杀进程/关终端）残留的幽灵持仓
        # （本地有、链上已无）当场清除；链上真实持仓保留继续管理。
        # 不清熔断计数/交易历史——只纠正持仓视图，不洗白风控与统计。
        self.wallet_sync.startup_reconcile(int(time.time()), self.state)
        # 启动交易历史同步线程（live）：data-api 真实流水增量落盘 api_trades.csv
        if self.history_sync is not None:
            self.history_sync.start()
        # 启动跳过进行中的窗口：不推理不交易，下一窗口起点才开始（避免中途启动做半窗决策）
        step = self.discovery.step_ms // 1000
        self._skip_window_until = window_start_sec(int(time.time()), step) + step
        self.state.skip_until_sec = self._skip_window_until
        self.save_status()  # 落盘：面板显示启动等待（独立进程读 status.json）
        logger.info("跳过进行中的窗口，%s 起运行推理与交易", self._skip_window_until)
        while not self._shutdown:
            try:
                self.tick(now_ms=int(time.time() * 1000))
            except KeyboardInterrupt:
                self._shutdown = True
            except Exception:
                logger.exception("tick 异常，跳过")
            time.sleep(self.poll_sec)
        self.shutdown(now_sec=int(time.time()))
        logger.info("优雅停机完成")

    def tick(self, now_ms: int) -> None:
        """引擎 tick：控制指令 → 日界/熔断 → 窗口切换编排 → 当前生命周期推进。"""
        if self._consume_control():
            return  # stop 指令：本 tick 不再交易，循环随即优雅停机
        st = self.state
        now_sec = now_ms // 1000
        day = datetime.fromtimestamp(now_sec, tz=timezone.utc).strftime("%Y-%m-%d")
        st.roll_day(day)  # 先处理跨天（重置今日基准），再刷新余额捕获新基准
        self.wallet_sync.reconcile(now_sec, st)
        self._drain_user_events()

        # 1. 窗口结束后结算持仓（gamma 结算有延迟；引擎级兜底，跨生命周期）
        if self.settler.should_run(now_sec, st.position):
            self.settler.settle(now_sec, st.position)

        # 2. 熔断/暂停：不交易（生命周期暂停推进，恢复后继续）
        if self._check_circuit_breaker(now_sec):
            return

        # 3. 窗口切换编排：旧生命周期收尾，新生命周期启动
        step = self.discovery.step_ms // 1000
        new_window = window_start_sec(now_ms // 1000, step)
        if st.window_start != new_window:
            # 跨窗口遗留挂单先撤单（基于 state 判断，兼容预置旧挂单场景）
            if st.pending_order is not None:
                self.trade.cancel(st.pending_order.order_id)
                logger.info("跨窗口撤单：%s", st.pending_order.order_id)
            st.roll_window(new_window)
            if self._lifecycle is not None:
                self._lifecycle.stop(now_sec)  # 旧生命周期 → DONE
            if new_window < self._skip_window_until:
                # 启动跳过进行中的窗口：对齐窗口起点但不建生命周期（不推理不交易）
                self._lifecycle = None
                st.skip_until_sec = self._skip_window_until
                self.save_status()  # 落盘：面板显示启动等待
                logger.info("启动跳过进行中的窗口 %s（%s 起运行）", new_window, self._skip_window_until)
            else:
                st.skip_until_sec = None
                self._lifecycle = MarketLifecycle(deps=self, window_start=new_window, now_sec=now_sec)
        elif self._lifecycle is None:
            if new_window >= self._skip_window_until:
                st.skip_until_sec = None
                self._lifecycle = MarketLifecycle(deps=self, window_start=new_window, now_sec=now_sec)
            elif st.skip_until_sec != self._skip_window_until:
                # 重启恢复：同窗口内首次 tick（st.window_start == new_window）不建生命周期，
                # 补记跳过信息（防面板无提示、误以为启动失败而反复停止/启动）
                st.skip_until_sec = self._skip_window_until
                self.save_status()
                logger.info("启动跳过进行中的窗口 %s（%s 起运行）", new_window, self._skip_window_until)

        # 4. 市场发现（引擎级：市场不存在时窗口逻辑整体跳过）
        market = self.discovery.find_current_window(self.symbol, now_ms)
        if market is None:
            logger.info("窗口 %d 市场不可交易/未找到，跳过", new_window)
            self.save_status()
            return

        # 4.5 盘口采样器订阅（高频 WS 线程；面板盘口展示单一来源 book.json，
        # 由采样器每 1s 落盘——tick 不再写 status.market_prices（双写死工作，
        # monitor 实际读 book.json 覆盖 status 同名字段）
        self._subscribe_sampler(market)

        # 5. 生命周期推进：INIT（信号）→ RUNNING（成交/决策/执行）
        lc = self._lifecycle
        if lc is not None:
            if lc.phase is Phase.INIT:
                lc.start(now_sec, market)
            lc.tick(now_sec, market)

    def _consume_control(self) -> bool:
        """消费面板控制指令（resume/reset/stop）；返回 True 表示本 tick 应提前结束。"""
        cmd = read_control(self.control_path)
        if cmd is None:
            return False
        st = self.state
        if cmd == "resume":
            st.paused = False
            st.was_paused = False
            st.consecutive_losses = 0
            st.daily_loss = 0.0
            st.pause_reason = None
            self.save_status()
            logger.info("控制指令：恢复运行（熔断计数已清零）")
            return False
        if cmd == "reset":
            if not self.dry_run:
                # 实盘禁止清除：抹掉交易历史/统计 + 丢弃真实持仓跟踪
                logger.warning("控制指令 reset：实盘模式拒绝清除数据（保护交易历史与持仓管理）")
                return False
            if st.position is not None:
                # 丢弃持仓跟踪（Polymarket Up/Down 结算自动兑付，不丢资金）：
                # 防止“停止后持仓不平仓 → 永远无法清除”的死锁
                logger.warning("控制指令 reset：存在持仓（%s %.2f 股），丢弃持仓跟踪（结算自动兑付）",
                               st.position.direction.value, st.position.size)
            # 文件清除（status/trades/K线/预测记录）与面板 reset 同一条路径
            # （control.reset_runtime）：删除清单不分裂；策略运行时数据（K线/预测
            # 记录 CSV）由同一文件清单覆盖，无需再调 strategy.reset_runtime_data
            from pmbot.control import reset_runtime

            reset_runtime(self.status_path, self.trades_path, str(self.status_path.parent),
                          symbol=self.symbol, interval=self.discovery.interval)
            self.state = TradeState(symbol=self.symbol, mode=self.state.mode)  # 保留运行模式标记
            self.save_status()
            logger.warning("控制指令：已清除数据并重建状态（symbol=%s）", self.symbol)
            return False
        if cmd == "stop":
            # 防御：--once/测试路径无 run_forever 的 _shutdown 初始化
            if not hasattr(self, "_shutdown"):
                self._shutdown = False
            self._shutdown = True
            logger.info("控制指令：停止主循环（优雅停机）")
            return True
        return False

    def shutdown(self, now_sec: int) -> None:
        """优雅停机：撤遗留挂单 → 结算已到期持仓 → 落盘。

        窗口未结束的持仓保留（status.json 持久化，重启后继续管理）。
        """
        st = self.state
        # 引擎级兜底：撤遗留挂单（不依赖 lifecycle 对象存在）
        if st.pending_order is not None:
            self.trade.cancel(st.pending_order.order_id)
            logger.info("停机撤单：%s", st.pending_order.order_id)
            st.pending_order = None
        if self._lifecycle is not None:
            self._lifecycle.stop(now_sec)
            self._lifecycle = None
        if self.settler.should_run(now_sec, st.position):
            self.settler.settle(now_sec, st.position)
        self.save_status()
        if self.history_sync is not None:
            self.history_sync.stop()

    def _check_circuit_breaker(self, now_sec: int) -> bool:
        """熔断/暂停检查；返回 True 表示本 tick 不交易。"""
        st = self.state
        if st.paused:
            st.was_paused = True
            self.save_status()
            return True
        if st.was_paused:
            # 人工恢复：用户把 paused 改回 false → 清零熔断计数后继续
            st.was_paused = False
            st.consecutive_losses = 0
            st.daily_loss = 0.0
            st.pause_reason = None
            logger.info("人工恢复：熔断计数已清零，继续运行")
        # 判定与文案单一事实源（engine.circuit_breaker：tick/decide 共用）
        trip = circuit_breaker(self.state_view(), self.config)
        if trip is not None:
            key, message = trip
            st.paused = True
            st.pause_reason = message
            logger.warning("熔断触发：%s。恢复：编辑 %s 将 paused 改为 false",
                           message, self.status_path)
            self.save_status()
            return True
        return False

    # ---- 内部 ----

    @property
    def step_sec(self) -> int:
        """窗口步长（秒），lifecycle 计算剩余时间用。"""
        return self.discovery.step_ms // 1000

    def _window_end_sec(self, now_sec: int) -> int:
        step = self.discovery.step_ms // 1000
        return window_end_sec(now_sec, step)

    def build_view(self, market: MarketInfo, now_sec: int) -> MarketView:
        st = self.state
        direction = st.signal.direction if st.signal else Direction.SKIP
        target_token = (
            market.yes_token_id if direction is Direction.UP else market.no_token_id
        )
        best_ask = self.book.best_ask(target_token)
        best_bid = None
        if st.position is not None:
            # 持仓窗口已结束（结算等待期）→ 不报价不卖出：Polymarket 后端可能已结算
            # （token 失效），卖出必失败（balance 0 → tick 异常死循环），只等结算记账。
            # 回归：18:47 窗口结束止盈卖出报错 → 每 2s tick 异常持续数分钟 → 结算超时丢跟踪。
            # 窗口结束判定与结算状态机同源（settler.should_run）。
            if not self.settler.should_run(now_sec, st.position):
                pos_token = token_for(market, st.position.direction)
                best_bid = self.book.best_bid(pos_token)
        return MarketView(
            remaining_sec=self._window_end_sec(now_sec) - now_sec,
            best_ask=best_ask,
            best_bid=best_bid,
            position=st.position,
            pending_order=st.pending_order,
            # 窗口已进行秒数（开仓延迟判断：now − 窗口起点）
            elapsed_sec=max(0, now_sec - (self._window_end_sec(now_sec) - self.discovery.step_ms // 1000)),
        )

    def state_view(self) -> StateView:
        st = self.state
        return StateView(
            consecutive_losses=st.consecutive_losses,
            daily_loss=st.daily_loss,
            window_bet_placed=st.window_bet_placed,
            paused=st.paused,
        )

    def _drain_user_events(self) -> None:
        """处理 UserStream（认证 WS）事件：订单成交/撤单实时更新。"""
        stream = self.user_stream
        if stream is None:
            return
        for etype, data in stream.drain():
            try:
                self._handle_user_event(etype, data)
            except Exception:
                logger.exception("用户事件处理失败: %s", etype)

    def _handle_user_event(self, etype: str, data: dict) -> None:
        st = self.state
        if etype == "order":
            oid = data.get("id")
            pending = st.pending_order
            if pending is None or oid != pending.order_id:
                return
            status = str(data.get("status", "")).lower()
            if status in ("filled", "matched"):
                logger.info("WS 订单成交确认：%s", oid)
                self._fill_pending(self._now_sec())
            elif status == "canceled":
                logger.info("WS 订单撤销确认：%s", oid)
                st.pending_order = None
        elif etype == "trade":
            # 外部卖出兜底（用户在网页/其他工具卖出持仓）：本地持仓记录清理。
            # 正常路径 _exec_sell 已同步平仓（position=None），此处不会触发；
            # 窗口内 outcome_prices 未结算时 _settle 会等待，窗口结束后按
            # 结算价清理本地记录（余额差 PnL 仍含真实卖出收益）。
            side = data.get("side", "").upper()
            if side == "SELL" and st.position is not None and not self.dry_run:
                logger.info("WS 卖出成交：%s", data.get("id", ""))
                self.settler.settle(self._now_sec(), st.position)

    def _now_sec(self) -> int:
        import time
        return int(time.time())

    def refresh_pending(self, market: MarketInfo, now_sec: int) -> None:
        st = self.state
        pending = st.pending_order
        if pending is None:
            return
        is_simulated = self.dry_run  # 模拟语义由引擎注入的 mode 判据承担（不再靠 order_id 前缀魔法串）
        if is_simulated:
            # dry-run 模拟成交：真实盘口 ask ≤ 限价即视为成交（与真实限价单吃单一致）
            token = token_for(market, pending.direction)
            ask = self.book.best_ask(token)
            if ask is not None and ask <= float(pending.price):
                self._fill_pending(now_sec, entry_price=ask)
            return
        try:
            if self.user_stream is not None and self.user_stream.connected:
                # WS 推送已覆盖成交/撤单确认（事件驱动），跳过 REST 轮询
                return
            order = self.trade.get_order(pending.order_id)
        except Exception:
            logger.exception("查询挂单失败")
            return
        if isinstance(order, dict) and order.get("status") == "filled":
            self._fill_pending(now_sec)

    def _fill_pending(self, now_sec: int, entry_price: float | None = None) -> None:
        st = self.state
        pending = st.pending_order
        entry = entry_price if entry_price is not None else float(pending.price)
        st.position = rebuilt_position(
            pending.direction, entry, float(pending.size), st.window_start, now_sec, self.step_sec,
        )
        st.pending_order = None
        logger.info("挂单成交：%s @ %.2f，建立持仓", pending.direction, entry)

    def _subscribe_sampler(self, market: MarketInfo) -> None:
        """BookSampler 订阅当前窗口 token（高频采样线程）。"""
        sampler = self.book.sampler
        if sampler is not None:
            sampler.subscribe(
                [market.yes_token_id, market.no_token_id],
                direction_map={market.yes_token_id: "up", market.no_token_id: "down"},
            )
        if self.user_stream is not None:
            self.user_stream.subscribe_markets([market.condition_id])

    def decide(self, view: MarketView) -> Action:
        """决策引擎调用（lifecycle 使用）。"""
        return decide(self.config, self.state_view(), view, self.state.signal)

    def execute(self, action: Action, market: MarketInfo, now_sec: int) -> None:
        """动作分派：按类型路由到对应执行方法（薄分派器）。"""
        st = self.state
        if action.type is ActionType.PLACE_MARKET:
            self._exec_place_market(action, market, now_sec)
        elif action.type is ActionType.CANCEL:
            self._exec_cancel(action)
        elif action.type is ActionType.SELL:
            self._exec_sell(action, market)
        elif action.type is ActionType.PAUSE:
            self._exec_pause(action)

    def _exec_place_market(self, action: Action, market: MarketInfo, now_sec: int) -> None:
        st = self.state
        if st.window_bet_placed:
            return
        token = token_for(market, action.direction)
        ask = self.book.best_ask(token)
        if ask is None:
            logger.warning("市价买入跳过：盘口无报价 %s", token[:16])
            return  # 不置 flag，下 tick 重试
        # 目标份额 = 金额/盘口价（真实成交以 API 返回为准，本值仅作回退）
        target_size = action.amount / ask
        filled = self.trade.market_buy(token, action.amount)
        if filled is None:
            # 下单失败，或实盘成交但 API 缺实际数据（执行器已放弃建仓追踪）：
            # 不置 flag、不建仓，下 tick 重试（防假持仓 / 重复下单）
            logger.warning("市价买入失败/无成交数据：%s，下 tick 重试", token[:16])
            return
        entry = filled.avg_price or ask
        size = filled.filled_size or target_size
        st.position = rebuilt_position(
            action.direction, entry, size, st.window_start, now_sec, self.step_sec,
        )
        st.window_bet_placed = True
        # 立即落盘：防下单后崩溃导致同窗口重启重复下单
        self.save_status()
        src = "API" if filled.avg_price and filled.filled_size else "盘口估算"
        logger.info(
            "市价买入：%s %s %.4f 股 @ %.3f (成本 %.2f USDC, 数据源 %s)",
            token[:16], action.direction.value, size, entry, size * entry, src,
        )

    def _exec_cancel(self, action: Action) -> None:
        st = self.state
        if st.pending_order:
            self.trade.cancel(st.pending_order.order_id)
            logger.info("撤单：%s", st.pending_order.order_id)
            st.pending_order = None

    def _exec_sell(self, action: Action, market: MarketInfo) -> None:
        st = self.state
        pos = st.position
        if pos is None:
            return
        token = token_for(market, pos.direction)
        try:
            fill = self.trade.market_sell(token, pos.size)
        except Exception:
            # 卖出失败（网络/已结算 token 失效）：保留持仓交给 settle/下 tick，不崩 tick
            # （回归：卖出失败曾导致每 2s tick 异常死循环 + 结算超时丢跟踪）
            logger.warning("市价卖出失败（%s %s %.4f 股），保留持仓等待下一 tick",
                           token[:16], pos.direction.value, pos.size)
            return
        if fill is None:
            logger.warning("市价卖出无成交（%s %s %.4f 股），保留持仓等待下一 tick",
                           token[:16], pos.direction.value, pos.size)
            return
        exit_price = fill.avg_price or 0.0  # 执行器已回退 best_bid；0.0 最后防御
        # 真实到账（Polymarket 成交聚合，含实际成交价/手续费）；取不到回退理论价差
        proceeds = None
        if not self.dry_run and fill.order_id:
            proceeds = self.trade.sell_proceeds(fill.order_id, token)
        self._close_position(pos, exit_price, action.reason or "sell", proceeds=proceeds)

    def _exec_pause(self, action: Action) -> None:
        st = self.state
        st.paused = True
        st.pause_reason = {
            "consecutive_losses": "连亏熔断",
            "daily_loss": "日亏熔断",
        }.get(action.reason or "", "熔断暂停")
        logger.warning("收到暂停指令（%s）", st.pause_reason)

    def _abandon_position(self) -> None:
        """结算超时丢弃持仓跟踪（Polymarket 结算自动兑付，不丢资金）。"""
        st = self.state
        st.position = None
        self.save_status()

    def _close_position(self, pos: Position, exit_price: float, reason: str,
                        proceeds: float | None = None) -> float:
        """平仓共用路径（卖出/结算）：盈亏优先用 Polymarket 真实成交。

        proceeds 为卖出订单真实到账（Polymarket 成交聚合）时，
        盈亏 = 卖出收入 − 买入成本（entry_price×size，均来自 API 实际成交）；
        无 proceeds（dry-run/聚合失败）回退理论价差；结算（settle）走理论价差。
        最后调用 state.close_position 兑现并更新熔断计数，然后记账 + 日志。
        """
        st = self.state
        pnl = None
        if proceeds is not None:
            pnl = proceeds - pos.size * pos.entry_price
        result = st.close_position(exit_price, actual_pnl=pnl)
        self._store.log_trade(
            st,
            window_start=pos.window_start,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size=pos.size,
            pnl=result,
            reason=reason,
        )
        logger.info("平仓（%s）：%s @ %.3f，盈亏 %.4f%s",
                    reason, pos.direction.value, exit_price, result,
                    "（真实兑付）" if (pnl is not None and reason == "settle")
                    else "（真实成交）" if pnl is not None else "（理论价差）")
        return result

    def save_status(self) -> None:
        self._store.save(self.state)
