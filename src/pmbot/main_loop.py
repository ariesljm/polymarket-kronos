"""主循环：15m 窗口编排。

每个轮询 tick：结算 → 熔断/暂停检查 → 窗口切换 → 市场发现 →
信号（窗口首次）→ 挂单成交检查 → 决策引擎 → 执行 → 状态落盘。
任何外部调用失败都跳过本 tick，不崩溃。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from pmbot.clob_executor import OrderPlacer
from pmbot.config import Config
from pmbot.constants import window_end_sec, window_start_sec
from pmbot.control import read_control
from pmbot.engine import decide
from pmbot.market_discovery import MarketDiscovery, MarketInfo
from pmbot.market_lifecycle import MarketLifecycle, Phase
from pmbot.state import StateStore, TradeState
from pmbot.strategy import Strategy
from pmbot.types import (
    Action,
    ActionType,
    Direction,
    MarketView,
    PendingOrder,
    Position,
    StateView,
    token_for,
)
from pmbot.user_stream import UserStream

logger = logging.getLogger(__name__)

# 钱包余额刷新间隔（秒）：余额变化慢，低频查询避免无谓 RPC
BALANCE_REFRESH_SEC = 30


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
        settle_timeout_sec: int = 1800,
        poll_sec: int = 10,
        user_stream: UserStream | None = None,
    ):
        self.config = config
        self.symbol = symbol
        self.strategy = strategy
        self.discovery = discovery
        self.executor = executor
        self.state = state
        self.trades_path = Path(trades_path)
        self.status_path = Path(status_path)
        self.control_path = Path(control_path)
        self._store = StateStore(status_path, trades_path)
        self.dry_run = dry_run
        self.settle_timeout_sec = settle_timeout_sec
        self.poll_sec = poll_sec
        self.user_stream = user_stream
        self._lifecycle: MarketLifecycle | None = None
        self._last_balance_sec = 0

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
        self._refresh_balance(now_sec)
        day = datetime.fromtimestamp(now_sec, tz=timezone.utc).strftime("%Y-%m-%d")
        st.roll_day(day)
        self._drain_user_events()

        # 1. 窗口结束后结算持仓（gamma 结算有延迟；引擎级兜底，跨生命周期）
        if st.position is not None and now_sec >= st.position.window_start + self.discovery.step_ms // 1000:
            self._settle(now_sec)

        # 2. 熔断/暂停：不交易（生命周期暂停推进，恢复后继续）
        if self._check_circuit_breaker(now_sec):
            return

        # 3. 窗口切换编排：旧生命周期收尾，新生命周期启动
        step = self.discovery.step_ms // 1000
        new_window = window_start_sec(now_ms // 1000, step)
        if st.window_start != new_window:
            # 跨窗口遗留挂单先撤单（基于 state 判断，兼容预置旧挂单场景）
            if st.pending_order is not None:
                self.executor.cancel(st.pending_order.order_id)
                logger.info("跨窗口撤单：%s", st.pending_order.order_id)
            st.roll_window(new_window)
            if self._lifecycle is not None:
                self._lifecycle.stop(now_sec)  # 旧生命周期 → DONE
            self._lifecycle = MarketLifecycle(deps=self, window_start=new_window, now_sec=now_sec)
        elif self._lifecycle is None:
            self._lifecycle = MarketLifecycle(deps=self, window_start=new_window, now_sec=now_sec)

        # 4. 市场发现（引擎级：市场不存在时窗口逻辑整体跳过）
        market = self.discovery.find_current_window(self.symbol, now_ms)
        if market is None:
            logger.info("窗口 %d 市场不可交易/未找到，跳过", new_window)
            self.save_status()
            return

        # 4.5 盘口价格（面板展示；查询失败不中断）
        self._subscribe_sampler(market)
        try:
            st.market_prices = {
                "up_ask": self.executor.best_ask(market.yes_token_id),
                "up_bid": self.executor.best_bid(market.yes_token_id),
                "down_ask": self.executor.best_ask(market.no_token_id),
                "down_bid": self.executor.best_bid(market.no_token_id),
            }
        except Exception:
            logger.exception("盘口查询失败，跳过写入")
            st.market_prices = None

        # 5. 生命周期推进：INIT（信号）→ RUNNING（成交/决策/执行）
        lc = self._lifecycle
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
            reset = getattr(self.strategy, "reset_runtime_data", None)
            if reset:
                reset()
            if self.trades_path.is_file():
                self.trades_path.unlink()
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
            self.executor.cancel(st.pending_order.order_id)
            logger.info("停机撤单：%s", st.pending_order.order_id)
            st.pending_order = None
        if self._lifecycle is not None:
            self._lifecycle.stop(now_sec)
            self._lifecycle = None
        if st.position is not None and now_sec >= st.position.window_start + self.discovery.step_ms // 1000:
            self._settle(now_sec)
        self.save_status()

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
        if st.consecutive_losses >= self.config.max_consecutive_losses:
            st.paused = True
            st.pause_reason = f"连亏 {st.consecutive_losses} 笔（上限 {self.config.max_consecutive_losses}）"
            logger.warning("熔断触发：连亏 %d 笔，已暂停。恢复：编辑 %s 将 paused 改为 false",
                           st.consecutive_losses, self.status_path)
            self.save_status()
            return True
        if st.daily_loss >= self.config.max_daily_loss:
            st.paused = True
            st.pause_reason = f"日亏 {st.daily_loss:.2f} USDC（上限 {self.config.max_daily_loss}）"
            logger.warning("熔断触发：当日亏损 %.2f USDC，已暂停。恢复：编辑 %s 将 paused 改为 false",
                           st.daily_loss, self.status_path)
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
        best_ask = self.executor.best_ask(target_token)
        best_bid = None
        if st.position is not None:
            # 持仓 token 盘口在结算前始终有效：跨窗口持仓（结算等待期）同样执行止盈/止损
            pos_token = token_for(market, st.position.direction)
            best_bid = self.executor.best_bid(pos_token)
        return MarketView(
            remaining_sec=self._window_end_sec(now_sec) - now_sec,
            best_ask=best_ask,
            best_bid=best_bid,
            position=st.position,
            pending_order=st.pending_order,
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
                self._settle(self._now_sec())

    def _now_sec(self) -> int:
        import time
        return int(time.time())

    def refresh_pending(self, market: MarketInfo, now_sec: int) -> None:
        st = self.state
        pending = st.pending_order
        if pending is None:
            return
        is_simulated = str(pending.order_id).startswith("dry-run-")
        if is_simulated:
            # dry-run 模拟成交：真实盘口 ask ≤ 限价即视为成交（与真实限价单吃单一致）
            token = token_for(market, pending.direction)
            ask = self.executor.best_ask(token)
            if ask is not None and ask <= float(pending.price):
                self._fill_pending(now_sec, entry_price=ask)
            return
        try:
            if self.user_stream is not None and self.user_stream.connected:
                # WS 推送已覆盖成交/撤单确认（事件驱动），跳过 REST 轮询
                return
            order = self.executor.get_order(pending.order_id)
        except Exception:
            logger.exception("查询挂单失败")
            return
        if isinstance(order, dict) and order.get("status") == "filled":
            self._fill_pending(now_sec)

    def _fill_pending(self, now_sec: int, entry_price: float | None = None) -> None:
        st = self.state
        pending = st.pending_order
        entry = entry_price if entry_price is not None else float(pending.price)
        st.position = Position(
            direction=pending.direction,
            entry_price=entry,
            size=float(pending.size),
            entered_remaining_sec=self._window_end_sec(now_sec) - now_sec,
            window_start=st.window_start,
            entry_balance=self._query_balance() if not self.dry_run else None,
        )
        st.pending_order = None
        logger.info("挂单成交：%s @ %.2f，建立持仓", pending.direction, entry)

    def _refresh_balance(self, now_sec: int) -> None:
        """定时刷新钱包余额快照（面板展示）；查询失败保留旧值。"""
        if now_sec - self._last_balance_sec < BALANCE_REFRESH_SEC:
            return
        self._last_balance_sec = now_sec
        b = self._query_balance()
        if b is not None and b != self.state.balance:
            self.state.balance = b
            self.save_status()

    def _query_balance(self) -> float | None:
        """查询钱包余额；无凭证/网络失败返回 None（静默，不刷屏）。"""
        try:
            return self.executor.collateral_balance()
        except Exception:
            return None

    def _subscribe_sampler(self, market: MarketInfo) -> None:
        """BookSampler 订阅当前窗口 token（高频采样线程）。"""
        sampler = self.executor.sampler
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
        ask = self.executor.best_ask(token)
        if ask is None:
            logger.warning("市价买入跳过：盘口无报价 %s", token[:16])
            return  # 不置 flag，下 tick 重试
        # 目标份额 = 金额/盘口价（真实成交以 API 返回为准，本值仅作回退）
        target_size = action.amount / ask
        filled = self.executor.market_buy(token, action.amount)
        if filled is None:
            logger.warning("市价买入失败（无成交数据）：%s，下 tick 重试", token[:16])
            return  # 不置 flag、不建仓：防假持仓
        entry = filled.get("avg_price") or ask
        size = filled.get("filled_size") or target_size
        st.position = Position(
            direction=action.direction,
            entry_price=entry,
            size=size,
            entered_remaining_sec=self._window_end_sec(now_sec) - now_sec,
            window_start=st.window_start,
            entry_balance=self._query_balance() if not self.dry_run else None,
        )
        st.window_bet_placed = True
        # 立即落盘：防下单后崩溃导致同窗口重启重复下单
        self.save_status()
        src = "API" if filled.get("avg_price") and filled.get("filled_size") else "盘口估算"
        logger.info(
            "市价买入：%s %s %.4f 股 @ %.3f (成本 %.2f USDC, 数据源 %s)",
            token[:16], action.direction.value, size, entry, size * entry, src,
        )

    def _exec_cancel(self, action: Action) -> None:
        st = self.state
        if st.pending_order:
            self.executor.cancel(st.pending_order.order_id)
            logger.info("撤单：%s", st.pending_order.order_id)
            st.pending_order = None

    def _exec_sell(self, action: Action, market: MarketInfo) -> None:
        st = self.state
        pos = st.position
        if pos is None:
            return
        token = token_for(market, pos.direction)
        exit_price = self.executor.market_sell(token, pos.size)
        if exit_price is None:
            exit_price = self.executor.best_bid(token) or 0.0
        self._close_position(pos, exit_price, action.reason or "sell")

    def _exec_pause(self, action: Action) -> None:
        st = self.state
        st.paused = True
        st.pause_reason = {
            "consecutive_losses": "连亏熔断",
            "daily_loss": "日亏熔断",
        }.get(action.reason or "", "熔断暂停")
        logger.warning("收到暂停指令（%s）", st.pause_reason)

    def _settle(self, now_sec: int) -> None:
        st = self.state
        pos = st.position
        market = self.discovery.find_window(self.symbol, pos.window_start, require_tradable=False)
        if market is None:
            # 清除负缓存：首次查询可能网络瞬时失败，让下一 tick 重新查询（防结算死循环）
            invalidate = getattr(self.discovery, "invalidate", None)
            if invalidate:
                invalidate(self.symbol, pos.window_start, require_tradable=False)
            if now_sec > pos.window_start + self.discovery.step_ms // 1000 + self.settle_timeout_sec:
                # 超时仍不可达：丢弃持仓跟踪（Polymarket 结算自动兑付，不丢资金）
                logger.warning("窗口 %d 结算市场持续不可达（超过 %ds），丢弃持仓跟踪",
                               pos.window_start, self.settle_timeout_sec)
                st.position = None
                self.save_status()
                return
            logger.info("窗口 %d 结算价未就绪，等待下一 tick", pos.window_start)
            return
        settle_price = (
            market.outcome_prices[0]
            if pos.direction is Direction.UP
            else market.outcome_prices[1]
        )
        # gamma 未结算时价格在 (0,1) 中间；超时后按当前价兜底
        if 0 < settle_price < 1 and now_sec <= pos.window_start + self.discovery.step_ms // 1000 + self.settle_timeout_sec:
            logger.info("窗口 %d 尚未结算（价格 %.3f），等待", pos.window_start, settle_price)
            return
        exit_balance = self._query_balance()
        actual = (
            exit_balance - pos.entry_balance
            if (exit_balance is not None and pos.entry_balance is not None)
            else None
        )
        pnl = st.close_position(settle_price, actual_pnl=actual)
        self._store.log_trade(
            st,
            window_start=pos.window_start,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=settle_price,
            size=pos.size,
            pnl=pnl,
            reason="settle",
        )
        logger.info("窗口 %d 结算：%s @ %.2f，盈亏 %.4f%s",
                    pos.window_start, pos.direction.value, settle_price, pnl,
                    "（余额差值）" if actual is not None else "（理论价差）")

    def _close_position(self, pos: Position, exit_price: float, reason: str) -> float:
        """平仓共用路径（卖出/结算）：余额差 PnL 优先（含滑点/手续费），

        查询失败回退理论价差；调用 state.close_position 兑现并更新熔断计数，
        最后记账 + 日志。两条路径只提供 exit 价来源与 reason。

        dry-run 不真实下单、钱包余额不变，余额差恒为 0——一律用理论价差。
        """
        st = self.state
        actual = None
        if not self.dry_run:
            exit_balance = self._query_balance()
            actual = (
                exit_balance - pos.entry_balance
                if (exit_balance is not None and pos.entry_balance is not None)
                else None
            )
        pnl = st.close_position(exit_price, actual_pnl=actual)
        self._store.log_trade(
            st,
            window_start=pos.window_start,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size=pos.size,
            pnl=pnl,
            reason=reason,
        )
        logger.info("平仓（%s）：%s @ %.3f，盈亏 %.4f%s",
                    reason, pos.direction.value, exit_price, pnl,
                    "（余额差值）" if actual is not None else "（理论价差）")
        return pnl

    def save_status(self) -> None:
        self._store.save(self.state)





