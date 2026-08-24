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
    # 分页增量：第一页实际新增后继续翻页探空（since = 最后一根 ts + 步长）
    assert calls == [None, -899000000, 4_600_000, 7_300_000]


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


def test_incremental_paginates_past_single_batch(tmp_path):
    """增量跨越单批上限：停机积累 > 单批根数 → 分页拉完，无数据空洞。

    回归：数据-api 单次默认只回 500 根，停机 4 天（1152 根）一次只拉
    500 → 中间缺口，推理特征断裂（实证 4 天停机实测 500/1152）。
    """
    calls = []

    def fake_fetch(symbol, timeframe, since, limit):
        if since is None:
            # 回填：5 根旧数据
            calls.append(("backfill", since))
            return [make_kline(1_000_000 + i * 900_000) for i in range(5)]
        if since < 0:
            return []  # 回填向前探空：已到最早
        start = since
        # 每页最多 3 根（模拟单批上限）；数据到 7_300_000 为止（停机积累 3 根）
        i0 = max(0, (start - 4_600_000) // 900_000)
        last = (7_300_000 - start) // 900_000
        if last < 0:
            calls.append(("empty", since))
            return []
        batch = [make_kline(4_600_000 + (i0 + i) * 900_000)
                 for i in range(min(3, last + 1))]
        calls.append(("page", since, len(batch)))
        return batch

    ds = make_data_source(tmp_path, fake_fetch)
    ds.update("BTC")
    df = ds.update("BTC")  # 增量：4_600_000 之后的 3 根未拉，需 2 页
    assert len(df) == 8  # 5 旧 + 3 新（无空洞）
    ts = df["timestamp"].tolist()
    assert ts == sorted(ts) and len(set(ts)) == len(ts)
    page_calls = [c for c in calls if c and c[0] == "page"]
    assert len(page_calls) >= 2  # 确实分了多页


def test_incremental_stops_when_no_new_data(tmp_path):
    """增量返回的数据全部重复（数据源忽略 since/服务端异常）→ 停止，防死循环。"""
    calls = []

    def fake_fetch(symbol, timeframe, since, limit):
        if since is None:
            return [make_kline(1_000_000 + i * 900_000) for i in range(3)]
        # 永远返回同一批旧数据（since 被忽略的异常数据源）
        calls.append(since)
        return [make_kline(1_000_000 + i * 900_000) for i in range(3)]

    ds = make_data_source(tmp_path, fake_fetch)
    ds.update("BTC")
    df = ds.update("BTC")
    assert len(df) == 3  # 无新增、不重复翻页
    # 2 次调用：回填向前探空（-899000000，返回同批 → 无新数据即停）+ 增量一次（2800000，
    # 同批全重复 → 立即停止，不无限循环）
    assert calls == [-899_000_000, 2_800_000]
