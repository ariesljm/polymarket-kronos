"""Kronos 模型推理客户端（真实模型，本地 CPU）。

KronosPredictor 的 sample_count 参数是"多条路径求平均"，不是独立采样；
要得到 P(up) 分布，这里对同一输入做多次独立采样（每次 sample_count=1）。
模型权重优先本地 models/，缺失时从 HuggingFace 下载。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from pmbot.variant_map import VARIANT_WEIGHTS

logger = logging.getLogger(__name__)


class KronosPredictorClient:
    def __init__(
        self,
        models_dir: str | Path = "models",
        variant: str = "kronos-mini",
        device: str | None = None,
    ):
        """device: None=启动时自动检测（有 GPU 用 cuda，否则 cpu）；显式指定则不检测。"""
        if variant not in VARIANT_WEIGHTS:
            raise ValueError(f"未知模型变体: {variant}，可用: {sorted(VARIANT_WEIGHTS)}")
        self._models_dir = Path(models_dir)
        self._variant = variant
        self._device = device
        self._predictor = None

    def _ensure_weights(self, model_dir: str, tokenizer_dir: str, repo: str) -> tuple[str, str]:
        model_path = self._models_dir / model_dir
        tok_path = self._models_dir / tokenizer_dir

        def ready(p: Path) -> bool:
            return any(p.glob("model.safetensors")) or any(p.glob("pytorch_model.bin"))

        from pmbot.vendor_kronos import Kronos, KronosTokenizer

        if not ready(model_path):
            logger.info("本地缺少模型权重 %s，从 %s/%s 下载", model_path, repo, model_dir)
            Kronos.from_pretrained(f"{repo}/{model_dir}").save_pretrained(model_path)
        if not ready(tok_path):
            logger.info("本地缺少 tokenizer %s，从 %s/%s 下载", tok_path, repo, tokenizer_dir)
            KronosTokenizer.from_pretrained(f"{repo}/{tokenizer_dir}").save_pretrained(tok_path)
        return str(model_path), str(tok_path)

    def _load(self):
        if self._predictor is not None:
            return self._predictor
        from pmbot.vendor_kronos import Kronos, KronosPredictor, KronosTokenizer
        from pmbot.variant_map import VARIANT_CONTEXT

        model_dir, tokenizer_dir, repo = VARIANT_WEIGHTS[self._variant]
        max_context = VARIANT_CONTEXT[self._variant]
        model_path, tok_path = self._ensure_weights(model_dir, tokenizer_dir, repo)
        logger.info("加载 Kronos 模型 %s（%s）", self._variant, model_path)
        model = Kronos.from_pretrained(model_path)
        tokenizer = KronosTokenizer.from_pretrained(tok_path)
        device = self._device
        if device is None:
            # 启动时检测：主机有 GPU 则 GPU 推理，否则 CPU 回退
            import torch

            device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info("推理设备: %s", device)
        self._predictor = KronosPredictor(
            model, tokenizer, device=device, max_context=max_context
        )
        return self._predictor

    def predict_closes(self, df: pd.DataFrame, sample_count: int = 20) -> list[float]:
        """对 df 的最后一段历史做 sample_count 次独立采样，返回每次的预测 close。

        便捷入口：预测目标 = 最后闭合 K 线 + 一个步长（pred_len=1）。
        """
        return self.predict_targets(df, sample_count, pred_len=1)

    def predict_targets(
        self,
        df: pd.DataFrame,
        sample_count: int = 20,
        pred_len: int = 1,
        y_timestamps: list | None = None,
    ) -> list[float]:
        """对 df 做 sample_count 次独立采样，返回每次的预测 close。

        - y_timestamps: 预测目标时间戳序列（默认 = 最后闭合 K 线 + 步长，pred_len=1）
        - pred_len > 1 时返回最后一个目标步的预测值（多步预测取终点）
        - 回测/在线共用此公开接口，避免推理参数（T/top_p/sample_count）在多处复刻
        """
        from pmbot.variant_map import VARIANT_CONTEXT

        predictor = self._load()
        max_context = VARIANT_CONTEXT[self._variant]
        x_df = df.tail(max_context).reset_index(drop=True)
        x_ts = pd.to_datetime(x_df["timestamp"], unit="ms")
        last_ts = x_ts.iloc[-1]
        if y_timestamps is None:
            step = int(x_df["timestamp"].astype(int).diff().dropna().median())
            y_ts = pd.Series([last_ts + pd.Timedelta(milliseconds=step)])
        else:
            y_ts = pd.Series(pd.to_datetime(y_timestamps, unit="ms"))
        closes = []
        # 批量采样：sample_count 是 vendor 的 batch 维度（向量化并行生成），
        # 一次调用替代串行 N 次——GPU 上提速一个数量级
        pred = predictor.predict(
            df=x_df,
            x_timestamp=x_ts,
            y_timestamp=y_ts,
            pred_len=pred_len,
            T=1.0,
            top_p=0.9,
            sample_count=sample_count,
            verbose=False,
            return_all=True,  # 保留全部独立样本（vendor 默认对 batch 求均值）
        )
        closes = [float(v) for v in pred["close"].tail(sample_count)]
        return closes
