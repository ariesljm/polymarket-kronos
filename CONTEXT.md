# CONTEXT: polymarket-kronos

Polymarket 加密货币涨跌（Up/Down）策略交易框架：策略信号（Strategy 抽象）→ 决策引擎 → CLOB 下单，5m/15m/1h 窗口对齐，dry-run 默认安全模式。内置策略：momentum（动量/Breakout 穿越入场，见 docs/reports/momentum_strategy.md）。Kronos 方向预测经全方位证伪（AUC≈0.51/50%）已移除，方向预测路线不再回退。

## Glossary

### 交易领域

- **窗口（window）** — 按 interval 对齐 UTC 边界的时间槽（5m=300s / 15m=900s / 1h=3600s），每个窗口对应一个 Polymarket Up/Down 市场。对齐计算统一走 `constants.window_start_sec / window_end_sec`。
- **生命周期（lifecycle）** — 单个市场窗口的 `MarketLifecycle` 状态机：INIT（信号生成）→ RUNNING（成交检测/决策/执行）→ DONE（STOPPING 中间态已移除，stop() 直接置 DONE）。依赖经 `LifecycleDeps` 窄接口注入，**不持有引擎引用**；直构测试锚点见 `tests/test_market_lifecycle.py`（fake deps 验证窗口内 5 接缝顺序与刷新门控）。
- **信号（signal）** — `Strategy.generate_signal` 的输出：`Signal(direction, p_up)`。方向 UP/DOWN/SKIP，p_up 为预测上涨概率 [0,1]。入参 `SignalContext`（TypedDict，`now_ms` 可选，缺失由策略自行取当前时间）。
- **持仓（position）** — `Position`：入场方向/价、股数、入场时窗口剩余秒、所属窗口。窗口结束必结算，不跨窗口。
- **挂单（pending order）** — `PendingOrder`：未成交限价单（方向/价/股数/order_id）。与"持仓"是互斥状态。当前策略市价入场不产生新挂单；pending 仅保留兼容旧状态恢复/WS 成交确认路径。
- **决策（Action）** — 决策引擎输出：`PLACE_MARKET / CANCEL / SELL / SKIP / PAUSE`，含 reason（take_profit / stop_loss / time_stop / settle 等）。
- **熔断（circuit breaker）** — 连亏 N 笔或单日亏 N USDC 自动暂停；人工改 status.json `paused=false` 恢复并清零计数。判定/文案单一事实源 `engine.circuit_breaker`（纯函数，(reason_key, 文案) | None）：tick 与决策引擎共用，`ExecutionDispatcher._exec_pause` 也消费同一 `BREAKER_MESSAGES`（写入 pause_reason 不再自拼短文案）。恢复侧对称事实源 `TradeState.clear_breaker()`：resume 指令分支与人工恢复分支共用清零清单（paused/was_paused/连亏/日亏/pause_reason）。
- **引擎级兜底（engine-level fallback）** — TradingLoop 负责的跨窗口关注点（日界/熔断/窗口切换/跨窗口撤单/结算兜底），与单窗口生命周期逻辑分离。结算兜底序列收敛于 `_settle_expired(now_sec, defer_only=)`（defer 转待结算槽 + Settler 推进一处定义）：tick 前置检查走完整推进，窗口切换分支 defer_only=True 只转槽不重复查询（步骤1 已推进过同一 settle_pending）。单窗口的成交检测/决策/执行/落盘委托 ExecutionDispatcher（执行分派器深模块），TradingLoop 退化为纯编排器。

### 决策引擎与视图

