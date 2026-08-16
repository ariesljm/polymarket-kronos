"""预测方向准确率记录。

每次预测记录 (ts, direction, p_up, baseline_close)；目标 K 线闭合后按实际方向评估，
把判定结果（correct 0/1）持久化，累计供验证报告使用。
文件按标的隔离（predictions_<symbol>.csv），方法不再重复传 symbol。
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

    def evaluate(self, klines: pd.DataFrame) -> tuple[int, int, float]:
        """评估目标 K 线已闭合的未结算预测，返回 (本批 correct, total, accuracy)。

        ts 记录的是预测目标 K 线的时间戳（= 预测窗口开始时间，与 baseline K 线
        差一个步长）；目标 K 线必须已闭合才算数（latest 无更新则跳过）。
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
            actual_up = float(actual_close) > float(row["baseline_close"])
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
