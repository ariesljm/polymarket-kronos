# ADR-0003: IO 原子写与拉取/类型窄化

- 状态：已接受
- 日期：2026-08-16

## 背景

架构审查发现三类可维护性问题：

1. **原子写模式复制**：`KlineStore.append/trim`、`PredictionLog._save`、
   `InstanceGuard._write` 四处各自实现"写 tmp 再 replace"，语义相同、实现漂移。
2. **Binance 拉取重复**：`data_source._http_fetch`（在线）与 `backtest.fetch_klines`
   （离线）各自实现同一镜像端点的请求/解析/分页，一处改动另一处遗忘即漂移。
3. **裸类型穿透**：`Strategy.generate_signal` 的 context 为裸 dict；主循环接缝方法
   （market/action/view）与 WS 连接对象无类型标注，静态检查失效。

## 决策

1. **fileio 单一事实源**：新增 `fileio.atomic_write_text`（tmp+replace）与
   `df_to_csv_text`，四处消费者全部改委托；新落盘一律走 fileio，禁止直接
   `write_text` 覆盖状态文件。
2. **拉取统一**：提取模块级 `data_source.fetch_klines_batch(symbol, timeframe,
   since, limit, proxies=None)` 为唯一批量拉取实现；在线数据源与离线回测共用。
   保留 `proxies` 参数以维持回测侧显式代理、在线侧环境变量两套既有行为。
3. **类型窄化**：
   - `SignalContext(TypedDict)` 替代裸 dict（strategy 接口）；
   - 主循环接缝方法补 `MarketInfo` / `Action` / `MarketView` / `ClobExecutor` /
     `UserStream` 标注；
   - 跨模块依赖收缩为窄接口 Protocol：`SamplerProto`（采样器）、
     `CancelExecutor`（生命周期撤单）；
   - WS 连接对象标注 `websockets.asyncio.client.ClientConnection`。
   - 展示配置打包：`monitor.build_view` 的 6 个配置注入参数收拢为
     `PanelConfig`（frozen dataclass），签名从 16 参数降到 7 个。

## 后果

- 状态文件落盘具备统一防半写保证；未来新增持久化只需调用 fileio。
- K 线拉取的请求/解析/代理语义只有一处实现，在线与离线评估数据口径一致。
- 类型错误在静态检查/构造期暴露；新接缝方法若无标注即为异常（对照
  ADR-0001 的"视图类型化"延续）。
