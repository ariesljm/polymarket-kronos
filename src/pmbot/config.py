"""配置加载与校验。

唯一配置源为 config.yaml：顶层为通用（引擎/执行/风控）参数，
策略专属参数按策略名分节（如 momentum: {...}）覆盖同名通用默认值。
所有字段有默认值；启动时校验合法性，非法参数抛出 ConfigError。

新增策略：在 src/pmbot/strategies/ 写策略类 + @register("名字")，
config.yaml 顶层 strategy: 名字 即可切换（专属参数放同名分节）——
config.py 本身无需改动。
"""

from __future__ import annotations

import typing
from dataclasses import dataclass
from pathlib import Path

import yaml

T = typing.TypeVar("T", float, int)

KNOWN_STRATEGIES = ("momentum",)

# 引擎参数白名单（Config → EngineConfig 自动映射字段名；与 EngineConfig 字段一致，
# 加新引擎参数 = EngineConfig 加字段 + 本白名单加名，不再手抄构造映射）
_ENGINE_FIELDS = (
    "amount_per_trade", "p_up_buy", "p_down_buy", "cancel_before_end_sec",
    "exit_loss_before_end_sec", "hold_until_end_sec", "take_profit",
    "stop_loss", "max_consecutive_losses", "max_daily_loss",
    "no_entry_before_end_sec", "open_delay_sec", "contradiction_skip_pct",
    "max_entry_price", "min_entry_price", "taker_fee_pct",
)

DEFAULTS: dict = {
    "symbols": ["BTC"],
    "amount_per_trade": 1,
    "p_up_buy": 0.60,
    "cancel_before_end_sec": 180,
    "exit_loss_before_end_sec": 60,  # 窗口结束前 N 秒内浮亏 → 市价离场（0 = 关闭）
    "hold_until_end_sec": 60,        # 窗口结束前 N 秒内浮盈 → 持有到结算（0 = 关闭）
    "no_entry_before_end_sec": 60,
    "open_delay_sec": 0,          # 市场开始后 N 秒内不开仓（观察早期波动；0 = 关闭）
    "contradiction_skip_pct": 0.0,  # 信号与 Binance 实时移动矛盾超此百分比跳过入场（0 = 关闭）
    "max_entry_price": 0.0,      # 入场价上限：盘口 ask 高于此价不入场（追高无意义；0 = 关闭）
    "min_entry_price": 0.0,      # 入场价下限：盘口 ask 低于此价不入场（0 = 关闭）
    "taker_fee_pct": 0.0,        # dry-run 模拟 taker 手续费率（按成交金额；实盘 pnl=余额差已含费）
    "threshold_pct": 0.08,     # momentum 策略：Binance 相对窗口开盘穿越阈值 %%——穿越后同向入场
    # 分标的阈值机制已删除（升阈致 0 成交，见 docs/adr/0004-revert-per-symbol-threshold.md）
    "btc_contradiction_pct": 0.0,  # BTC 反向矛盾过滤阈值（0 = 关闭）
    "take_profit": 0.95,
    "stop_loss": 0.20,
    "max_consecutive_losses": 10,
    "max_daily_loss": 10,
}


class ConfigError(Exception):
    """配置非法或无法加载。"""


@dataclass(frozen=True)
class EngineConfig:
    """引擎决策所需参数窄视图（engine.decide / circuit_breaker 消费）。"""

    amount_per_trade: float
    p_up_buy: float
    p_down_buy: float
    cancel_before_end_sec: int
    exit_loss_before_end_sec: int
    hold_until_end_sec: int
    take_profit: float
    stop_loss: float
    max_consecutive_losses: int
    max_daily_loss: float
    no_entry_before_end_sec: int = 0
    open_delay_sec: int = 0
    contradiction_skip_pct: float = 0.0  # 信号与 Binance 实时移动矛盾超此百分比跳过入场（0 = 关闭）
    max_entry_price: float = 0.0  # 入场价上限：盘口 ask 高于此价不入场（0 = 关闭）
    min_entry_price: float = 0.0  # 入场价下限：盘口 ask 低于此价不入场（0 = 关闭）
    taker_fee_pct: float = 0.0    # dry-run 模拟 taker 手续费率（按成交金额；实盘 pnl=余额差已含费）


