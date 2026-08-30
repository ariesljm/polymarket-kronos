"""BookSampler（WS 版）测试：快照构建、增量更新、REST 兜底。"""

import json
import threading
import time

import pytest

from pmbot.book_sampler import BookSampler, _apply_price_change


def fake_book(price):
    return {"bids": [{"price": f"{price - 0.01}", "size": "10"}],
            "asks": [{"price": f"{price + 0.01}", "size": "10"}]}


def book_event(asset, bids, asks):
    return {"event_type": "book", "asset_id": asset,
            "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks]}


class FakeWS:
    """模拟 WS 连接：send 记录消息，recv 队列喂消息。"""

    def __init__(self, messages):
        self.sent = []
        self._messages = list(messages)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, msg):
        self.sent.append(msg)

    async def __aiter__(self):
        for m in self._messages:
            yield m


@pytest.fixture
def fake_connect(monkeypatch):
    """patch websockets.connect → FakeWS 队列（依次连接，耗尽后抛异常结束线程）。"""
    import websockets
    import json as _json

    def make(conns):
        state = {"i": 0}

        class FakeConn:
            def __init__(self, messages):
                self.ws = FakeWS(messages)
                self.sent = self.ws.sent

        def connect(*a, **kw):  # websockets.connect 是同步返回 async context manager
            if state["i"] >= len(conns):
                raise RuntimeError("no more conns")
            msgs = conns[state["i"]]
            state["i"] += 1
            return FakeWS(msgs)

        monkeypatch.setattr(websockets, "connect", connect)
        return state

    return make


def test_initial_dump_builds_snapshot(fake_connect):
    """订阅后 initial_dump（[book,...] 数组）构建全深度快照。"""
    import json as _json

    snap = book_event("tok-a", [(0.45, 10), (0.44, 5)], [(0.46, 8)])
    fake_connect([[f"PONG", _json.dumps([snap])]])
    s = BookSampler(interval=0.05)
    s.subscribe(["tok-a"])
    s.start()
    try:
        deadline = time.time() + 2
        while time.time() < deadline and s.snapshot("tok-a") is None:
            time.sleep(0.02)
        got = s.snapshot("tok-a")
        assert got["bids"][0] == {"price": "0.45", "size": "10"}
        assert got["asks"][0] == {"price": "0.46", "size": "8"}
    finally:
        s.stop()
        s.join(timeout=2)


def test_price_change_updates_snapshot(fake_connect):
    """price_change 增量更新：BUY 改 bids，SELL 改 asks，size=0 删档。"""
    import json as _json

    snap = book_event("tok-a", [(0.45, 10)], [(0.46, 8)])
    changes = {"event_type": "price_change", "price_changes": [
        {"asset_id": "tok-a", "price": "0.45", "size": "12", "side": "BUY"},
        {"asset_id": "tok-a", "price": "0.47", "size": "0", "side": "SELL"},
    ]}
    fake_connect([[_json.dumps([snap]), _json.dumps(changes)]])
    s = BookSampler(interval=0.05)
    s.subscribe(["tok-a"])
    s.start()
    try:
        deadline = time.time() + 2
        got = None
        while time.time() < deadline:
            got = s.snapshot("tok-a")
            if got and got["bids"][0]["size"] == "12":
                break
            time.sleep(0.02)
        assert got["bids"][0] == {"price": "0.45", "size": "12"}  # 更新
        assert all(l["price"] != "0.47" for l in got["asks"])  # 删档
    finally:
        s.stop()
        s.join(timeout=2)


def test_unsubscribe_clears_snapshot(fake_connect):
    """退订清空快照；再次订阅触发新连接。"""
    import json as _json

    snap = book_event("tok-a", [(0.45, 10)], [(0.46, 8)])
    fake_connect([[_json.dumps([snap])]])
    s = BookSampler(interval=0.05)
    s.subscribe(["tok-a"])
    s.start()
    try:
        deadline = time.time() + 2
        while time.time() < deadline and s.snapshot("tok-a") is None:
            time.sleep(0.02)
        s.subscribe([])
        time.sleep(0.15)
        assert s.snapshot("tok-a") is None
    finally:
        s.stop()
        s.join(timeout=2)


