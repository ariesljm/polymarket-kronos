"""盘口定价纯函数：从 CLOB orderbook 计算可执行价（单一事实源）。

executor（成交）与 book_sampler（面板落盘）共用此处实现，避免语义漂移：
统一口径 = 按可成交量加权均价（免疫微小量垃圾挂单污染），
累计可成交量不足 size 时返回 None（流动性不足 → 无报价，宁可显示缺失也不显示误导价）。
"""

from __future__ import annotations


def best_price(book: dict | None, side: str, want_max: bool) -> float | None:
    """从 CLOB orderbook 取最优价（极值）。

    注意：asks/bids 内部排序不定（升/降序均见），且可能存在微小量垃圾挂单
    污染极值——展示/成交应使用 weighted_price（按可成交量加权均价）。
    """
    if not book:
        return None
    levels = book.get(side) or []
    prices = []
    for level in levels:
        try:
            prices.append(float(level.get("price")))
        except (TypeError, ValueError):
            continue
    if not prices:
        return None
    return max(prices) if want_max else min(prices)


def weighted_price(book: dict | None, side: str, size: float = 5.0) -> float | None:
    """按可成交量加权均价：从最优档（asks 最低价 / bids 最高价）累计到 size 股。

    - 免疫微小量垃圾挂单污染（如 1 股 @0.009 使极值失真）
    - 累计量不足 size 时返回 None（流动性不足，无有效报价）
    - 返回 4 位小数的加权均价
    """
    if not book:
        return None
    levels = book.get(side) or []
    parsed = []
    for level in levels:
        try:
            parsed.append((float(level.get("price")), float(level.get("size", 0))))
        except (TypeError, ValueError):
            continue
    if not parsed:
        return None
    # asks 从低到高（最便宜先吃），bids 从高到低（最高价先卖）
    parsed.sort(key=lambda p: p[0], reverse=(side == "bids"))
    cum = 0.0
    cost = 0.0
    for price, qty in parsed:
        take = min(qty, size - cum)
        cost += take * price
        cum += take
        if cum >= size:
            break
    if cum < size:
        return None
    return round(cost / size, 4)
