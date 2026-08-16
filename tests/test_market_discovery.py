"""市场发现模块测试：计算 15m 窗口 slug + gamma 查询（注入 fake 响应）。"""

import pytest

from pmbot.market_discovery import MarketDiscovery, MarketInfo, window_start_ts


def test_window_start_ts_aligns_to_15m():
    # 2026-08-15 05:22:00 UTC → 对齐到 05:15:00（1786770900）
    assert window_start_ts(1_786_771_020_000) == 1_786_770_900
    # 恰好对齐时不偏移
    assert window_start_ts(1_786_770_900_000) == 1_786_770_900


def test_slug_pattern():
    assert MarketDiscovery(interval="15m")._slug_for("BTC", 1_786_770_900) == "btc-updown-15m-1786770900"
    assert MarketDiscovery(interval="5m")._slug_for("BTC", 1_786_770_900) == "btc-updown-5m-1786770900"


def test_find_current_window_market():
    """fake gamma 返回与 slug 匹配的市场 → 解析 token/condition/时间线。"""
    calls = []

    def fake_gamma(slug):
        calls.append(slug)
        assert slug == "btc-updown-15m-1786770900"
        return [
            {
                "slug": slug,
                "question": "Bitcoin Up or Down - August 15, 1:15AM-1:30AM ET",
                "conditionId": "0xabc",
                "outcomes": '["Up", "Down"]',
                "clobTokenIds": '["111", "222"]',
                "outcomePrices": '["0.505", "0.495"]',
                "acceptingOrders": True,
                "closed": False,
                "endDate": "2026-08-15T05:30:00Z",
            }
        ]

    disc = MarketDiscovery(fetch_markets=fake_gamma)
    m = disc.find_current_window("BTC", now_ms=1_786_771_020_000)
    assert isinstance(m, MarketInfo)
    assert m.condition_id == "0xabc"
    assert m.yes_token_id == "111"
    assert m.no_token_id == "222"
    assert m.accepting_orders is True
    assert m.outcome_prices == (0.505, 0.495)


def test_market_not_found_returns_none():
    disc = MarketDiscovery(fetch_markets=lambda slug: [])
    assert disc.find_current_window("BTC", now_ms=1_786_771_020_000) is None


def test_closed_market_returns_none():
    disc = MarketDiscovery(
        fetch_markets=lambda slug: [
            {"slug": slug, "acceptingOrders": False, "closed": True}
        ]
    )
    assert disc.find_current_window("BTC", now_ms=1_786_771_020_000) is None


def test_json_string_fields_parsed():
    """gamma 的 outcomes/clobTokenIds/outcomePrices 是 JSON 编码字符串，需二次解析。"""
    market = {
        "slug": "btc-updown-15m-1786770900",
        "acceptingOrders": True,
        "closed": False,
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": '["111", "222"]',
        "outcomePrices": '["0.505", "0.495"]',
    }
    disc = MarketDiscovery(fetch_markets=lambda slug: [market])
    m = disc.find_current_window("BTC", now_ms=1_786_771_020_000)
    assert m.yes_token_id == "111"
    assert m.no_token_id == "222"
    assert m.outcome_prices == (0.505, 0.495)


def test_outcomes_order_not_assumed():
    """outcomes 顺序颠倒（[Down, Up]）时仍按标签正确映射 yes/no token。"""
    market = {
        "slug": "btc-updown-15m-1786770900",
        "acceptingOrders": True,
        "closed": False,
        "outcomes": '["Down", "Up"]',
        "clobTokenIds": '["222", "111"]',
        "outcomePrices": '["0.495", "0.505"]',
    }
    disc = MarketDiscovery(fetch_markets=lambda slug: [market])
    m = disc.find_current_window("BTC", now_ms=1_786_771_020_000)
    assert m.yes_token_id == "111"
    assert m.no_token_id == "222"
    assert m.outcome_prices == (0.505, 0.495)


def test_string_accepting_orders_parsed():
    """acceptingOrders 可能是字符串 "False"，不应误判为可交易。"""
    market = {
        "slug": "btc-updown-15m-1786770900",
        "acceptingOrders": "False",
        "closed": False,
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": '["111", "222"]',
        "outcomePrices": '["0.505", "0.495"]',
    }
    disc = MarketDiscovery(fetch_markets=lambda slug: [market])
    assert disc.find_current_window("BTC", now_ms=1_786_771_020_000) is None


def test_non_list_and_bad_responses_return_none():
    disc = MarketDiscovery(fetch_markets=lambda slug: {"error": "boom"})
    assert disc.find_current_window("BTC", now_ms=1_786_771_020_000) is None
    disc2 = MarketDiscovery(fetch_markets=lambda slug: ["not-a-dict"])
    assert disc2.find_current_window("BTC", now_ms=1_786_771_020_000) is None


def test_slug_mismatch_rejected():
    """返回的市场 slug 与请求不符（如 gamma 缓存串了）→ 拒绝。"""
    market = {
        "slug": "btc-updown-15m-999",
        "acceptingOrders": True,
        "closed": False,
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": '["111", "222"]',
        "outcomePrices": '["0.505", "0.495"]',
    }
    disc = MarketDiscovery(fetch_markets=lambda slug: [market])
    assert disc.find_current_window("BTC", now_ms=1_786_771_020_000) is None


def test_window_cache_skips_repeated_fetch():
    """同窗口重复查询不触发网络请求（窗口级缓存）。"""
    calls = {"n": 0}

    def counting_fetch(slug):
        calls["n"] += 1
        return [{
            "slug": slug,
            "question": "Q",
            "conditionId": "0xabc",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["111", "222"]',
            "outcomePrices": '["0.5", "0.5"]',
            "acceptingOrders": True,
        }]

    disc = MarketDiscovery(fetch_markets=counting_fetch, interval="5m")
    m1 = disc.find_current_window("BTC", now_ms=1_786_771_020_000)
    m2 = disc.find_current_window("BTC", now_ms=1_786_771_040_000)  # 同窗口（20s 后）
    assert m1 is not None and m2 is not None
    assert calls["n"] == 1  # 只 fetch 一次

    # 跨窗口 → 重新 fetch
    disc.find_current_window("BTC", now_ms=1_786_771_300_000)  # +280s 到下一窗口
    assert calls["n"] == 2


def test_window_cache_caches_none():
    """窗口不存在（None 结果）也缓存，避免每 tick 反复请求。"""
    calls = {"n": 0}

    def counting_fetch(slug):
        calls["n"] += 1
        return []

    disc = MarketDiscovery(fetch_markets=counting_fetch, interval="5m")
    assert disc.find_current_window("BTC", now_ms=1_786_771_020_000) is None
    assert disc.find_current_window("BTC", now_ms=1_786_771_040_000) is None
    assert calls["n"] == 1