def test_rest_fallback_keeps_snapshot_alive(fake_connect):
    """WS 断线重连期间 REST 兜底刷新快照（旧快照保留）。"""
    import json as _json

    ws_snap = book_event("tok-a", [(0.45, 10)], [(0.46, 8)])
    calls = {"n": 0}

    def fetch(tok):
        calls["n"] += 1
        return fake_book(0.50)

    fake_connect([[_json.dumps([ws_snap])]])
    s = BookSampler(fetch, interval=0.05)
    s.subscribe(["tok-a"])
    s.start()
    try:
        deadline = time.time() + 2
        while time.time() < deadline and s.snapshot("tok-a") is None:
            time.sleep(0.02)
        # WS 断开（conns 耗尽抛异常）→ 重连前 REST 兜底
        deadline = time.time() + 3
        while time.time() < deadline and calls["n"] == 0:
            time.sleep(0.05)
        assert calls["n"] > 0
        assert float(s.snapshot("tok-a")["bids"][0]["price"]) == 0.49  # REST 快照
    finally:
        s.stop()
        s.join(timeout=2)


def test_apply_price_change_orders_levels():
    """_apply_price_change：bids 降序、asks 升序。"""
    snap = {"bids": [], "asks": []}
    _apply_price_change(snap, {"side": "BUY", "price": "0.45", "size": "5"})
    _apply_price_change(snap, {"side": "BUY", "price": "0.50", "size": "3"})
    _apply_price_change(snap, {"side": "SELL", "price": "0.46", "size": "7"})
    _apply_price_change(snap, {"side": "SELL", "price": "0.44", "size": "2"})
    assert [l["price"] for l in snap["bids"]] == ["0.50", "0.45"]
    assert [l["price"] for l in snap["asks"]] == ["0.44", "0.46"]


def test_flush_book_writes_mapped_prices(tmp_path, fake_connect):
    """落盘 book.json：direction_map 映射 + 加权价（免疫垃圾单）。"""
    from pmbot.book_sampler import BookSampler

    book_path = tmp_path / "book.json"
    state = fake_connect([[]])
    s = BookSampler(book_path=str(book_path))
    s.subscribe(["tok-up", "tok-down"], direction_map={"tok-up": "up", "tok-down": "down"})
    # 注入快照：UP 带 1 股 @0.009 垃圾 ask；真实流动性在 0.02
    with s._lock:
        s._snapshots["tok-up"] = {
            "bids": [{"price": "0.01", "size": "10"}],
            "asks": [{"price": "0.009", "size": "1"}, {"price": "0.02", "size": "10"}],
        }
        s._snapshots["tok-down"] = {
            "bids": [{"price": "0.97", "size": "10"}],
            "asks": [{"price": "0.98", "size": "10"}],
        }
    s._flush_book()
    assert book_path.is_file()
    data = json.loads(book_path.read_text(encoding="utf-8"))
    # 5 股加权：0.009×1 + 0.02×4 = 0.089/5 = 0.0178
    assert data["up_ask"] == 0.0178
    assert data["up_bid"] == 0.01
    assert data["down_ask"] == 0.98
    assert data["down_bid"] == 0.97
    assert "ts" in data


def test_flush_book_partial_writes_nulls(tmp_path, fake_connect):
    """方向缺快照/流动性不足时：该方向写 null，文件照写、时间戳照更（防旧价冻结）。"""
    from pmbot.book_sampler import BookSampler

    book_path = tmp_path / "book.json"
    s = BookSampler(book_path=str(book_path))
    s.subscribe(["tok-up", "tok-down"], direction_map={"tok-up": "up", "tok-down": "down"})
    # tok-down 无快照（null）；tok-up asks 仅 1 股（<5 股 → 流动性不足 → null），bids 正常
    with s._lock:
        s._snapshots["tok-up"] = {"bids": [{"price": "0.01", "size": "10"}],
                                  "asks": [{"price": "0.02", "size": "1"}]}
    s._flush_book()
    assert book_path.is_file()
    data = json.loads(book_path.read_text(encoding="utf-8"))
    assert data["up_ask"] is None  # 流动性不足 → null
    assert data["up_bid"] == 0.01
    assert data["down_ask"] is None  # 无快照 → null
    assert data["down_bid"] is None
    assert "ts" in data

    # 方向补齐后正常写全量（时间戳持续更新）
    with s._lock:
        s._snapshots["tok-down"] = {"bids": [{"price": "0.97", "size": "10"}],
                                    "asks": [{"price": "0.98", "size": "10"}]}
        s._snapshots["tok-up"] = {"bids": [{"price": "0.01", "size": "10"}],
                                    "asks": [{"price": "0.02", "size": "10"}]}
    s._flush_book()
    data = json.loads(book_path.read_text(encoding="utf-8"))
    assert data["up_bid"] == 0.01
    assert data["up_ask"] == 0.02
    assert data["down_ask"] == 0.98
    assert data["down_bid"] == 0.97