- **决策引擎（engine.decide）** — 纯函数 `decide(config, state, market, signal, now_sec=None) → Action`，不碰 IO。测试接缝：注入 `StateView/MarketView` 纯数据。config 入参为 `EngineConfig` 窄视图（引擎决策参数，由 Config 字段白名单自动映射），不接整 `Config`——消费方表达真实依赖。建仓冷却（盘口无报价 N 秒内不再决策）经 `StateView.retry_until_sec` 注入、now 由调用方传入——决策规则不再散落编排层。
- **入场价闸门（entry_gate）** — 入场价区间 [下限, 上限] 判定的单一事实源：`resolve(config_min, config_max, override) → EntryGate`（上限经 auto_tune 覆盖、下限直取 config）→ `EntryGate.check(ask) → 越界原因 | None`。`engine.decide` 初判与 `ExecutionDispatcher` 执行层二次校验共用同一接口：决策与下单之间盘口被 WS 线程异步更新（穿越后秒级极化），两处须按同一区间判定——曾各自实现上限比较（engine 另含下限、执行层无下限），`override if override else config` 回落散在两处。闸门无 IO 无状态，纯函数注入 ask 直接测。
- **自适应调参（auto_tune）** — 分价格带按真实交易 EV 生成引擎参数覆盖 delta（`AutoTuneOverride`：字段 None = 不覆盖、非 None = 覆盖值）。`tune(trades, min_band_n, config_max_entry, config_take_profit)` 只收窄不放宽（样本足且累计 EV ≤ 0 的带下界为新上限；不低于 config 即置 None 不产生无效覆盖）+ 门槛保护（全局样本 < min_band_n → 空 delta）；低带（≤0.45）胜率 > 55% 且严格上调才覆盖止盈（0.99）。回落收口在解析层：上限在 `entry_gate.resolve`（engine 与执行层两处共用）、止盈在 `engine._manage_position`（单一消费方内联）；消费方不自行 if/else。main_loop 每 60s 重算，日志按 delta 是否非空报「调整参数」否则「评估(维持配置)」。
- **状态视图（StateView）** — 决策输入：连亏/日亏/本窗口已下注/暂停。
- **市场视图（MarketView）** — 决策输入：窗口剩余秒、目标方向 best ask/bid、当前持仓、挂单。
- **接缝方法（seam methods）** — TradingLoop 上生命周期消费的公开方法：`refresh_pending / build_view / decide / execute / save_status`。不要改回下划线私有穿透。`execute` 与 `refresh_pending` 委托 `ExecutionDispatcher`（执行分派器），但接缝仍在 TradingLoop。
- **状态（state）** — `TradeState` 领域对象：只含交易语义（窗口/持仓/挂单/熔断/运行快照字段）。**不做序列化**——持久化归 StateStore。
- **执行分派器（ExecutionDispatcher）** — 从 TradingLoop 提取的动作执行与挂单成交检测深模块；TradeState 经 getter 注入（不持引用副本），reset 重建状态自动跟随、无需手工回写同步：`execute`（动作分派→`_exec_place_market/_exec_sell/_exec_cancel/_exec_pause`）、`refresh_pending`（挂单成交检测）、`close_position`/`abandon_position`（平仓与结算回调）、`fill_pending`（挂单成交）。TradingLoop tick 编排 + LifecycleDeps 接缝 + 窗口切换/熔断/控制指令仍在主循环；执行 IO + 平仓记账收敛于此。
- **平仓（close）** — `ExecutionDispatcher.close_position` 统一卖出/结算两条路径：余额差 PnL（exit_balance − entry_balance，含滑点/手续费）优先，余额查询失败回退理论价差；兑现持仓、更新熔断计数、记 trades.csv。`entry_balance` 取**买入前**余额（净盈亏基准：结算所得 − 买入成本含费）。
- **结算等待期（settle pending）** — 持仓窗口已结束后 gamma 结算未完成的等待期：**不交易只等结算**（build_view 不给 bid → 决策引擎不卖）。曾因结算等待期止盈卖出失败（Polymarket 已结算 token 失效，balance 0）导致 tick 异常死循环与结算超时丢跟踪。
- **实盘数据只用实际获取值** — 持仓股数=订单详情 `size_matched`/响应 `takingAmount`；入场价=详情 `price`（纯成交价，不含费）；盈亏=余额差（实证含 ~3% taker 手续费：1 USDC 单实扣 1.0301，链上两笔转账：成交 1.0 + 费用 0.03）。API 无实际成交数据时**放弃建仓**（不盘口估算，dry-run 除外）。
- **状态存储（StateStore）** — TradeState 的持久化：status.json 快照 + trades.csv 交易日志。序列化只在进程边界（run/monitor）发生。
- **控制指令（control）** — 面板 ↔ 主循环指令通道（control.json 原子写 + 读删）：resume/reset/stop；start 为进程级操作不走本通道。reset 语义收敛于 `control.reset_runtime`（主循环与面板共用同一文件删除清单：status/trades/K线/预测记录；实盘拒绝 reset 保护在各调用方）。

### 市场接入

