"""Polymarket 市场发现：定位当前窗口的 Up/Down 市场。

slug 模式（实查确认）：btc-updown-15m-<窗口起始 Unix 秒> / btc-updown-5m-<ts>。
窗口按 interval 对齐（UTC 边界），7×24 连续。gamma 返回的
outcomes/clobTokenIds/outcomePrices 是 JSON 编码字符串，需二次解析。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

import requests

from pmbot.constants import step_ms_for, window_start_sec

GAMMA_HOST = "https://gamma-api.polymarket.com"


def window_start_ts(now_ms: int, interval: str = "15m") -> int:
    """当前窗口起始时间（Unix 秒，按 interval 对齐）。"""
    step = step_ms_for(interval)
    return window_start_sec(now_ms // 1000, step // 1000)


@dataclass(frozen=True)
class MarketInfo:
    condition_id: str
    yes_token_id: str
    no_token_id: str
    outcome_prices: tuple[float, float]
    accepting_orders: bool
    question: str
    end_date: str
    window_start: int


class MarketDiscovery:
    def __init__(
        self,
        fetch_markets: Callable | None = None,
        proxies: dict | None = None,
        timeout: int = 30,
        interval: str = "15m",
    ):
        # 注入 fake 便于测试；默认走 gamma-api
        self._fetch = fetch_markets or self._gamma_fetch
        self._proxies = proxies
        self._timeout = timeout
        self.interval = interval
        self.step_ms = step_ms_for(interval)
        self._cache: dict[tuple, MarketInfo | None] = {}  # (symbol, window_start, require_tradable)

    def _slug_for(self, symbol: str, window_start: int) -> str:
        return f"{symbol.lower()}-updown-{self.interval}-{window_start}"

    def _gamma_fetch(self, slug: str) -> list[dict]:
        try:
            r = requests.get(
                f"{GAMMA_HOST}/markets",
                params={"slug": slug},
                proxies=self._proxies,
                timeout=self._timeout,
            )
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            # 网络失败/非 JSON 响应 → 优雅降级为空
            return []

    @staticmethod
    def _parse_json_field(value) -> list:
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, ValueError):
                return []
        return []

    @staticmethod
    def _as_bool(value) -> bool:
        """gamma 的布尔字段可能是真布尔或字符串 \"True\"/\"False\"。"""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() == "true"
        return bool(value)

    def find_window(self, symbol: str, window_start: int, require_tradable: bool = True) -> MarketInfo | None:
        """定位指定 15m 窗口的 Up/Down 市场；不存在/不可交易返回 None。

        require_tradable=False 时允许查询已结算（closed）市场——结算时使用。
        """
        key = (symbol, window_start, require_tradable)
        if key in self._cache:
            return self._cache[key]
        result = self._fetch_window(symbol, window_start, require_tradable)
        self._cache[key] = result
        return result

    def invalidate(self, symbol: str, window_start: int, require_tradable: bool = True) -> None:
        """清除指定窗口的查询缓存（含负缓存 None）。

        _settle 首次查询网络失败会把 None 永久缓存导致结算死循环，
        失败后清除缓存让下一 tick 重新查询。
        """
        self._cache.pop((symbol, window_start, require_tradable), None)

    def _fetch_window(self, symbol: str, window_start: int, require_tradable: bool):
        """实际查询 gamma（被 find_window 缓存包裹，同窗口只查一次）。"""
        slug = self._slug_for(symbol, window_start)
        markets = self._fetch(slug)
        if not isinstance(markets, list) or not markets:
            return None
        m = markets[0]
        if not isinstance(m, dict):
            return None
        # 防御：返回的市场必须匹配请求的 slug
        if m.get("slug") and m.get("slug") != slug:
            return None
        if require_tradable:
            if m.get("closed") or not self._as_bool(m.get("acceptingOrders")):
                return None
            if m.get("active") is False:
                return None

        outcomes = self._parse_json_field(m.get("outcomes"))
        clob_ids = self._parse_json_field(m.get("clobTokenIds"))
        prices = self._parse_json_field(m.get("outcomePrices"))
        if len(outcomes) != 2 or len(clob_ids) != 2 or len(prices) != 2:
            return None

        # 按结果标签定位 yes/no token（不依赖 [Up, Down] 顺序）
        labels = [str(o).strip().lower() for o in outcomes]
        yes_idx = next((i for i, l in enumerate(labels) if l in ("up", "yes", "true", "1")), 0)
        no_idx = next((i for i, l in enumerate(labels) if l in ("down", "no", "false", "0")), 1)
        if yes_idx == no_idx:
            return None
        try:
            yes_price, no_price = float(prices[yes_idx]), float(prices[no_idx])
        except (TypeError, ValueError):
            return None

        return MarketInfo(
            condition_id=str(m.get("conditionId", "")),
            yes_token_id=str(clob_ids[yes_idx]),
            no_token_id=str(clob_ids[no_idx]),
            outcome_prices=(yes_price, no_price),
            accepting_orders=True,
            question=str(m.get("question", "")),
            end_date=str(m.get("endDate", "")),
            window_start=window_start,
        )

    def find_current_window(self, symbol: str, now_ms: int) -> MarketInfo | None:
        """定位当前窗口的 Up/Down 市场（按 interval 对齐）。"""
        return self.find_window(symbol, window_start_ts(now_ms, self.interval))
