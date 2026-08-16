# 08a — MarketLifecycle 状态机重构

**What to build:** 参考 KaustubhPatange/polymarket-trade-engine 的 EarlyBird + MarketLifecycle
架构，把"单窗口编排"从 TradingLoop.tick() 提取为显式生命周期状态机：

```
INIT → RUNNING → STOPPING → DONE
```

- INIT: 市场发现、信号生成（提前入场由 08b 做）
- RUNNING: 成交检测、持仓管理、撤单、决策执行
- STOPPING: 撤剩余买单、结算持仓、记录 PnL
- DONE: 归档，交还引擎

TradingLoop 变为编排器（窗口切换时 stop 旧 → start 新）。
**行为不变**：重构后 160 测试全绿，dry-run 逻辑不变。

**Blocked by:** 07 — 5m 支持（interval 动态化已在 07 完成）

**Status:** in-progress

- [ ] MarketLifecycle 类：Phase 状态机 + start/tick/stop
- [ ] TradingLoop 改为 lifecycle 编排（窗口切换、熔断、跨窗口撤单保留）
- [ ] 状态持久化时机不变（每 tick 落盘）
- [ ] 全量测试通过，dry-run 行为与重构前一致