- **执行器（OrderPlacer）** — 主循环/生命周期下单依赖的协议：市价/限价/撤单/盘口/余额/凭证。接口按消费角色拆窄：`MarketBook`（盘口）、`TradeExecutor`（下单）、`WalletView`（钱包）、`AuthSource`（凭证）；OrderPlacer 是四者并集的组合面。窄接口 Protocols 与限价规则（`validate_limit_order`/`min_shares_for_price`）收敛于 `executor_protocols.py`（单一事实源），两个适配器（`ClobExecutor` 实盘 / `SimExecutor` 模拟）在 `clob_executor.py`。限价规则校验与成交解析（`_parse_fill`）为执行器内部单一事实源（禁止适配器各自复刻）。成交经 seam 用类型化契约 `Fill`（order_id/avg_price/filled_size）传递——曾用 dict 魔法键跨两适配器与引擎五处手抄；实盘“成交缺实际数据→放弃建仓”“卖价取不到→回退 best_bid”收进执行器成为成交语义，调用方只消费 Fill。采样器依赖经 `SamplerProto` 窄接口注入。data-api 公开端点（/positions、/activity）HTTP 样板收敛 `_data_api_get`（代理默认值/超时/错误归一一次）+ `_activity_page`（TRADE/REDEEM 分页参数化）——代理 dict 曾四处手抄。
- **钱包核对（WalletReconciler）** — 引擎 tick 的“外部世界同步”关注点（深模块）：余额定时刷新（30s 节流 + 今日盈亏基准捕获）与 Polymarket 实时持仓核对。引擎只留一行调用（wallet_sync.reconcile），规则独立可测（注入 WalletSource 窄替身）。
- **结算状态机（Settler）** — 持仓窗口结束后的结算等待/兑付深模块：市场查询（find_window/invalidate）与兑付查询（settle_proceeds）窄接口注入，平仓/丢弃回调注入；状态机 PRICE_WAIT → REDEEM_WAIT → DONE/ABANDONED 可观察；结算超时按窗口步长自适应（2×步长，下限 300s）。引擎 tick/shutdown 各一行调用（settler.should_run/settle），build_view 的“结算等待期不报价”判定与结算同源。四路分支：市场不可达（超时丢弃跟踪）/ 结算归零（输，立即按成本记账）/ 中间价（超时按当前价兜底）/ 价格就绪（赢，等 REDEEM 真实兑付）。
- **交易账本（ledger）** — 交易记录的统一读面与 schema 单一事实源：`RECORD_COLUMNS` 唯一列定义（引擎写入 TRADE_COLUMNS 与流水配对共用）；`load_records(data_dir)` 判据唯一——api_trades.csv（真实流水配对，含手续费）**存在且含数据行**优先（同步中断/半写空表头回退 trades.csv 完整业务记录），缺回退 trades.csv（引擎业务记录）。API 流水配对（`build_records`）收在本模块（曾与 trade_history 循环依赖，现 trade_history 只做增量写入）；monitor/stats/report 不再各自选文件。流水同步依赖走 `TradeHistorySource` 窄 Protocol（runtime_checkable）替代鸭子探测。
- **幽灵持仓（ghost position）** — 本地记录有持仓但 Polymarket 实际无该标的持仓（崩溃/强杀残留）：核对时清除并警告；**清除有宽限保护**（持仓窗口结束 + 180s 后仍无才判幽灵，防买入后 /positions 索引延迟误清）；反向（本地无但远端有）未跟踪持仓**自动接管**（slug 可解析窗口起点时，重建 position 恢复止损/结算管理；非 bot 市场格式只警告不接管）；查询失败不核对（防误清真实持仓）。
- **盘口采样器（BookSampler）** — 高频盘口 WS 线程（REST 兜底），内存快照供执行器报价，book.json 落盘供面板 1s 级展示。
- **用户流（UserStream）** — 认证 WS（订单/成交推送）→ 事件队列，主循环 tick drain。无凭证时空转。
- **可重连 WS 线程（ReconnectingWsThread）** — 两个 WS 线程的公共骨架（指数退避重连/心跳应答/停止/4 钩子 + 订阅集合动态推送 `_push_subscriptions`：跨线程统一 `run_coroutine_threadsafe` 调度——`loop.create_task` 从非事件循环线程调用不是线程安全的曾致丢任务窗口）。新 WS 流应继承它而非复制样板。**心跳：应答不主动**——Polymarket 应用层心跳为服务端发 PING 文本、客户端回 PONG；客户端主动发 PING 被判非法（1008 policy violation，曾致盘口流 3 秒断连循环）。
- **市场格式（slug/outcome）** — Polymarket 市场格式解析单一事实源在 `types.py`（window_start_from_slug / symbol_from_slug / direction_from_outcome）：曾把 slug `rsplit` 三处、outcome→方向映射两处当字符串手工处理。外部数据重建持仓走 `rebuilt_position` 工厂（entered_remaining_sec = 窗口剩余，负数截 0）：挂单成交 / API 成交 / 钱包接管三条路径共用。
- **盘口定价（weighted_price / best_price）** — `book_price.py` 纯函数，单一事实源：按可成交量加权均价，**流动性不足返回 None**（宁缺毋滥，不显示误导价）。执行器与面板落盘必须共用，禁止本地复刻。
- **盘口展示（book.json）** — 面板盘口单一来源：BookSampler 每 1s 落盘 book.json，monitor 只读它；tick 不再写 status.market_prices（曾是双写死工作，monitor 用 book.json 覆盖 status）。
- **市场发现（MarketDiscovery）** — 定位当前窗口的 Up/Down 市场（gamma-api，slug 模式）。
- **数据源（BinanceDataSource / KlineStore）** — Binance 镜像 K 线拉取 + 本地 CSV 滚动存储，增量/去重/裁剪。单批拉取统一走 `fetch_klines_batch`（在线与离线回测共用，禁止复刻）。

