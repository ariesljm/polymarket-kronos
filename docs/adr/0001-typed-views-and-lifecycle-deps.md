# ADR-0001: 决策视图类型化 + 生命周期窄接口

- 状态：已接受
- 日期：2026-08-15

## 背景

架构审查发现两处摩擦：

1. 决策引擎 `engine.decide` 与监控面板以裸 dict 传递状态/市场视图，魔法字符串键
   （`"consecutive_losses"`、`"remaining_sec"` 等）横跨 main_loop 构造、引擎消费、
   monitor 读取、status.json 序列化 4 处手工维护，改键名无静态检查。
2. `MarketLifecycle` 持有 TradingLoop 整体引用并调用其 8 个私有成员
   （`loop._decide/_execute/_build_view/...`），双向依赖、私有方法互穿，测试只能
   走完整集成路径。

## 决策

1. **视图类型化**：新增 `StateView` / `MarketView` / `PendingOrder`（`Position` 迁入
   `types.py`），成为决策引擎输入契约；`monitor.build_view` 改收 `TradeState`，
   魔法键只存在于 StateStore 边界转换。
2. **生命周期窄接口**：`MarketLifecycle` 依赖 `LifecycleDeps` Protocol（state /
   strategy / executor + `refresh_pending` / `build_view` / `decide` / `execute` /
   `save_status` 五个公开接缝方法），不再持有引擎引用；TradingLoop 对应方法去下划线
   成为正式接缝。

## 后果

- 测试可直接注入 fake deps 而非构造完整 TradingLoop；类型错误在构造期暴露。
- 主循环与生命周期的职责边界固化：引擎只管跨窗口关注点，生命周期只管单窗口逻辑。
- 未来加第二个策略/入口时，视图类型与接缝方法即为契约，禁止回退到私有方法穿透。