def test_flush_book_no_direction_map_skips(tmp_path, fake_connect):
    """尚未订阅方向映射（direction_map 空）时不落盘。"""
    from pmbot.book_sampler import BookSampler

    book_path = tmp_path / "book.json"
    s = BookSampler(book_path=str(book_path))
    s.subscribe(["tok-a"])
    s._flush_book()
    assert not book_path.is_file()


def test_push_subscriptions_diff_in_base():
    """订阅 diff 推送收敛在 ReconnectingWsThread._push_subscriptions。"""
    from pmbot.ws_thread import ReconnectingWsThread

    t = ReconnectingWsThread()
    t._last_subscribed = {"a", "c"}
    sent = []

    class FakeLoopWS:
        async def send(self, msg):
            sent.append(msg)

    import asyncio
    import threading
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    t._connected_ws = FakeLoopWS()
    t._loop = loop
    try:
        t._push_subscriptions({"a", "b"}, "assets_ids")
        fut = asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), loop)
        fut.result(timeout=2)
        assert len(sent) == 2
        rm = json.loads(sent[0])
        add = json.loads(sent[1])
        assert rm == {"operation": "unsubscribe", "assets_ids": ["c"]}
        assert add == {"operation": "subscribe", "assets_ids": ["b"]}
        assert t._last_subscribed == {"a", "b"}
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_subscribe_pushes_update_message():
    """订阅集合变化且 WS 连接中 → 推送官方 update 消息（不重连）。"""
    import asyncio
    import threading
    from pmbot.book_sampler import BookSampler

    s = BookSampler()
    s.subscribe(["tok-a"], direction_map={"tok-a": "up"})
    s._last_subscribed = {"tok-a"}  # 模拟已连接且已全量订阅
    ws = FakeWS([])
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    s._connected_ws = ws
    s._loop = loop
    try:
        s.subscribe(["tok-a", "tok-b"])  # 变化 → 调度 update
        fut = asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), loop)
        fut.result(timeout=2)
        msgs = [json.loads(m) for m in ws.sent]
        add = [m for m in msgs if m.get("operation") == "subscribe"]
        rm = [m for m in msgs if m.get("operation") == "unsubscribe"]
        assert add and add[-1]["assets_ids"] == ["tok-b"]
        assert not rm  # 只有新增，无退订
        assert s._last_subscribed == {"tok-a", "tok-b"}
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_subscribe_no_message_when_unchanged():
    """订阅集合无变化 → 不推送任何消息。"""
    import asyncio
    import threading
    from pmbot.book_sampler import BookSampler

    s = BookSampler()
    s.subscribe(["tok-a"])
    s._last_subscribed = {"tok-a"}
    ws = FakeWS([])
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    s._connected_ws = ws
    s._loop = loop
    try:
        s.subscribe(["tok-a"])  # 无变化
        fut = asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), loop)
        fut.result(timeout=2)
        assert ws.sent == []
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


# ---- 快照新鲜度（A）+ 健康检查（B）+ 轻量事件（D） ----


def test_update_snapshot_and_age():
    """update_snapshot 注入快照并刷新时间戳；snapshot_age 返回年龄（秒）。"""
    from pmbot.book_sampler import BookSampler

    s = BookSampler()
    assert s.snapshot("tok-a") is None
    assert s.snapshot_age("tok-a") is None  # 无快照
    s.update_snapshot("tok-a", {"bids": [], "asks": []})
    assert s.snapshot("tok-a") == {"bids": [], "asks": []}
    assert s.snapshot_age("tok-a") is not None
    assert s.snapshot_age("tok-a") >= 0.0  # 刚更新：年龄 ≈ 0


def test_light_price_event_parses_best_bid_ask():
    """轻量事件 best_bid_ask：记录轻量价 + 事件流心跳（无快照也不报错）。"""
    from pmbot.book_sampler import BookSampler

    s = BookSampler()
    s._apply_light_event({
        "event_type": "best_bid_ask", "asset_id": "tok-a",
        "best_bid": "0.61", "best_ask": "0.62",
    })
    # asset（非 asset_id）字段也容错
    s._apply_light_event({
        "event_type": "last_trade_price", "asset": "tok-b", "last_trade_price": "0.50",
    })
    lp = s.light_price("tok-a")
    assert lp is not None and lp["best_bid"] == 0.61 and lp["best_ask"] == 0.62
    assert s.light_price("tok-b")["last_trade_price"] == 0.50
    assert s.light_price("missing") is None


