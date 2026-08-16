# 07 — 5m 市场支持（interval 可配置）

**What to build:** 支持 Polymarket 5m Up/Down 市场（slug `btc-updown-5m-<ts>` 已实查确认）。
窗口步长、市场发现、K 线频率、结算对齐全部从 config 的 `market_interval` 读取（5m/15m/1h），
不再硬编码 15m。

**Blocked by:** 06 — 监控面板（复用盘口/状态显示，interval 无关）

**Status:** in-progress

- [ ] config 增加 market_interval（5m/15m/1h 校验）
- [ ] market_discovery slug 模板与窗口对齐随 interval 动态
- [ ] prediction_log evaluate 目标步长从 K 线时间差推断（消除 STEP_MS 硬编码）
- [ ] Kronos 策略/数据源/主循环步长随 interval
- [ ] 参数适配提醒（5m 窗口 300s：cancel_before_end_sec、time_stop_min 需调小）
