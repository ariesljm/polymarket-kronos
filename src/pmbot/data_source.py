"""Binance 数据源：增量拉取 + CSV 滚动存储。

KlineStore 负责本地 CSV 持久化（每标的一个文件，追加/去重/裁剪）；
BinanceDataSource 负责网络拉取（Binance 公开接口），只把新数据交给 KlineStore。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from pmbot.constants import step_ms_for
from pmbot.fileio import atomic_write_text, df_to_csv_text

logger = logging.getLogger(__name__)

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class Kline:
    timestamp: int  # epoch 毫秒
    open: float
    high: float
    low: float
    close: float
    volume: float


def fetch_klines_batch(
    symbol: str,
    timeframe: str,
    since: int | None,
    limit: int | None,
    proxies: dict | None = None,
) -> list[Kline]:
    """Binance 公共数据镜像拉取一批 K 线（在线数据源与离线回测共用）。

    主站 api.binance.com 在中国大陆不可达，镜像端点只提供公开市场数据，
    足够 K 线需求；直接请求公开接口。proxies 为 None 时走 requests 环境变量。
    """
    import requests

    sym = symbol.replace("/", "")
    if not sym.endswith("USDT"):
        sym += "USDT"
    params = {"symbol": sym, "interval": timeframe}
    if limit is not None:
        params["limit"] = limit
    if since is not None:
        params["startTime"] = since
    r = requests.get(
        "https://data-api.binance.vision/api/v3/klines",
        params=params,
        timeout=45,
        proxies=proxies,
    )
    r.raise_for_status()
    return [
        Kline(int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]))
        for row in r.json()
    ]


class KlineStore:
    """本地 CSV 滚动存储：每标的一个文件，追加增量、按时间戳去重、超上限裁剪。"""

    def __init__(self, data_dir: Path | str, timeframe: str = "15m"):
        self._data_dir = Path(data_dir)
        self._timeframe = timeframe
        self._data_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str) -> Path:
        return self._data_dir / f"{symbol.lower()}_{self._timeframe}.csv"

    def load(self, symbol: str) -> pd.DataFrame:
        path = self._path(symbol)
        if not path.is_file():
            return pd.DataFrame(columns=COLUMNS)
        return pd.read_csv(path)

    def latest_ts(self, symbol: str) -> int | None:
        df = self.load(symbol)
        if df.empty:
            return None
        return int(df["timestamp"].iloc[-1])

    def append(self, symbol: str, klines: list[Kline]) -> None:
        if not klines:
            return
        new_df = pd.DataFrame(
            [
                {
                    "timestamp": k.timestamp,
                    "open": k.open,
                    "high": k.high,
                    "low": k.low,
                    "close": k.close,
                    "volume": k.volume,
                }
                for k in klines
            ]
        )
        path = self._path(symbol)
        existing = self.load(symbol)
        merged = (
            pd.concat([existing, new_df])
            .drop_duplicates(subset="timestamp", keep="last")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )
        # 原子写入：先写临时文件再替换
        atomic_write_text(path, df_to_csv_text(merged, COLUMNS))

    def trim(self, symbol: str, max_rows: int) -> None:
        df = self.load(symbol)
        if len(df) > max_rows:
            df = df.tail(max_rows).reset_index(drop=True)
            path = self._path(symbol)
            atomic_write_text(path, df_to_csv_text(df, COLUMNS))


class BinanceDataSource:
    """Binance 数据源：首次全量回填，之后增量拉取，持久化到 KlineStore。"""

    def __init__(
        self,
        store: KlineStore,
        fetch_fn=None,
        max_klines: int = 2048,
        timeframe: str = "15m",
        backfill_page: int = 1000,
    ):
        self.store = store
        self.max_klines = max_klines
        self.timeframe = timeframe
        self._page = backfill_page
        self._fetch = fetch_fn or fetch_klines_batch

    def update(self, symbol: str):
        """拉取最新数据并返回当前全量 K 线（DataFrame）。"""
        latest = self.store.latest_ts(symbol)
        if latest is None:
            logger.info("首次运行，回填 %s 历史 K 线（%d 根）", symbol, self.max_klines)
            self._backfill(symbol)
        else:
            logger.info("增量拉取 %s：since=%d", symbol, latest)
            self._fetch_incremental(symbol, latest)
        self.store.trim(symbol, self.max_klines)
        return self.store.load(symbol)

    def _backfill(self, symbol: str) -> None:
        """向前分页拉历史，直到拉满 max_klines 或没有更早数据。"""
        need = self.max_klines
        since = None
        while need > 0:
            batch = self._fetch(symbol, self.timeframe, since, self._page)
            if not batch:
                break
            before = len(self.store.load(symbol))
            self.store.append(symbol, batch)
            added = len(self.store.load(symbol)) - before
            if added == 0:
                break  # 已无新数据（防御：数据源未响应 since）
            need -= added
            # 向前翻页：以本批最早一条再往前一页
            since = batch[0].timestamp - self._page * step_ms_for(self.timeframe)

    def _fetch_incremental(self, symbol: str, since: int) -> None:
        batch = self._fetch(symbol, self.timeframe, since, None)
        self.store.append(symbol, batch)
