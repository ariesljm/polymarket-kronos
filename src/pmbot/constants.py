"""共享时序常量。"""

# 多 interval 支持：窗口步长与 Binance K 线频率对齐（config market_interval 指定）
INTERVAL_STEP_MS = {
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
}

# 兼容旧引用：默认 15m
STEP_MS = INTERVAL_STEP_MS["15m"]

# 窗口长度（秒）
WINDOW_SECONDS = STEP_MS // 1000


def step_ms_for(interval: str) -> int:
    """interval 字符串 → 步长毫秒；未知值抛 ValueError。"""
    try:
        return INTERVAL_STEP_MS[interval]
    except KeyError:
        raise ValueError(f"未知 interval: {interval}，可用: {sorted(INTERVAL_STEP_MS)}")


def window_start_sec(now_sec: int, step_sec: int) -> int:
    """按窗口步长对齐的窗口起始（Unix 秒，UTC 边界）。"""
    return (now_sec // step_sec) * step_sec


def window_end_sec(now_sec: int, step_sec: int) -> int:
    """当前窗口结束（Unix 秒）＝ 起始 + 步长。"""
    return window_start_sec(now_sec, step_sec) + step_sec