def test_light_event_touches_snapshot_freshness():
    """轻量事件刷新快照新鲜度：陈旧的快照被判定为新鲜（事件流仍活着）。"""
    from pmbot.book_sampler import BookSampler

    s = BookSampler()
    s.update_snapshot("tok-a", {"bids": [], "asks": []})
    with s._lock:
        s._snapshot_ts["tok-a"] = time.monotonic() - 30  # 30 秒前 → 陈旧
    assert s.snapshot_age("tok-a") > 3.0
    s._apply_light_event({"event_type": "best_bid_ask", "asset_id": "tok-a", "best_bid": "0.6"})
    assert s.snapshot_age("tok-a") < 3.0  # 心跳刷新


def test_health_check_refreshes_stale_snapshot():
    """健康检查（B）：WS 连接中快照陈旧 → REST 现拉刷新并更新时间戳。"""
    from pmbot.book_sampler import BookSampler

    calls = {"n": 0}

    def fetch(tok):
        calls["n"] += 1
        return fake_book(0.60)

    s = BookSampler(fetch)
    s.subscribe(["tok-a"])
    with s._lock:
        s._snapshots["tok-a"] = {"bids": [{"price": "0.40", "size": "10"}],
                                 "asks": [{"price": "0.41", "size": "10"}]}
        s._snapshot_ts["tok-a"] = time.monotonic() - 30  # 陈旧
    s._connected_ws = object()  # 模拟 WS 连接中
    s._health_check()
    assert calls["n"] == 1
    snap = s.snapshot("tok-a")
    assert float(snap["bids"][0]["price"]) == 0.59  # REST 新快照（0.60-0.01）
    assert s.snapshot_age("tok-a") < 1.0  # 时间戳已刷新


def test_health_check_skips_fresh_snapshot():
    """健康检查：快照新鲜（≤ STALE_AGE_SEC）→ 不 REST，避免浪费查询。"""
    from pmbot.book_sampler import BookSampler

    calls = {"n": 0}

    def fetch(tok):
        calls["n"] += 1
        return fake_book(0.60)

    s = BookSampler(fetch)
    s.subscribe(["tok-a"])
    s.update_snapshot("tok-a", {"bids": [{"price": "0.40", "size": "10"}],
                                "asks": [{"price": "0.41", "size": "10"}]})
    s._connected_ws = object()
    s._health_check()
    assert calls["n"] == 0


def test_health_check_skips_when_disconnected():
    """健康检查：WS 断线期间跳过（_while_disconnected 已有 2s REST 兜底，防重复查询）。"""
    from pmbot.book_sampler import BookSampler

    calls = {"n": 0}

    def fetch(tok):
        calls["n"] += 1
        return fake_book(0.60)

    s = BookSampler(fetch)
    s.subscribe(["tok-a"])
    with s._lock:
        s._snapshot_ts["tok-a"] = time.monotonic() - 30  # 陈旧但断线
    s._health_check()
    assert calls["n"] == 0


def test_health_check_missing_snapshot_refreshes():
    """健康检查：订阅了但快照缺失（动态订阅失败的场景）→ REST 补齐。"""
    from pmbot.book_sampler import BookSampler

    calls = {"n": 0}

    def fetch(tok):
        calls["n"] += 1
        return fake_book(0.60)

    s = BookSampler(fetch)
    s.subscribe(["tok-a"])
    s._connected_ws = object()
    s._health_check()
    assert calls["n"] == 1
    assert float(s.snapshot("tok-a")["asks"][0]["price"]) == 0.61


def test_rest_fallback_updates_timestamp():
    """REST 兜底写回同时刷新新鲜度时间戳（断线兜底结果同样被消费方信任）。"""
    from pmbot.book_sampler import BookSampler

    def fetch(tok):
        return fake_book(0.50)

    s = BookSampler(fetch)
    s.subscribe(["tok-a"])
    s._rest_fallback()
    assert float(s.snapshot("tok-a")["asks"][0]["price"]) == 0.51
    assert s.snapshot_age("tok-a") < 3.0
