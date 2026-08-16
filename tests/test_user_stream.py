"""UserStream（认证 WS）测试：订阅协议、事件入队、动态 markets、主循环处理。"""

import asyncio
import json
import threading
import time

import pytest

from pmbot.user_stream import UserStream

AUTH = {"apiKey": "k-uuid", "secret": "s", "passphrase": "p"}


class FakeWS:
    def __init__(self, messages, hold_sec=0.5):
        self.sent = []
        self._messages = list(messages)
        self._hold = hold_sec

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, msg):
        self.sent.append(msg)

    async def __aiter__(self):
        for m in self._messages:
            yield m
        # 保持连接一小段时间（模拟真实 WS 长连接），再自然断开
        await asyncio.sleep(self._hold)


@pytest.fixture
def fake_connect(monkeypatch):
    import websockets

    def make(conns):
        state = {"i": 0, "ws": None}

        def connect(*a, **kw):
            if state["i"] >= len(conns):
                raise RuntimeError("no more conns")
            fw = FakeWS(conns[state["i"]])
            state["i"] += 1
            state["ws"] = fw
            return fw

        monkeypatch.setattr(websockets, "connect", connect)
        return state

    return make


def test_subscribe_sends_auth_and_markets(fake_connect):
    """订阅消息含 auth 凭证 + markets 过滤。"""
    state = fake_connect([[]])
    s = UserStream(AUTH)
    s.subscribe_markets(["cond-1", "cond-2"])
    s.start()
    try:
        deadline = time.time() + 2
        while time.time() < deadline and state["ws"] is None:
            time.sleep(0.02)
        assert state["ws"] is not None
        sub = json.loads(state["ws"].sent[0])
        assert sub["type"] == "user"
        assert sub["auth"] == AUTH
        assert sub["markets"] == ["cond-1", "cond-2"]
    finally:
        s.stop()
        s.join(timeout=2)


def test_order_and_trade_events_queued(fake_connect):
    """order/trade 事件入队；PONG 忽略。"""
    order = {"event_type": "order", "id": "oid-1", "status": "filled"}
    trade = {"event_type": "trade", "id": "t-1", "side": "SELL"}
    state = fake_connect([["PONG", json.dumps(order), json.dumps(trade)]])
    s = UserStream(AUTH)
    s.start()
    try:
        deadline = time.time() + 2
        got = []
        while time.time() < deadline and len(got) < 2:
            got = s.drain()
            time.sleep(0.02)
        assert got == [("order", order), ("trade", trade)]
    finally:
        s.stop()
        s.join(timeout=2)


def test_no_auth_does_not_connect(fake_connect):
    """无凭证不启动连接。"""
    state = fake_connect([[]])
    s = UserStream(None)
    s.start()
    time.sleep(0.2)
    assert state["i"] == 0
    assert s.drain() == []
    s.stop()
    s.join(timeout=2)


def test_connected_flag_updates(fake_connect):
    """连接成功 → connected=True；断开 → False。"""
    state = fake_connect([[]])
    s = UserStream(AUTH)
    s.start()
    try:
        deadline = time.time() + 2
        while time.time() < deadline and not s.connected:
            time.sleep(0.02)
        assert s.connected is True
        state["ws"]._messages.append("CLOSE")  # 结束 async for → 断开
        deadline = time.time() + 2
        while time.time() < deadline and s.connected:
            time.sleep(0.02)
        assert s.connected is False
    finally:
        s.stop()
        s.join(timeout=2)


def test_subscribe_markets_push_update(fake_connect):
    """连接后 markets 变化 → 增量 subscribe/unsubscribe 消息。"""
    state = fake_connect([["PONG"]])
    s = UserStream(AUTH)
    s.subscribe_markets(["cond-a", "cond-b"])
    s.start()
    try:
        deadline = time.time() + 2
        while time.time() < deadline and state["ws"] is None:
            time.sleep(0.02)
        assert state["ws"] is not None
        # 初始订阅后，动态更新 markets
        time.sleep(0.3)
        s.subscribe_markets(["cond-b", "cond-c"])
        time.sleep(0.5)
        ops = [json.loads(m) for m in state["ws"].sent if "operation" in m]
        assert any(o.get("operation") == "subscribe" and "cond-c" in o.get("markets", [])
                   for o in ops), f"应发 subscribe cond-c, 实际: {ops}"
        assert any(o.get("operation") == "unsubscribe" and "cond-a" in o.get("markets", [])
                   for o in ops), f"应发 unsubscribe cond-a, 实际: {ops}"
    finally:
        s.stop()
        s.join(timeout=2)


def test_subscribe_markets_before_connect_full_on_subscribe(fake_connect):
    """未连接时设置 markets → 连接后首条订阅消息含全量。"""
    state = fake_connect([["PONG"]])
    s = UserStream(AUTH)
    s.subscribe_markets(["cond-x"])
    s.start()
    try:
        deadline = time.time() + 2
        while time.time() < deadline and state["ws"] is None:
            time.sleep(0.02)
        assert state["ws"] is not None
        sub = json.loads(state["ws"].sent[0])
        assert sub["type"] == "user" and sub["markets"] == ["cond-x"]
    finally:
        s.stop()
        s.join(timeout=2)
