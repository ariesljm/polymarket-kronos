"""结算状态机（Settler）：持仓窗口结束后的结算等待/兑付深模块。

背景：结算规则曾散在主循环三处（tick 前置分支、shutdown 同表达式复制、
build_view 的"结算等待期不报价"注释特判），状态靠每 tick 重算分支表达，
无名字、无可观察状态；12+ 回归测试全部走完整 tick 集成。

本模块把结算收敛为一个深模块（仿 WalletReconciler 先例）：
- 引擎 tick / shutdown 各一行调用 `should_run` + `settle`；
- 外部依赖经窄接口注入（市场查询 + 兑付查询），回调注入（平仓/丢弃跟踪）；
- 状态机 PRICE_WAIT → REDEEM_WAIT → DONE/ABANDONED 可观察；
- 结算超时推导（按窗口步长自适应）也收敛于此。

结算规则（四路分支，与主循环历史实现逐字等价，仅封装+状态化）：
1. 市场不可达：invalidate 防负缓存，超时（settle_timeout）丢弃持仓跟踪；
2. 结算归零（输）：直接按成本记账清仓——输的仓永远不会有 REDEEM 记录，
   若与赢的仓一样等兑付确认会永久占用持仓（回归事故）；
3. 中间价（未结算）：等待，超时后按当前价兜底记账（不能永久占用持仓）；
4. 价格就绪（赢）：等 REDEEM 真实兑付确认（usdcSize 到账），确认后记账。
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Callable, Protocol

from pmbot.types import Direction, Position

logger = logging.getLogger(__name__)


class SettlePhase(Enum):
    """结算状态机的可观察状态。"""

    IDLE = "idle"            # 无待结算持仓
    PRICE_WAIT = "price_wait"    # 等待结算价就绪（市场不可达/中间价）
    REDEEM_WAIT = "redeem_wait"  # 结算价就绪（赢），等待真实兑付 REDEEM 记录
    DONE = "done"            # 已结算记账（或超时丢弃跟踪）


class SettlementSource(Protocol):
    """结算所需市场查询的窄接口（TradingLoop/MarketDiscovery 满足）。"""

    def find_window(self, symbol: str, window_start: int,
                    require_tradable: bool = True): ...
    def invalidate(self, symbol: str, window_start: int,
                   require_tradable: bool = True) -> None: ...


class Settler:
    """持仓窗口结束后的结算状态机：tick 一行调用，规则独立可测。"""

    def __init__(
        self,
        *,
        symbol: str,
        source: SettlementSource,
        settle_proceeds: Callable[[str], float | None],
        on_settle: Callable[[Position, float, float | None], float],
        on_abandon: Callable[[], None],
        step_sec: int,
        settle_timeout_sec: int | None = None,
        dry_run: bool = True,
    ):
        self.symbol = symbol
        self.source = source
        self._settle_proceeds = settle_proceeds
        self._on_settle = on_settle
        self._on_abandon = on_abandon
        self.step_sec = step_sec
        # 结算超时按窗口步长自适应：5m 窗口 10 分钟，15m 窗口 30 分钟。
        # 固定 1800s 对 5m 市场过长（gamma 结算几分钟即出结果）——残留持仓
        # 最多卡 6 个窗口不交易，不符合常理；下限 300s 防小步长配置过激。
        self.settle_timeout_sec = (
            settle_timeout_sec if settle_timeout_sec is not None
            else self.default_timeout_sec(step_sec)
        )
        self.dry_run = dry_run
        self.phase = SettlePhase.IDLE

    @staticmethod
    def default_timeout_sec(step_sec: int) -> int:
        """默认结算超时：窗口步长自适应（2×步长，下限 300s）。"""
        return max(2 * step_sec, 300)

    def should_run(self, now_sec: int, position: Position | None) -> bool:
        """持仓窗口是否已结束（需要进入结算流程）。"""
        return position is not None and now_sec >= position.window_start + self.step_sec

    def settle(self, now_sec: int, position: Position) -> None:
        """推进结算状态机（引擎每 tick 调用一次；无持仓时不应调用）。"""
        pos = position
        market = self.source.find_window(self.symbol, pos.window_start, require_tradable=False)
        if market is None:
            # 清除负缓存：首次查询可能网络瞬时失败，让下一 tick 重新查询（防结算死循环）
            invalidate = getattr(self.source, "invalidate", None)
            if invalidate:
                invalidate(self.symbol, pos.window_start, require_tradable=False)
            timeout_at = pos.window_start + self.step_sec + self.settle_timeout_sec
            if now_sec > timeout_at:
                # 超时仍不可达：丢弃持仓跟踪（Polymarket 结算自动兑付，不丢资金）
                logger.warning("窗口 %d 结算市场持续不可达（超过 %ds），丢弃持仓跟踪",
                               pos.window_start, self.settle_timeout_sec)
                self.phase = SettlePhase.DONE
                self._on_abandon()
                return
            self.phase = SettlePhase.PRICE_WAIT
            logger.info("窗口 %d 结算价未就绪，等待下一 tick", pos.window_start)
            return
        settle_price = (
            market.outcome_prices[0]
            if pos.direction is Direction.UP
            else market.outcome_prices[1]
        )
        timeout_at = pos.window_start + self.step_sec + self.settle_timeout_sec
        # 结算归零（输）：无兑付，永远不会有 REDEEM 记录 → 直接按成本记账清仓。
        # 若不区分输赢一律"等 REDEEM"，输的仓会永久占用持仓 → 新窗口无法开仓（回归事故）。
        if settle_price <= 0.01:
            logger.info("窗口 %d 结算归零（价格 %.3f），按成本记账", pos.window_start, settle_price)
            self.phase = SettlePhase.DONE
            self._on_settle(pos, settle_price, None)
            return
        if settle_price < 0.99:
            # gamma 未结算时价格在 (0,1) 中间；超时后按当前价兜底记账（不查 REDEEM，
            # 结算迟迟不确认的仓不能永久占用持仓）。
            if now_sec <= timeout_at:
                self.phase = SettlePhase.PRICE_WAIT
                logger.info("窗口 %d 尚未结算（价格 %.3f），等待", pos.window_start, settle_price)
                return
            logger.warning("窗口 %d 结算价超时未就绪（价格 %.3f），按当前价兜底记账",
                           pos.window_start, settle_price)
            self.phase = SettlePhase.DONE
            self._on_settle(pos, settle_price, None)
            return
        # 结算价已就绪（赢）：优先真实兑付记账（data-api /activity REDEEM 的 usdcSize
        # = 实际到账含本金）。未确认（结算延迟/网络）→ 不记账不回退，下一 tick 重试；
        # 持续未确认超时 → 按结算价兜底记账（兑付记录长期不出现，持仓不能永久占用
        # → 否则新窗口无法开仓，回归事故与「输的仓死等 REDEEM」同构）。
        if not self.dry_run:
            proceeds = self._settle_proceeds(market.condition_id)
            if proceeds is None:
                if now_sec <= timeout_at:
                    self.phase = SettlePhase.REDEEM_WAIT
                    logger.info(
                        "窗口 %d 结算价已就绪（%.3f）但真实兑付未确认，等待 REDEEM 记录（下一 tick 重试）",
                        pos.window_start, settle_price)
                    return
                logger.warning(
                    "窗口 %d 结算价已就绪（%.3f）但真实兑付持续未确认（超过 %ds），按结算价兜底记账",
                    pos.window_start, settle_price, self.settle_timeout_sec)
                proceeds = None  # 兜底：走理论价差记账（依旧记入交易历史/熔断计数）
        else:
            proceeds = None
        self.phase = SettlePhase.DONE
        self._on_settle(pos, settle_price, proceeds)