### 展示与验证

- **监控面板（monitor）** — 只读 TUI，独立进程：从 `StateStore.load()` 读状态、trades.csv、book.json 构建视图。任何异常只显示不崩溃。展示逻辑（`build_view`/`render`/`PanelView`/`PanelConfig`）提取至 `panel_view.py` 深模块（纯函数 + 类型化视图，TUI 与 Web 控制台共用）；实时价轮询提取至 `spot_price.py`（`SpotPrice` 后台线程）。`monitor.py` 只负责 CLI 入口与 TUI/Web 渲染循环，无兼容 re-export（测试直接锚定 panel_view，防「改 panel_view 但旧路径仍绿」假安全感）。`build_view` 的展示配置（阈值/窗口长度/摘要/止盈止损/运行时长）经 `PanelConfig` 打包注入。
- **展示视图（PanelView）** — `build_view` 输出的类型化视图：TUI render 属性访问（静态检查）；Web 控制台经 `asdict` 边界转换，字段名即 JSON 键名唯一出处。展示侧禁止魔法字符串键（ADR-0001 精神延伸）。
- **运行路径（RuntimePaths）** — 数据目录派生单一事实源（status/trades/log_dir/pid_file/mode）：模拟 data/、实盘 data_live/；`paths_for(live, data_dir)` 工厂。
- **协调状态（ProcessControl）** — monitor ↔ Web 控制台共享的进程协调（proc/show_tui/live/paths），替代裸 holder dict；模拟/实盘切换一次赋值（pc.paths 换新即全部跟随）。进程级操作也收敛于此：`spawn(config)`（按当前模式/数据目录拉起主循环）与 `loop_alive()`（读当前 pid 文件判存活），spawn_loop 模块函数在 paths.py，web_ui 只做 HTTP 路由。
- **父进程看门狗（ParentWatchdog）** — start_bot 的终端存活探测关注点（深模块，`watchdog.py`）：uv run shim 层脱离控制台信号，关闭终端窗口时信号传不到本进程——后台守护线程定时探测父进程（终端）存活，父进程退出即整树清理子进程并自退出。存活检查（`is_alive`）与清理回调（`on_parent_exit`）经窄接口注入，可独立测试（不依赖真实进程树）。
- **策略注册（strategies/）** — 策略 = `Strategy` 子类 + `@register("名字")` + `strategies/__init__.py` 导入一行；`create_strategy` 按名实例化。config.yaml 顶层 `strategy:` 切换，专属参数放同名分节（config.py 无需改动即支持新策略）。事件驱动策略可覆写 `refresh_signal`（窗口内每 tick 刷新信号）与 `status_text`（面板状态文案）。
- **动量策略（momentum）** — 窗口开盘建基准价，实时监控 Binance 价（WS ~1s），穿越 ±threshold_pct 同向入场，持有到结算（执行层 low-band 拦截：max_entry_price 防穿越后极化高价追单）。研究依据 docs/reports/momentum_strategy.md：穿越后方向延续 72-97%（大样本 robust），低价档（0-0.65）实证正 EV。

## 架构约定

- **决策/视图类型化**：引擎与主循环之间传递 `StateView/MarketView` 类型，不传裸 dict（见 ADR-0001）。
- **配置窄视图**：`Config` 按消费角色拆窄视图——`EngineConfig`（engine.decide 消费；由字段白名单 `_ENGINE_FIELDS` 自动映射，加参数不再手抄构造映射）、`StrategyConfig`（Strategy 构造消费 market_interval/threshold_pct）、`PanelConfig`（面板展示）。策略名单单一事实源在 strategy registry（`config._known_strategies` 延迟导入推导）；run.py 依赖注入按策略类声明（`needs_fetch_price`），不再按策略名硬编码 if。
- **窄接口注入**：跨模块依赖用 Protocol 收缩到最小能力面（`LifecycleDeps`/`SamplerProto`/`CancelExecutor`），禁止持有对方整体引用或穿透私有成员。
- **序列化只在边界**：领域内不裸 dict 传递状态；JSON 键名只存在于 StateStore 与边界转换（含 PanelView.asdict）。
- **落盘原子写**：所有 CSV/JSON 状态文件经 `fileio.atomic_write_text`（tmp+replace）写入，禁止直接 `write_text` 覆盖（防半写/崩溃损坏，见 ADR-0003）。
- **dry-run 语义**：模拟模式不触碰真实认证/订单；盘口查询走公开端点（无凭证也能跑）。
- **避免的词汇**：不要用"服务/组件/API/边界"称呼上述模块；领域词见本词汇表（如"生命周期"而非"生命周期服务"）。
