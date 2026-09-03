"""预测方向准确率记录。

每次预测记录 (ts, direction, p_up, baseline_close)；目标窗口结束后按实际方向评估，
把判定结果（correct 0/1）持久化，累计供验证报告使用。
文件按标的隔离（predictions_<symbol>.csv），方法不再重复传 symbol。

评估口径（与 Polymarket 结算对齐）：
- 默认用目标窗口结束瞬时价（close）> baseline 判定（旧口径）；
- 传入 settle_prices（{窗口开始 ts: 窗口最后 60 秒均价}，近似 Polymarket 5m/15m/4h
  市场的 Chainlink 60s TWAP 结算参考价）时改用该均价判定，缺失窗口回退 close 口径。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from pmbot.fileio import atomic_write_text, df_to_csv_text
from pmbot.types import Direction

COLUMNS = ["ts", "direction", "p_up", "baseline_close", "evaluated", "correct"]


class PredictionLog:
    def __init__(self, data_dir: Path | str, symbol: str = "BTC"):
        self._path = Path(data_dir) / f"predictions_{symbol.lower()}.csv"

    def _load(self) -> pd.DataFrame:
        if not self._path.is_file():
            return pd.DataFrame(columns=COLUMNS)
        return pd.read_csv(self._path)

    def _save(self, df: pd.DataFrame) -> None:
        atomic_write_text(self._path, df_to_csv_text(df, COLUMNS))

    def reset(self) -> None:
        """删除预测记录文件（下次预测自动重建）。"""
        if self._path.is_file():
            self._path.unlink()

    def record(
        self,
        ts: int,
        direction: Direction,
        p_up: float,
        baseline_close: float,
    ) -> None:
        row = pd.DataFrame(
            [
                {
                    "ts": ts,
                    "direction": direction.value,
                    "p_up": p_up,
                    "baseline_close": baseline_close,
                    "evaluated": 0,
                    "correct": None,
                }
            ]
        )
        merged = (
            pd.concat([self._load(), row])
            .drop_duplicates(subset="ts", keep="last")
            .sort_values("ts")
            .reset_index(drop=True)
        )
        self._save(merged)

    def pending_targets(self) -> list[int]:
        """未评估预测的目标窗口 ts 列表（供外部按 TWAP 结算口径获取结算参考价）。"""
        df = self._load()
        return [int(r.ts) for r in df.itertuples() if not r.evaluated]

    def evaluate(
        self, klines: pd.DataFrame, settle_prices: dict[int, float] | None = None
    ) -> tuple[int, int, float]:
        """评估目标 K 线已闭合的未结算预测，返回 (本批 correct, total, accuracy)。

        ts 记录的是预测目标 K 线的时间戳（= 预测窗口开始时间，与 baseline K 线
        差一个步长）；目标 K 线必须已闭合才算数（latest 无更新则跳过）。

        settle_prices: {目标窗口 ts: 窗口最后 60 秒均价}，与 Polymarket 5m/15m/4h
        市场的 Chainlink 60s TWAP 结算口径对齐（比"窗口结束瞬时 close"更接近结算
        参考价）。settle_prices 为 None/空（未启用 TWAP 口径）时用目标 K 线 close 判定；
        启用后缺失的窗口暂不评估（数据未最终/拉取失败，留待下轮重试），不回退 close
        口径——避免用错误时效的数据污染准确率。
        """
        if klines.empty:
            return 0, 0, 0.0
        latest = int(klines["timestamp"].iloc[-1])
        # 步长从 K 线时间差推断（支持 5m/15m/1h 任意 interval）
        step = int(klines["timestamp"].astype(int).diff().dropna().median())
        close_by_ts = dict(zip(klines["timestamp"].astype(int), klines["close"]))
        df = self._load()
        correct = 0
        total = 0
        touched = False
        for idx, row in df.iterrows():
            if row["evaluated"]:
                continue
            target_ts = int(row["ts"])
            if target_ts >= latest:
                continue  # 目标 K 线未闭合（或正在进行）
            actual_close = close_by_ts.get(target_ts)
            if actual_close is None:
                continue  # 数据缺口（如被滚动裁剪），跳过
            # 结算口径：优先目标窗口最后 60 秒均价（近似 Polymarket 60s Chainlink TWAP）。
            # 启用 TWAP 口径（settle_prices 非空）后，缺失/未最终化的窗口暂不评估（下轮重试）；
            # 未启用（None/空 dict）时回退目标 K 线结束瞬时 close（旧口径）。
            ref_price = actual_close
            if settle_prices:
                ref_price = settle_prices.get(target_ts)
                if ref_price is None:
                    continue  # 结算参考价未就绪：留待下轮重试，不用 close 近似污染统计
            actual_up = float(ref_price) > float(row["baseline_close"])
            predicted_up = row["direction"] == Direction.UP.value
            is_correct = int(actual_up == predicted_up)
            df.at[idx, "evaluated"] = 1
            df.at[idx, "correct"] = is_correct
            total += 1
            correct += is_correct
            touched = True
        if touched:
            self._save(df)
        acc = correct / total if total else 0.0
        return correct, total, acc

    def accuracy(self) -> dict:
        df = self._load()
        done = df[df["evaluated"] == 1]
        total = len(done)
        correct = int(done["correct"].fillna(0).sum())
        return {
            "correct": correct,
            "total": total,
            "accuracy": correct / total if total else 0.0,
        }
