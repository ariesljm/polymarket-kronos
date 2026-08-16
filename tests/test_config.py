"""配置模块测试：加载与校验。

测试接缝：load_config 公共接口 —— 给定 YAML 内容/路径，期望得到校验后的配置对象或明确报错。
"""

import pytest

from pmbot.config import ConfigError, load_config


def write_config(tmp_path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


DEFAULT_CFG = """
strategy: kronos
kronos:
  symbols: [BTC]
"""


def test_load_valid_config(tmp_path):
    cfg = load_config(write_config(tmp_path, DEFAULT_CFG))
    assert cfg.strategy == "kronos"
    assert cfg.symbols == ["BTC"]


def test_missing_fields_get_defaults(tmp_path):
    cfg = load_config(write_config(tmp_path, DEFAULT_CFG))
    assert cfg.amount_per_trade == 1
    assert cfg.p_up_buy == 0.60
    assert cfg.p_down_buy == 0.40
    assert cfg.cancel_before_end_sec == 180
    assert cfg.take_profit == 0.30
    assert cfg.take_profit_max == 0.95
    assert cfg.stop_loss == 0.20
    assert cfg.time_stop_min == 10
    assert cfg.max_consecutive_losses == 10
    assert cfg.max_daily_loss == 10
    assert cfg.max_klines == 2048  # 默认跟随 model.variant（kronos-mini）
    assert cfg.model_variant == "kronos-mini"
    assert cfg.sample_count == 20


def test_custom_values_override_defaults(tmp_path):
    text = """
strategy: kronos
kronos:
  symbols: [BTC, ETH]
  amount_per_trade: 5
  p_up_buy: 0.55
"""
    cfg = load_config(write_config(tmp_path, text))
    assert cfg.symbols == ["BTC", "ETH"]
    assert cfg.amount_per_trade == 5
    assert cfg.p_up_buy == 0.55


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_unknown_strategy_raises(tmp_path):
    text = """
strategy: quantum-ai
quantum-ai: {}
"""
    with pytest.raises(ConfigError, match="strategy"):
        load_config(write_config(tmp_path, text))


def test_empty_symbols_raises(tmp_path):
    text = """
strategy: kronos
kronos:
  symbols: []
"""
    with pytest.raises(ConfigError, match="symbols"):
        load_config(write_config(tmp_path, text))


def test_non_numeric_parameter_raises_configerror(tmp_path):
    text = """
strategy: kronos
kronos:
  symbols: [BTC]
  amount_per_trade: abc
"""
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, text))

    text2 = """
strategy: kronos
kronos:
  symbols: [BTC]
  p_up_buy: not-a-number
"""
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, text2))


def test_buy_threshold_below_half_raises(tmp_path):
    """p_up_buy < 0.5 非法：买涨买跌区间会重叠（买跌阈值自动 = 1 − p_up_buy）。"""
    text = """
strategy: kronos
kronos:
  symbols: [BTC]
  p_up_buy: 0.40
"""
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, text))


def test_down_threshold_derived_from_up(tmp_path):
    """p_down_buy 自动推导 = 1 − p_up_buy（配置只留一个参数）。"""
    text = """
strategy: kronos
kronos:
  symbols: [BTC]
  p_up_buy: 0.55
"""
    cfg = load_config(write_config(tmp_path, text))
    assert cfg.p_up_buy == 0.55
    assert cfg.p_down_buy == pytest.approx(0.45)


def test_thresholds_out_of_range_raises(tmp_path):
    text = """
strategy: kronos
kronos:
  symbols: [BTC]
  p_up_buy: 1.5
"""
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, text))


def test_negative_amount_raises(tmp_path):
    text = """
strategy: kronos
kronos:
  symbols: [BTC]
  amount_per_trade: 0
"""
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, text))


def test_percent_params_validated_independently(tmp_path):
    """百分比语义：止盈/止损独立校验（stop_loss 可大于 take_profit，如 -70% 止损 +30% 止盈）。"""
    # 合法：sl=0.70 > tp=0.30（-70% 止损与 +30% 止盈并存）
    ok = """
strategy: kronos
kronos:
  symbols: [BTC]
  take_profit: 0.30
  stop_loss: 0.70
"""
    assert load_config(write_config(tmp_path, ok)).stop_loss == 0.70

    bad_tp = """
strategy: kronos
kronos:
  symbols: [BTC]
  take_profit: 1.05  # 超出 (0,1)
"""
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, bad_tp))

    bad_sl = """
strategy: kronos
kronos:
  symbols: [BTC]
  stop_loss: 1.20  # 超出 [0,1)
"""
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, bad_sl))


def test_max_klines_follows_model_variant(tmp_path):
    text = """
strategy: kronos
kronos:
  symbols: [BTC]
model:
  variant: kronos-small
"""
    cfg = load_config(write_config(tmp_path, text))
    assert cfg.model_variant == "kronos-small"
    assert cfg.max_klines == 512  # small 上下文 512


def test_max_klines_explicit_override(tmp_path):
    text = """
strategy: kronos
kronos:
  symbols: [BTC]
data:
  max_klines: 100
"""
    cfg = load_config(write_config(tmp_path, text))
    assert cfg.max_klines == 100


def test_unknown_model_variant_raises(tmp_path):
    text = """
strategy: kronos
kronos:
  symbols: [BTC]
model:
  variant: kronos-xl
"""
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, text))


def test_negative_durations_raise(tmp_path):
    text = """
strategy: kronos
kronos:
  symbols: [BTC]
  cancel_before_end_sec: 0
"""
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, text))


def test_equal_thresholds_allowed(tmp_path):
    """0.5 合法：任何偏离 0.5 的方向都下注（严格比较，恰好 0.5 跳过）。"""
    p = tmp_path / "config.yaml"
    p.write_text("""
strategy: kronos
kronos:
  p_up_buy: 0.5
""", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.p_up_buy == 0.5
    assert cfg.p_down_buy == pytest.approx(0.5)


def test_stop_loss_zero_disables_stop_loss(tmp_path):
    """stop_loss=0 合法（关闭止损）：0 <= stop_loss < 1 独立校验。"""
    text = """
strategy: kronos
kronos:
  symbols: [BTC]
  stop_loss: 0.0
"""
    cfg = load_config(write_config(tmp_path, text))
    assert cfg.stop_loss == 0.0
