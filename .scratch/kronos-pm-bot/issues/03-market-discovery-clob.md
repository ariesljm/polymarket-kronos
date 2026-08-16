# 03 — Polymarket 市场发现 + CLOB 执行

**What to build:** gamma-api 查询当前 BTC 15m up/down 市场（过滤 active/acceptingOrders/双结果/有 clobTokenIds；解析 outcomePrices/outcomes/clobTokenIds 等 JSON 编码字符串字段；取 yes/no token_id 与 entry 时间线——参考 LuciferForge/polymarket-btc-autotrader 的 scanner.py）；用 py-clob-client-v2 实现认证（钱包私钥自 `.env`，L1 派生 API key → L2 creds）、挂限价单/撤单/卖出（注意 CLOB orderbook 排序陷阱：bids 升序/asks 降序，best ask = min）；提供 dry-run 模式（只打印下单指令不真下）。

**Blocked by:** 01 — 项目骨架 + 配置 + Strategy 接口 + 决策引擎

**Status:** resolved

- [ ] 只读查询能列出当前 BTC 15m up/down 市场（token_id、entry 时间线、可下单状态）
- [ ] 私钥从 `.env` 读取，不进代码仓库
- [ ] 挂单/撤单/卖出接口实现，dry-run 模式打印指令不真下
- [ ] 认证流程可用（主网 137；Amoy 测试网 80002 作为可选验证通道）
- [ ] 市场查询失败时优雅降级（返回空/异常，不崩溃）

## Comments

- 2026-08-14 实现完成：82 测试全绿（403d75c, 后续 fix 提交）
- 市场发现：slug 模式 btc-updown-15m-<窗口起始Unix秒>（实查确认，15m 对齐 7×24）；
  结算规则 Chainlink BTC/USD TWAP（>= 窗口开始价 → Up）
- CLOB：py-clob-client-v2；L1 私钥派生 → L2（creds 缓存 data/clob_creds.json 免 400 噪音）；
  真实链路验证通过（L2 认证 + open orders + 盘口查询）
- 参考项目 aulekator/Polymarket-BTC-15-Minute-Trading-Bot：其市场发现是 TODO 占位、
  用 py-clob-client v1（已归档），仅作架构参考，未直接采用
