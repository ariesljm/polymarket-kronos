"""配置加载与校验。

唯一配置源为 config.yaml，策略参数按策略名分节（如 kronos: {...}）。
所有字段有默认值；启动时校验合法性，非法参数抛出 ConfigError。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from pmbot.variant_map import DEFAULT_VARIANT, VARIANT_CONTEXT

KNOWN_STRATEGIES = ("kronos",)

DEFAULTS: dict = {
    "symbols": ["BTC"],
    "amount_per_trade": 1,
    "p_up_buy": 0.60,
    "cancel_before_end_sec": 180,
    "exit_loss_before_end_sec": 60,  # 窗口结束前 N 秒内浮亏 → 市价离场（0 = 关闭）
    "hold_until_end_sec": 60,        # 窗口结束前 N 秒内浮盈 → 持有到结算（0 = 关闭）
    "no_entry_before_end_sec": 60,
    "take_profit": 0.30,
    "take_profit_max": 0.95,
    "stop_loss": 0.20,
    "time_stop_min": 10,
    "max_consecutive_losses": 10,
    "max_daily_loss": 10,
}


class ConfigError(Exception):
    """配置非法或无法加载。"""


@dataclass(frozen=True)
class Config:
    strategy: str
    symbols: list[str]
    market_interval: str
    amount_per_trade: float
    p_up_buy: float
    p_down_buy: float
    cancel_before_end_sec: int
    exit_loss_before_end_sec: int  # 窗口结束前 N 秒内浮亏 → 市价离场（0 = 关闭）
    hold_until_end_sec: int        # 窗口结束前 N 秒内浮盈 → 持有到结算（0 = 关闭）
    take_profit: float
    take_profit_max: float
    stop_loss: float
    time_stop_min: int
    max_consecutive_losses: int
    max_daily_loss: float
    max_klines: int
    model_variant: str
    sample_count: int
    # 窗口结束前 N 秒禁止买入（中途启动时避免窗口末仓，0 = 关闭）
    no_entry_before_end_sec: int = 0


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"配置文件不存在: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"配置文件解析失败: {e}") from e

    strategy = raw.get("strategy", "kronos")
    if strategy not in KNOWN_STRATEGIES:
        raise ConfigError(f"未知 strategy: {strategy}，可用: {KNOWN_STRATEGIES}")

    interval = str(raw.get("market_interval", "15m"))
    from pmbot.constants import step_ms_for

    step_ms_for(interval)  # 校验合法值


    # 策略分节覆盖默认值
    s = {**DEFAULTS, **raw.get(strategy, {})}

    symbols = s["symbols"]
    if not isinstance(symbols, list) or not symbols:
        raise ConfigError("symbols 必须是非空列表")

    amount = _as_float(s["amount_per_trade"], "amount_per_trade")
    if amount <= 0:
        raise ConfigError("amount_per_trade 必须 > 0")

    buy = _as_float(s["p_up_buy"], "p_up_buy")
    # 买跌阈值自动推导（镜像对称）：P(up) < 1 − p_up_buy 时买 Down
    sell = 1.0 - buy
    if not (0.5 <= buy < 1):
        raise ConfigError(
            f"p_up_buy 必须在 [0.5, 1) 之间（买跌阈值自动为 1 − p_up_buy = {sell:.2f}）"
        )

    tp = _as_float(s["take_profit"], "take_profit")
    tpm = _as_float(s.get("take_profit_max", DEFAULTS["take_profit_max"]), "take_profit_max")
    sl = _as_float(s["stop_loss"], "stop_loss")
    # 百分比语义下两个参数独立：止盈 +tp、止损 -sl（如 tp=0.30 / sl=0.70 均合法）
    if not (0 < tp < 1):
        raise ConfigError("take_profit（百分比）必须在 (0,1) 之间")
    if not (0 <= sl < 1):
        raise ConfigError("stop_loss（百分比）必须在 [0,1) 之间（0 表示关闭止损）")
    if not (0 < tpm < 1):
        raise ConfigError("take_profit_max 必须在 (0,1) 之间")

    cbe = _as_int(s["cancel_before_end_sec"], "cancel_before_end_sec")
    elbe = _as_int(s.get("exit_loss_before_end_sec", DEFAULTS["exit_loss_before_end_sec"]), "exit_loss_before_end_sec")
    hte = _as_int(s.get("hold_until_end_sec", DEFAULTS["hold_until_end_sec"]), "hold_until_end_sec")
    nebs = _as_int(s.get("no_entry_before_end_sec", DEFAULTS["no_entry_before_end_sec"]), "no_entry_before_end_sec")
    tsm = _as_int(s["time_stop_min"], "time_stop_min")
    if cbe <= 0 or elbe < 0 or hte < 0 or nebs < 0 or tsm <= 0:
        raise ConfigError(
            "cancel_before_end_sec / time_stop_min 必须 > 0；"
            "exit_loss_before_end_sec / hold_until_end_sec / "
            "no_entry_before_end_sec 必须 ≥ 0（0 表示关闭）"
        )

    mcl = _as_int(s["max_consecutive_losses"], "max_consecutive_losses")
    mdl = _as_float(s["max_daily_loss"], "max_daily_loss")
    if mcl <= 0 or mdl <= 0:
        raise ConfigError("max_consecutive_losses / max_daily_loss 必须 > 0")

    model_cfg = raw.get("model", {})
    variant = model_cfg.get("variant", DEFAULT_VARIANT)
    if variant not in VARIANT_CONTEXT:
        raise ConfigError(f"未知模型变体: {variant}，可用: {sorted(VARIANT_CONTEXT)}")
    sample_count = _as_int(model_cfg.get("sample_count", 20), "model.sample_count")
    if sample_count <= 0:
        raise ConfigError("model.sample_count 必须 > 0")

    # 数据拉取/存储数量跟随模型上下文长度（mini=2048, small/base=512），可显式覆盖
    data_cfg = raw.get("data") or {}
    mk = _as_int(data_cfg.get("max_klines", VARIANT_CONTEXT[variant]), "max_klines")
    if mk <= 0:
        raise ConfigError("max_klines 必须 > 0")

    return Config(
        strategy=strategy,
        symbols=list(symbols),
        market_interval=interval,
        amount_per_trade=amount,
        p_up_buy=buy,
        p_down_buy=sell,
        cancel_before_end_sec=cbe,
        exit_loss_before_end_sec=elbe,
        hold_until_end_sec=hte,
        no_entry_before_end_sec=nebs,
        take_profit=tp,
        take_profit_max=tpm,
        stop_loss=sl,
        time_stop_min=tsm,
        max_consecutive_losses=mcl,
        max_daily_loss=mdl,
        max_klines=mk,
        model_variant=variant,
        sample_count=sample_count,
    )


def _as_float(value, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{name} 必须是数字，实际: {value!r}") from e


def _as_int(value, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{name} 必须是整数，实际: {value!r}") from e
