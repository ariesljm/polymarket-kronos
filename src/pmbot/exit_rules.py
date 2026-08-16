"""退出规则纯函数：百分比止盈/止损价（单一事实源）。

engine（在线决策）、monitor（面板展示）、backtest_sim（离线回测）三处共用，
避免公式漂移（历史教训：回测硬编码 TP=0.30/SL=0.20 与实盘 config 漂移，
封顶 0.999 vs take_profit_max 语义分裂）。

统一口径：
- 止盈价 = min(入场价 × (1 + tp_pct), tp_max)（封顶防超 1）
- 止损价 = max(入场价 × (1 − sl_pct), floor)（sl_pct ≤ 0 表示关闭止损 → 0.0）
"""

from __future__ import annotations


def position_exit_levels(
    entry_price: float,
    tp_pct: float,
    sl_pct: float,
    tp_max: float = 0.999,
    floor: float = 0.001,
) -> tuple[float, float]:
    """按百分比规则计算 (止盈价, 止损价)。

    - tp_pct: 止盈百分比（如 0.30 = +30%）；必须 ≥ 0
    - sl_pct: 止损百分比（如 0.20 = −20%）；≤ 0 表示关闭止损 → 止损价 0.0
    - tp_max: 止盈封顶价（防超 1 产生无意义目标）
    - floor: 止损价下限保护（防价格算成 ≤ 0）
    """
    tp = min(entry_price * (1 + tp_pct), tp_max)
    sl = max(entry_price * (1 - sl_pct), floor) if sl_pct > 0 else 0.0
    return tp, sl
