# ADR-0005: 运行快照组装留在 save_status（不抽独立模块）

- 状态：已接受
- 日期：2026-09-11

## 背景

架构审查（improve-codebase-architecture）重提：`TradingLoop.save_status` 组装
运行快照（策略 `status_text` + WS 连接状态）穿透接缝，建议抽「状态快照」模块；
同批还标出它两处防御性 `getattr`。

ADR-0002 已裁定「TradeState 的面板字段不拆分」，但未覆盖「是否把组装抽成模块」。

## 决策

不抽模块。组装留在 `save_status`，同时删除两处多余防御：

1. `getattr(self.strategy, "status_text", None)` → `self.strategy.status_text()`：
   `Strategy.status_text()` 基类已声明并默认返回 None（`strategy.py`），能力有保证，
   无需鸭子类型探测。
2. `getattr(self, "_ticker", None) is not None` → `self._ticker is not None`：
   `_ticker` 在 `__init__` 必存在（可为 None），与同类其它检查（事件等待 / 价格读取）
   保持一致。

## 后果

- 组装只有一个消费方（`save_status`），抽模块是 pass-through extraction——删除测试
  不过关（删掉模块复杂度不重现），故不做。
- 真实摩擦是「接口已保证却仍防御」的噪音，已就地清除，接缝保持诚实。
- 未来若出现**多个展示源**（如 Web + TUI 需不同快照），按 ADR-0002 的指示在
  StateStore 之上建立拆分接缝，而非抽 `save_status`。
