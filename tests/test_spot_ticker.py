"""SpotTickerThread 测试：WS miniTicker 解析、REST 兜底注入、端到端 WS 收价。"""

import json

import pytest

from pmbot.spot_ticker import SpotTickerThread, REST_URL_TMPL, WS_URL_TMPL
from pmbot.data_source import normalize_symbol


def mini_ticker(price: float) -> str:
    return json.dumps({"e": "24hrMiniTicker", "s": "BTCUSDT",
                       "c": f"{price}", "o": "70000", "h": "80000", "l": "65000"})


def test_ws_url_uses_mirror_endpoint():
    """必须用 data-stream.binance.vision 镜像（主站大陆不可达 HTTP 451）。"""
    t = SpotTickerThread(symbol="BTC")
    assert t.ws_url == WS_URL_TMPL.format(sym="btcusdt")
    assert "data-stream.binance.vision" in t.ws_url
    assert "stream.binance.com" not in t.ws_url


def test_handle_message_parses_mini_ticker():
    t = SpotTickerThread(symbol="BTC", fetch_ticker=lambda: None)
    assert t.latest_price() is None  # 初始无价
    t._handle_message(mini_ticker(78148.1))
    assert t.latest_price() == 78148.1
    t._handle_message(mini_ticker(78150.5))
    assert t.latest_price() == 78150.5


def test_handle_message_ignores_garbage():
    t = SpotTickerThread(symbol="BTC", fetch_ticker=lambda: None)
    t._handle_message("not json")
    t._handle_message(json.dumps({"e": "other"}))  # 无 c 字段
    t._handle_message(json.dumps({"e": "24hrMiniTicker", "s": "BTCUSDT", "c": "abc"}))
    assert t.latest_price() is None


def test_rest_fallback_uses_injected_fetch():
    calls = []
    t = SpotTickerThread(symbol="BTC", fetch_ticker=lambda: calls.append(1) or 78000.0)
    assert t.latest_price() is None
    t._while_disconnected()  # 断线等待期间兜底钩子
    assert t.latest_price() == 78000.0
    assert len(calls) == 1


def test_rest_fallback_failure_keeps_old_value():
    t = SpotTickerThread(symbol="BTC", fetch_ticker=lambda: None)
    t._handle_message(mini_ticker(77_000.0))
    t._while_disconnected()  # 失败静默
    assert t.latest_price() == 77_000.0


def test_rest_url_uses_mirror():
    t = SpotTickerThread(symbol="BTC")
    assert t._rest_url == REST_URL_TMPL.format(sym="BTCUSDT")
    assert t.symbol == "BTCUSDT"  # 幂等规范化：不拼成 btcusdtUSDT
    assert "data-api.binance.vision" in t._rest_url


def test_snapshot_compatible_with_panel():
    """snapshot() 与旧 SpotPrice 兼容（面板顶栏 {"price","delta"} 语义）。"""
    t = SpotTickerThread(symbol="BTC", fetch_ticker=lambda: None)
    assert t.snapshot() is None  # 尚未拉取
    t._handle_message(mini_ticker(77_000.0))
    assert t.snapshot() == {"price": 77_000.0, "delta": 0.0}
    t._handle_message(mini_ticker(77_050.0))
    assert t.snapshot() == {"price": 77_050.0, "delta": 50.0}


def test_normalize_symbol_shared_single_source():
    """交易对规范化单一事实源（data_source 归属，K 线/实时价/面板共用）。"""
    assert normalize_symbol("BTC") == "BTCUSDT"
    assert normalize_symbol("btc") == "BTCUSDT"
    assert normalize_symbol("BTCUSDT") == "BTCUSDT"
    assert normalize_symbol("btcusdt") == "BTCUSDT"  # 大小写幂等（原两实现互不相同）
    assert normalize_symbol("ETH/USDT") == "ETHUSDT"
    t = SpotTickerThread(symbol="BTCUSDT")
    assert t.symbol == "BTCUSDT" and t.ws_url.endswith("btcusdt@miniTicker")


# ---- 端到端 WS（patch websockets.connect → FakeWS，同 BookSampler 测试模式） ----


class FakeWS:
    def __init__(self, messages):
        self.sent = []
        self._messages = list(messages)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, msg):
        self.sent.append(msg)

    async def __aiter__(self):
        for m in self._messages:
            yield m


def test_ws_end_to_end_updates_price(monkeypatch):
    import time
    import websockets

    ticker = SpotTickerThread(symbol="BTC", fetch_ticker=lambda: None)
    messages = [mini_ticker(77_100.0), mini_ticker(77_200.0)]
    state = {"i": 0}

    def fake_connect(*a, **kw):
        if state["i"] >= 1:
            raise RuntimeError("no more conns")  # 耗尽连接，线程进入重连等待
        state["i"] += 1
        return FakeWS(messages)

    monkeypatch.setattr(websockets, "connect", fake_connect)
    ticker.start()
    try:
        deadline = time.time() + 3
        while time.time() < deadline and ticker.latest_price() is None:
            time.sleep(0.02)
        assert ticker.latest_price() == 77_200.0
    finally:
        ticker.stop()
        ticker.join(timeout=2)