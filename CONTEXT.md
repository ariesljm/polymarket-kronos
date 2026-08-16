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
- **平仓（close）** — `TradingLoop._close_position` 统一卖出/结算两条路径：余额差 PnL（exit_balance − entry_balance，含滑点/手续费）优先，余额查询失败回退理论价差；兑现持仓、更新熔断计数、记 trades.csv。`entry_balance` 取**买入前**余额（净盈亏基准：结算所得 − 买入成本含费）。
- **结算等待期（settle pending）** — 持仓窗口已结束后 gamma 结算未完成的等待期：**不交易只等结算**（build_view 不给 bid → 决策引擎不卖）。曾因结算等待期止盈卖出失败（Polymarket 已结算 token 失效，balance 0）导致 tick 异常死循环与结算超时丢跟踪。
- **实盘数据只用实际获取值** — 持仓股数=订单详情 `size_matched`/响应 `takingAmount`；入场价=详情 `price`（纯成交价，不含费）；盈亏=余额差（实证含 ~3% taker 手续费：1 USDC 单实扣 1.0301，链上两笔转账：成交 1.0 + 费用 0.03）。API 无实际成交数据时**放弃建仓**（不盘口估算，dry-run 除外）。
- **状态存储（StateStore）** — TradeState 的持久化：status.json 快照 + trades.csv 交易日志。序列化只在进程边界（run/monitor）发生。
- **控制指令（control）** — 面板 ↔ 主循环指令通道（control.json 原子写 + 读删）：resume/reset/stop；start 为进程级操作不走本通道。reset 语义收敛于 `control.reset_runtime`（主循环与面板共用同一文件删除清单：status/trades/K线/预测记录；实盘拒绝 reset 保护在各调用方）。

### 市场接入

- **执行器（OrderPlacer）** — 主循环/生命周期下单依赖的协议：市价/限价/撤单/盘口/余额/凭证。接口按消费角色拆窄：`MarketBook`（盘口）、`TradeExecutor`（下单）、`WalletView`（钱包）、`AuthSource`（凭证）；OrderPlacer 是四者并集的组合面。两个适配器：`ClobExecutor`（实盘：真实下单、解析 API 成交 averagePrice/matchedAmount）与 `SimExecutor`（dry-run：打印指令、按盘口估算模拟成交；盘口/钱包/凭证面委托内部实盘实例）。限价规则校验（validate_limit_order）与成交解析（_parse_fill）为执行器内部单一事实源（禁止适配器各自复刻）。采样器依赖经 `SamplerProto` 窄接口注入。
- **钱包核对（WalletReconciler）** — 引擎 tick 的“外部世界同步”关注点（深模块）：余额定时刷新（30s 节流 + 今日盈亏基准捕获）与 Polymarket 实时持仓核对。引擎只留一行调用（wallet_sync.reconcile），规则独立可测（注入 WalletSource 窄替身）。
- **幽灵持仓（ghost position）** — 本地记录有持仓但 Polymarket 实际无该标的持仓（崩溃/强杀残留）：核对时清除并警告；**清除有宽限保护**（持仓窗口结束 + 180s 后仍无才判幽灵，防买入后 /positions 索引延迟误清）；反向（本地无但远端有）未跟踪持仓**自动接管**（slug 可解析窗口起点时，重建 position 恢复止损/结算管理；非 bot 市场格式只警告不接管）；查询失败不核对（防误清真实持仓）。
- **盘口采样器（BookSampler）** — 高频盘口 WS 线程（REST 兜底），内存快照供执行器报价，book.json 落盘供面板 1s 级展示。
- **用户流（UserStream）** — 认证 WS（订单/成交推送）→ 事件队列，主循环 tick drain。无凭证时空转。
- **可重连 WS 线程（ReconnectingWsThread）** — 两个 WS 线程的公共骨架（指数退避重连/PING/停止/4 钩子）。新 WS 流应继承它而非复制样板。
- **盘口定价（weighted_price / best_price）** — `book_price.py` 纯函数，单一事实源：按可成交量加权均价，**流动性不足返回 None**（宁缺毋滥，不显示误导价）。执行器与面板落盘必须共用，禁止本地复刻。
- **市场发现（MarketDiscovery）** — 定位当前窗口的 Up/Down 市场（gamma-api，slug 模式）。
- **数据源（BinanceDataSource / KlineStore）** — Binance 镜像 K 线拉取 + 本地 CSV 滚动存储，增量/去重/裁剪。单批拉取统一走 `fetch_klines_batch`（在线与离线回测共用，禁止复刻）。

### 展示与验证

- **监控面板（monitor）** — 只读 TUI，独立进程：从 `StateStore.load()` 读状态、trades.csv、PredictionLog、book.json 构建视图。任何异常只显示不崩溃。`build_view` 的展示配置（模型变体/阈值/窗口长度/摘要/止盈止损/运行时长）经 `PanelConfig` 打包注入。
- **展示视图（PanelView）** — `build_view` 输出的类型化视图：TUI render 属性访问（静态检查）；Web 控制台经 `asdict` 边界转换，字段名即 JSON 键名唯一出处。展示侧禁止魔法字符串键（ADR-0001 精神延伸）。
- **运行路径（RuntimePaths）** — 数据目录派生单一事实源（status/trades/log_dir/pid_file/mode）：模拟 data/、实盘 data_live/；`paths_for(live, data_dir)` 工厂。
- **协调状态（ProcessControl）** — monitor ↔ Web 控制台共享的进程协调（proc/show_tui/live/paths），替代裸 holder dict；模拟/实盘切换一次赋值（pc.paths 换新即全部跟随）。进程级操作也收敛于此：`spawn(config)`（按当前模式/数据目录拉起主循环）与 `loop_alive()`（读当前 pid 文件判存活），spawn_loop 模块函数在 paths.py，web_ui 只做 HTTP 路由。
- **预测日志（PredictionLog）** — 预测方向准确率记录（与交易盈亏解耦），`predictions_<symbol>.csv`。
- **回测（backtest）** — 离线方向准确率评估，走 `KronosPredictorClient.predict_targets` 公开接口（禁止复刻推理循环或调用 `_load()`）。

## 架构约定

- **决策/视图类型化**：引擎与主循环之间传递 `StateView/MarketView` 类型，不传裸 dict（见 ADR-0001）。
- **窄接口注入**：跨模块依赖用 Protocol 收缩到最小能力面（`LifecycleDeps`/`SamplerProto`/`CancelExecutor`），禁止持有对方整体引用或穿透私有成员。
- **序列化只在边界**：领域内不裸 dict 传递状态；JSON 键名只存在于 StateStore 与边界转换（含 PanelView.asdict）。
- **落盘原子写**：所有 CSV/JSON 状态文件经 `fileio.atomic_write_text`（tmp+replace）写入，禁止直接 `write_text` 覆盖（防半写/崩溃损坏，见 ADR-0003）。
- **dry-run 语义**：模拟模式不触碰真实认证/订单；盘口查询走公开端点（无凭证也能跑）。
- **避免的词汇**：不要用"服务/组件/API/边界"称呼上述模块；领域词见本词汇表（如"生命周期"而非"生命周期服务"）。
