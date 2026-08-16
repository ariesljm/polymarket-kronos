"""BinanceDataSource 测试：回填、增量、裁剪、分页（fetch 注入 fake，不碰网络）。"""

from pathlib import Path

import pytest

from pmbot.data_source import BinanceDataSource, Kline, KlineStore


def make_kline(ts, close=100.0):
    return Kline(timestamp=ts, open=close, high=close + 1, low=close - 1, close=close, volume=1.0)


def make_data_source(tmp_path, fetch_fn, max_klines=2048, page=1000):
    return BinanceDataSource(
        KlineStore(Path(tmp_path)), fetch_fn=fetch_fn, max_klines=max_klines, backfill_page=page
    )


def test_first_update_backfills_from_history(tmp_path):
    calls = []
    first = True

    def fake_fetch(symbol, timeframe, since, limit):
        calls.append(since)
        nonlocal first
        if first:
            first = False
            return [make_kline(1_000_000 + i * 900_000) for i in range(10)]
        return []  # 分页探空：已到最早

    ds = make_data_source(tmp_path, fake_fetch)
    df = ds.update("BTC")
    assert len(df) == 10
    assert calls[0] is None  # 首次回填 since=None


def test_second_update_is_incremental(tmp_path):
    calls = []

    def fake_fetch(symbol, timeframe, since, limit):
        calls.append(since)
        if since is None:
            return [make_kline(1_000_000 + i * 900_000) for i in range(5)]
        if since == 4_600_000:
            # 增量：从最后一条 ts 开始，含重合的最后一条，实际新增 2 根
            return [make_kline(4_600_000 + i * 900_000) for i in range(3)]
        return []  # 回填探空

    ds = make_data_source(tmp_path, fake_fetch)
    ds.update("BTC")
    df = ds.update("BTC")
    assert len(df) == 7  # 5 旧 + 2 新（重合的 4_600_000 被去重）
    assert calls == [None, -899000000, 4_600_000]


def test_update_trims_to_max_klines(tmp_path):
    def fake_fetch(symbol, timeframe, since, limit):
        return [make_kline(1_000_000 + i * 900_000) for i in range(12)]

    ds = make_data_source(tmp_path, fake_fetch, max_klines=5)
    ds.update("BTC")
    df = ds.update("BTC")
    assert len(df) == 5


def test_backfill_paginates_to_fill_history(tmp_path):
    """首次回填需拉满 max_klines 根：fetch 每页最多 limit 根，自动向前翻页。"""
    calls = []
    max_klines = 10
    page = 3

    def fake_fetch(symbol, timeframe, since, limit):
        if since is None:
            start = 10_000_000
        else:
            start = since
        batch = [make_kline(start + i * 900_000) for i in range(page)]
        calls.append((since, start))
        return batch

    ds = make_data_source(tmp_path, fake_fetch, max_klines=max_klines, page=page)
    ds.update("BTC")
    df = ds.update("BTC")
    # 回填 + 增量共至少 4 次分页（10 根 / 每页 3）
    assert len(calls) >= 4
    assert len(df) >= max_klines
