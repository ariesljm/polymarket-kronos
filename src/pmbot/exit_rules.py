"""退出规则纯函数：止盈/止损价（单一事实源）。

engine（在线决策）、monitor（面板展示）、backtest_sim（离线回测）三处共用，
避免公式漂移。

统一口径：
- 止盈价 = tp_price（绝对价，如 0.95 = 价格到 0.95 止盈，接近持有到结算，
  利用 redeem 无费；0 < tp_price < 1）
- 止损价 = max(入场价 × (1 − sl_pct), floor)（sl_pct ≤ 0 表示关闭止损 → 0.0）
"""

from __future__ import annotations


def position_exit_levels(
    entry_price: float,
    tp_price: float,
    sl_pct: float,
    floor: float = 0.001,
) -> tuple[float, float]:
    """计算 (止盈价, 止损价)。

    - tp_price: 绝对止盈价（如 0.95 = 价格到 0.95 止盈，接近持有到结算；
      结算 redeem 无费，低带持仓持有到接近结算优于早止盈，见
      docs/reports/momentum_strategy.md）；必须在 (0,1)
    - sl_pct: 止损百分比（如 0.20 = −20%）；≤ 0 表示关闭止损 → 止损价 0.0
    - floor: 止损价下限保护（防价格算成 ≤ 0）
    """
    tp = tp_price
    sl = max(entry_price * (1 - sl_pct), floor) if sl_pct > 0 else 0.0
    return tp, sl