@dataclass(frozen=True)
class StrategyConfig:
    """策略所需参数窄视图（Strategy 构造消费）。"""

    market_interval: str = "5m"
    threshold_pct: float = 0.08  # momentum：穿越阈值 %%（相对窗口开盘）
    btc_contradiction_pct: float = 0.0  # BTC 反向矛盾过滤阈值（0 = 关闭）


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
    stop_loss: float
    max_consecutive_losses: int
    max_daily_loss: float
    # 窗口结束前 N 秒禁止买入（中途启动时避免窗口末仓，0 = 关闭）
    no_entry_before_end_sec: int = 0
    # 市场开始后 N 秒内不开仓（0-300，0 = 关闭）
    open_delay_sec: int = 0
    # 信号与 Binance 实时移动矛盾超此百分比跳过入场（0 = 关闭）
    contradiction_skip_pct: float = 0.0
    # 入场价上限：盘口 ask 高于此价不入场（追高无意义；0 = 关闭）
    max_entry_price: float = 0.0
    # 入场价下限：盘口 ask 低于此价不入场（0 = 关闭）
    min_entry_price: float = 0.0
    # dry-run 模拟 taker 手续费率（按成交金额；实盘 pnl=余额差已含费）
    taker_fee_pct: float = 0.0
    # momentum 策略：Binance 相对窗口开盘穿越阈值 %%（穿越后同向入场）
    threshold_pct: float = 0.08
    # BTC 反向矛盾过滤阈值 %%（0 = 关闭；标的穿越方向与 BTC 反向时跳过入场）
    btc_contradiction_pct: float = 0.0

    def to_engine_config(self) -> EngineConfig:
        """引擎窄视图派生：字段与 EngineConfig 一一对应（白名单自动映射，

        加新引擎参数 = EngineConfig 加字段 + 白名单加名，不再手抄构造映射；
        字段名在 Config 与 EngineConfig 间必须一致（getattr 取同名属性）。
        """
        return EngineConfig(**{f: getattr(self, f) for f in _ENGINE_FIELDS})

    def to_strategy_config(self) -> StrategyConfig:
        return StrategyConfig(
            market_interval=self.market_interval,
            threshold_pct=self.threshold_pct,
            btc_contradiction_pct=self.btc_contradiction_pct,
        )


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"配置文件不存在: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"配置文件解析失败: {e}") from e

    strategy = raw.get("strategy", "momentum")
    known = _known_strategies()
    if known and strategy not in known:
        raise ConfigError(f"未知 strategy: {strategy}，可用: {known}")

    interval = str(raw.get("market_interval", "5m"))
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
    sl = _as_float(s["stop_loss"], "stop_loss")
    # 止盈为绝对价（0.95 = 价格到 0.95 止盈，接近持有到结算）；止损为相对百分比
    if not (0 < tp < 1):
        raise ConfigError("take_profit（绝对止盈价）必须在 (0,1) 之间")
    if not (0 <= sl < 1):
        raise ConfigError("stop_loss（百分比）必须在 [0,1) 之间（0 表示关闭止损）")

    cbe = _field(s, "cancel_before_end_sec", _as_int, lo=1)
    elbe = _field(s, "exit_loss_before_end_sec", _as_int, lo=0, hint="0 表示关闭")
    hte = _field(s, "hold_until_end_sec", _as_int, lo=0, hint="0 表示关闭")
    nebs = _field(s, "no_entry_before_end_sec", _as_int, lo=0, hint="0 表示关闭")
    ods = _field(s, "open_delay_sec", _as_int, lo=0, hi=300, hint="0 表示关闭开仓延迟")

    csp = _field(s, "contradiction_skip_pct", _as_float, lo=0, hint="0 表示关闭方向一致性过滤")

    mep = _field(s, "max_entry_price", _as_float, lo=0, hint="0 表示关闭入场价上限")

    miep = _field(s, "min_entry_price", _as_float, lo=0, hint="0 表示关闭入场价下限")
    if 0 < miep and 0 < mep and miep >= mep:
        raise ConfigError("min_entry_price 必须 < max_entry_price")

    tfp = _field(s, "taker_fee_pct", _as_float, lo=0, hi=1, hi_excl=True, hint="0 表示不模拟手续费")

    thr = _field(s, "threshold_pct", _as_float, lo=0, lo_excl=True,
                 hint="momentum 穿越阈值，如 0.08 = 0.08%")

    btc_cp = _field(s, "btc_contradiction_pct", _as_float, lo=0,
                    hint="0 表示关闭 BTC 反向矛盾过滤")

    mcl = _field(s, "max_consecutive_losses", _as_int, lo=1)
    mdl = _field(s, "max_daily_loss", _as_float, lo=0, lo_excl=True)

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
        open_delay_sec=ods,
        take_profit=tp,
        stop_loss=sl,
        max_consecutive_losses=mcl,
        max_daily_loss=mdl,
        contradiction_skip_pct=csp,
        max_entry_price=mep,
        min_entry_price=miep,
        taker_fee_pct=tfp,
        threshold_pct=thr,
        btc_contradiction_pct=btc_cp,
    )


def _known_strategies() -> tuple[str, ...]:
    """已注册策略名单：registry 单一事实源（延迟导入避免 config↔策略循环）。"""
    import pmbot.strategies  # noqa: F401  触发各策略 @register

    from pmbot.strategy import strategies

    return strategies()


def _as_float(value: object, name: str) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{name} 必须是数字，实际: {value!r}") from e


def _as_int(value: object, name: str) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{name} 必须是整数，实际: {value!r}") from e


def _interval_desc(lo: float | None, hi: float | None,
                   lo_excl: bool, hi_excl: bool) -> str:
    """范围描述（错误消息用）：纯 Python 无需多态，直接字符串化边界。"""
    if lo is not None and hi is not None:
        l, r = ("(" if lo_excl else "["), (")" if hi_excl else "]")
        return f"在 {l}{lo}, {hi}{r} 之间"
    if lo is not None:
        return ("必须 > " if lo_excl else "必须 ≥ ") + str(lo)
    return "必须 ≤ " + str(hi) if hi is not None else ""


def _field(s: dict, key: str,
           cast: typing.Callable[[object, str], T],
           *, lo: float | None = None, hi: float | None = None,
           lo_excl: bool = False, hi_excl: bool = False,
           hint: str = "") -> T:
    """合并配置取值：类型转换 + 范围校验，错误消息统一带字段名。

    收敛重复的『_as_X(s.get(key, DEFAULTS[key]), key) + 范围 if + raise』骨架。
    """
    v = cast(s.get(key, DEFAULTS[key]), key)  # type: ignore[arg-type]
    outside = (lo is not None and (v <= lo if lo_excl else v < lo)) or \
              (hi is not None and (v >= hi if hi_excl else v > hi))
    if outside:
        msg = f"{key} {_interval_desc(lo, hi, lo_excl, hi_excl)}"
        raise ConfigError(msg + (f"（{hint}）" if hint else ""))
    return v