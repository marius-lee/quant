# HANDOFF — 盈迹 (quant) 项目变更日志

> **修改前**: `rg "关键词" HANDOFF.md HYPOTHESES.md docs/adr/` 三文件联动搜索，
> 避免重复踩坑、重新讨论已否决方案、遗漏已有设计。

---

## test-v212: Alpha 候选池 UI 优化

**变更**: `web/static/app.js`, `web/static/style.css`

- renderSignals: 候选池从 8 行缩至 5 行，得分从 3 位小数缩至 2 位
- reason 列截断：超过 2 个因子贡献时只显示前 2 个 + "N more"，hover 显示完整
- CSS: `.status-badge` → `.badge` 基类重命名，对齐 JS 中的 `class="badge badge-red"`
- 新增 `.trunc-reason em` 样式

---

## test-v211: pipeline 封死涨停预过滤

**变更**: `quant/pipeline.py`, `quant/constraints.py`, `config/config.yaml`

- constraints.py: 新增 `filter_sealed_limit_up()` — 从 `limit_up_pool` 查询昨日封死涨停股
- pipeline.py Step 2.3: 在 apply_all_filters 后调用预过滤
- config.yaml: `universe.sealed_limit_up_ratio: 3.0`

---

## test-v210: 信号执行反馈闭环 — cancel_reason + exec_notes

**变更**: `quant/data/trade_repo.py`, `quant/order_manager.py`, `web/*`

- trade_repo.py: `pending_orders.cancel_reason` + `daily_signals.exec_notes` 架构迁移
- order_manager.py: `_cancel()` 写入 cancel_reason；封死放弃写入 exec_notes
- app.js: Alpha 候选池表格显示 exec_note 列（封死→红色 badge）

---

## test-v209: 配置键路径修复

**变更**: `quant/optimizer/portfolio.py`

- `sleeve.positions_per_factor` → `alpha.sleeve.positions_per_factor`
- 修复 PortfolioConstructor 层级读取 config.yaml（嵌套在 alpha.sleeve 下）

---

## test-v208: include_ask_bid — 五档盘口 + 涨停封死检测

**变更**: `quant/monitor/quote.py`, `quant/order_manager.py`, `quant/monitor/monitor.py`, `quant/scheduler/orchestrator.py`

- quote.py: `_parse_tencent_line` 提取买一/卖一价量（字段 9/10/19/20）
- order_manager.py: 使用 ask（卖一价）替代 price 做成交判断；封死涨停即时放弃
- monitor.py: `fetch_quotes(all_syms, include_ask_bid=True)`
- orchestrator.py: monitor 启动时间 09:35→09:30

---

## test-v207: orchestrator 韧性 + PortfolioConstructor 修复

**变更**: `quant/scheduler/orchestrator.py`, `quant/optimizer/portfolio.py`, `quant/monitor/monitor.py`

- portfolio.py: 新增 `alpha.sleeve.positions_per_factor` 读取
- orchestrator.py: `_run_task` try/except 包裹防线程死亡；monitor 线程僵死检测 + 自动重启
- monitor.py: `fetch_quotes` try/except 网络韧性

---

## 2026-07-21: 数据拉取全链路修复 (test-v165~v206 已压缩)

关键修复:
- tushare token 写入 config.yaml
- 日期格式统一策略（YYYYMMDD vs YYYY-MM-DD 按数据源）
- tushare 50次/分钟 + tickflow 10次/分钟 限速适配
- turnover 回填走 baostock（免费无硬限速，~2只/秒）
- 交易日历自动判断非交易日
- tickflow 免费版 vs API Key 版分流
- INSERT OR REPLACE 覆写旧 turnover → 只在 turnover>0 时才写入
- 僵尸 task_runs 清理 + monitor 去重
