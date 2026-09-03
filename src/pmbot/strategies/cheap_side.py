"""cheap_side 策略：买「ask 最低且 ≤ 门槛」的方向（市场定价滞后低估带）。

依据：盘口审计（docs/reports/market_audit.md，498 候选）显示 0.2-0.3 档
Down 持续被低估（定价 ~0.22、实际胜率 52%，t=+6.59）；0.6+ 追高档显著负。
机制：窗口初段市场定价滞后于真实概率，低价档收敛慢——买入贴真的便宜价。

与模型无关：不跑 Kronos，方向选择 = 比价（up_ask vs down_ask 取低者），
要求 ≤ entry_price_threshold（默认 0.35）。选中后输出强信号复用餐用阈值
链路（p_up=0.9 买 up / 0.1 买 down），engine/max_entry_price 等参数不变。

实验性质：样本仅 22h 录音，需与 kronos 对照积累复验（验证协议：2 周 ≥2000 窗口）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from pmbot.config import StrategyConfig
from pmbot.strategy import Strategy, register
from pmbot.types import Direction, Signal, SignalContext


@register("cheap_side")
class CheapSideStrategy(Strategy):
    def __init__(
        self,
        *,
        strategy_config: StrategyConfig | None = None,
        symbol: str = "BTC",
        log_dir: str | Path = "data",  # 兼容工厂统一签名（无 K 线/预测记录，忽略）
        best_ask_fn: Callable[[Direction], float | None] | None = None,
    ):
        """best_ask_fn: 盘口询价回调（测试注入 fake；运行态由信号上下文注入）。"""
        sc = strategy_config or StrategyConfig()
        self.threshold = sc.entry_price_threshold
        self.symbol = symbol
        self._best_ask_fn = best_ask_fn

    def generate_signal(self, context: SignalContext | None = None) -> Signal:
        ctx = context or {}
        ask_fn = self._best_ask_fn or ctx.get("best_ask")
        if ask_fn is None:
            return Signal(direction=Direction.SKIP, p_up=0.5)  # 无盘口查询能力 → 不入场
        up_ask = ask_fn(Direction.UP)
        down_ask = ask_fn(Direction.DOWN)
        # 单边缺失：不比较，只看可用方向是否满足门槛
        if up_ask is None and down_ask is None:
            return Signal(direction=Direction.SKIP, p_up=0.5)
        picks = [(Direction.UP, up_ask), (Direction.DOWN, down_ask)]
        picks = [(d, a) for d, a in picks if a is not None]
        direction, ask = min(picks, key=lambda p: p[1])
        if ask > self.threshold:
            # 最低价方向都贵过门槛：该窗口无廉价方向，不入场（等待下一窗口）
            return Signal(direction=Direction.SKIP, p_up=0.5)
        # 强信号复用 engine 阈值链路：p_up=0.9 → 买 up；0.1 → 买 down（1−0.9=0.1）
        p_up = 0.90 if direction is Direction.UP else 0.10
        return Signal(direction=direction, p_up=p_up)