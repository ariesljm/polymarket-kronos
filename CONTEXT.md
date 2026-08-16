# CONTEXT: polymarket-kronos

Polymarket 加密货币涨跌（Up/Down）预测交易机器人：Kronos 模型推理信号 → 决策引擎 → CLOB 下单，5m/15m/1h 窗口对齐，dry-run 默认安全模式。

## Glossary

### 交易领域

- **窗口（window）** — 按 interval 对齐 UTC 边界的时间槽（5m=300s / 15m=900s / 1h=3600s），每个窗口对应一个 Polymarket Up/Down 市场。对齐计算统一走 `constants.window_start_sec / window_end_sec`。
- **生命周期（lifecycle）** — 单个市场窗口的 `MarketLifecycle` 状态机：INIT（信号生成）→ RUNNING（成交检测/决策/执行）→ STOPPING → DONE。依赖经 `LifecycleDeps` 窄接口注入，**不持有引擎引用**。
- **信号（signal）** — `Strategy.generate_signal` 的输出：`Signal(direction, p_up)`。方向 UP/DOWN/SKIP，p_up 为预测上涨概率 [0,1]。入参 `SignalContext`（TypedDict，`now_ms` 可选，缺失由策略自行取当前时间）。
- **持仓（position）** — `Position`：入场方向/价、股数、入场时窗口剩余秒、所属窗口。窗口结束必结算，不跨窗口。
- **挂单（pending order）** — `PendingOrder`：未成交限价单（方向/价/股数/order_id）。与"持仓"是互斥状态。当前策略市价入场不产生新挂单；pending 仅保留兼容旧状态恢复/WS 成交确认路径。
- **决策（Action）** — 决策引擎输出：`PLACE_MARKET / CANCEL / SELL / SKIP / PAUSE`，含 reason（take_profit / stop_loss / time_stop / settle 等）。
- **熔断（circuit breaker）** — 连亏 N 笔或单日亏 N USDC 自动暂停；人工改 status.json `paused=false` 恢复并清零计数。
- **引擎级兜底（engine-level fallback）** — TradingLoop 负责的跨窗口关注点（日界/熔断/窗口切换/跨窗口撤单/结算兜底），与单窗口生命周期逻辑分离。

### 决策引擎与视图

- **决策引擎（engine.decide）** — 纯函数 `decide(config, state, market, signal) → Action`，不碰 IO。测试接缝：注入 `StateView/MarketView` 纯数据。
- **状态视图（StateView）** — 决策输入：连亏/日亏/本窗口已下注/暂停。
- **市场视图（MarketView）** — 决策输入：窗口剩余秒、目标方向 best ask/bid、当前持仓、挂单。
- **接缝方法（seam methods）** — TradingLoop 上生命周期消费的公开方法：`refresh_pending / build_view / decide / execute / save_status`。不要改回下划线私有穿透。
- **状态（state）** — `TradeState` 领域对象：只含交易语义（窗口/持仓/挂单/熔断/运行快照字段）。**不做序列化**——持久化归 StateStore。
- **状态存储（StateStore）** — TradeState 的持久化：status.json 快照 + trades.csv 交易日志。序列化只在进程边界（run/monitor）发生。

### 市场接入

- **执行器（ClobExecutor）** — Polymarket CLOB 下单/撤单/卖出/盘口查询。dry-run 只打印指令；公开面含 `fetch_book`（无认证盘口查询）/ `attach_sampler` / `api_auth`（凭证唯一解析方）。采样器依赖经 `SamplerProto` 窄接口注入。
- **盘口采样器（BookSampler）** — 高频盘口 WS 线程（REST 兜底），内存快照供执行器报价，book.json 落盘供面板 1s 级展示。
- **用户流（UserStream）** — 认证 WS（订单/成交推送）→ 事件队列，主循环 tick drain。无凭证时空转。
- **可重连 WS 线程（ReconnectingWsThread）** — 两个 WS 线程的公共骨架（指数退避重连/PING/停止/4 钩子）。新 WS 流应继承它而非复制样板。
- **盘口定价（weighted_price / best_price）** — `book_price.py` 纯函数，单一事实源：按可成交量加权均价，**流动性不足返回 None**（宁缺毋滥，不显示误导价）。执行器与面板落盘必须共用，禁止本地复刻。
- **市场发现（MarketDiscovery）** — 定位当前窗口的 Up/Down 市场（gamma-api，slug 模式）。
- **数据源（BinanceDataSource / KlineStore）** — Binance 镜像 K 线拉取 + 本地 CSV 滚动存储，增量/去重/裁剪。单批拉取统一走 `fetch_klines_batch`（在线与离线回测共用，禁止复刻）。

### 展示与验证

- **监控面板（monitor）** — 只读 TUI，独立进程：从 `StateStore.load()` 读状态、trades.csv、PredictionLog、book.json 构建视图。任何异常只显示不崩溃。`build_view` 的展示配置（模型变体/阈值/窗口长度/摘要/止盈止损/运行时长）经 `PanelConfig` 打包注入。
- **预测日志（PredictionLog）** — 预测方向准确率记录（与交易盈亏解耦），`predictions_<symbol>.csv`。
- **回测（backtest）** — 离线方向准确率评估，走 `KronosPredictorClient.predict_targets` 公开接口（禁止复刻推理循环或调用 `_load()`）。

## 架构约定

- **决策/视图类型化**：引擎与主循环之间传递 `StateView/MarketView` 类型，不传裸 dict（见 ADR-0001）。
- **窄接口注入**：跨模块依赖用 Protocol 收缩到最小能力面（`LifecycleDeps`/`SamplerProto`/`CancelExecutor`），禁止持有对方整体引用或穿透私有成员。
- **序列化只在边界**：领域内不裸 dict 传递状态；JSON 键名只存在于 StateStore 与边界转换。
- **落盘原子写**：所有 CSV/JSON 状态文件经 `fileio.atomic_write_text`（tmp+replace）写入，禁止直接 `write_text` 覆盖（防半写/崩溃损坏，见 ADR-0003）。
- **dry-run 语义**：模拟模式不触碰真实认证/订单；盘口查询走公开端点（无凭证也能跑）。
- **避免的词汇**：不要用"服务/组件/API/边界"称呼上述模块；领域词见本词汇表（如"生命周期"而非"生命周期服务"）。
