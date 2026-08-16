"""KlineStore 测试：CSV 滚动存储（增量追加、去重、裁剪）。"""

from pathlib import Path

import pytest

from pmbot.data_source import Kline, KlineStore


def make_klines(start_ts, n):
    return [
        Kline(
            timestamp=start_ts + i * 900_000,  # 15m = 900_000 ms
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            volume=10.0 + i,
        )
        for i in range(n)
    ]


@pytest.fixture
def store(tmp_path):
    return KlineStore(Path(tmp_path))


def test_empty_store_loads_empty_frame(store):
    df = store.load("BTC")
    assert df.empty
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]


def test_append_then_load(store):
    store.append("BTC", make_klines(1_000_000, 3))
    df = store.load("BTC")
    assert len(df) == 3
    assert df["timestamp"].tolist() == [1_000_000, 1_900_000, 2_800_000]
    assert df["close"].tolist() == [100.5, 101.5, 102.5]


def test_append_is_incremental_and_sorted(store):
    # 先写 2 条，再追加 2 条更新的（乱序传入也应按 ts 升序落盘）
    store.append("BTC", make_klines(1_000_000, 2))
    store.append("BTC", make_klines(2_800_000, 2))
    df = store.load("BTC")
    assert len(df) == 4
    assert df["timestamp"].is_monotonic_increasing


def test_duplicate_timestamps_deduplicated(store):
    store.append("BTC", make_klines(1_000_000, 2))
    # 增量边界可能重拉最后一条，应去重
    store.append("BTC", make_klines(1_900_000, 2))
    df = store.load("BTC")
    assert len(df) == 3
    assert df["timestamp"].tolist() == [1_000_000, 1_900_000, 2_800_000]


def test_trim_keeps_tail(store):
    store.append("BTC", make_klines(1_000_000, 10))
    store.trim("BTC", max_rows=4)
    df = store.load("BTC")
    assert len(df) == 4
    # 保留的是最新的 4 条（index 6-9）
    assert df["timestamp"].tolist() == [6_400_000, 7_300_000, 8_200_000, 9_100_000]


def test_latest_ts(store):
    assert store.latest_ts("BTC") is None
    store.append("BTC", make_klines(1_000_000, 3))
    assert store.latest_ts("BTC") == 2_800_000


def test_symbols_are_isolated(store):
    store.append("BTC", make_klines(1_000_000, 2))
    store.append("ETH", make_klines(5_000_000, 2))
    assert len(store.load("BTC")) == 2
    assert len(store.load("ETH")) == 2
