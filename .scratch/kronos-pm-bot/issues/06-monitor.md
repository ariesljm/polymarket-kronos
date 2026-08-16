# 06 — 终端监控面板（TUI）

**What to build:** `uv run python -m pmbot.monitor` 独立只读监控面板，与主循环并行运行，每 2 秒刷新：
模式/当前窗口与倒计时/信号/挂单/持仓/熔断计数/今日盈亏/最近交易/方向准确率。

数据全部来自 status.json + trades.csv + PredictionLog，不改主循环任何代码。

**Blocked by:** 05 — 统计 + 验证报告（复用 accuracy 逻辑）

**Status:** resolved

- [ ] build_view 从 status.json 提取字段（窗口/剩余秒/信号/挂单/持仓/熔断），缺失字段安全占位
- [ ] 今日盈亏与笔数从 trades.csv 计算；最近交易截断展示
- [ ] 方向准确率来自 PredictionLog
- [ ] 渲染输出含全部区块；Ctrl-C 干净退出
- 2026-08-14 实现完成：134 测试全绿，code-review 双轴通过
