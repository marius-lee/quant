# ADR 034: 执行层边界 (P2-8) 与 DataStore 收口范围 (P2-10)

**日期**: 2026-07-26 | **状态**: 已定界 | **条目**: test-v306 P2-8 / P2-10

---

## P2-8 执行层边界: 真实券商接入搁置

**决策 (用户拍板)**: 不做真实券商接入 — 未开 Level-2, 接不了, 搁置。
不抽象 Brokerage 接口 (YAGNI), 维持现状:

- `execution/engine.py` ExecutionEngine — 模拟执行, 订单写 trades.db
  (sim_trades 为资金唯一真相源)。
- `backtest/broker.py` SimulatedBroker — 回测事件层, 包 DataStore +
  ExecutionEngine。
- `scheduler/execute.py` — 每日 09:30 限价挂单 (ADR 033), 同为模拟闭环。

**重启触发条件**: 开通 Level-2 + 确定券商通道 (如 miniQMT) 后, 再按
LEAN 模式抽象 Brokerage 接口, 先纸面后实盘。

---

## P2-10 DataStore 收口范围

**现状 (代码实测, 2026-07-26)**: `data/store.py` 2056 行 / 54 个 def,
god-object; `repos/` 已拆 4 个 (universe 168 / trade 677 / factor 323 /
evaluation 79 行), 拆分过半。

**目标**: store.py 只留连接管理 + schema, 方法按表归属迁入 repos,
DataStore 退化为 façade (过渡期委托) 直至调用方改注入 repo 后删除。

**迁移分组 (按 store.py 现有方法块)**:

| 组 | 方法 | 规模 | 目标 repo | ④ factor_cache 依赖 |
|---|---|---|---|---|
| G1 基准/名称 | get_benchmark, get_stock_names | ~45 行 | universe_repo | 无 |
| G2 universe 同步 | sync_stock_list, sync_delisted_stocks, get_universe, sync_industry, _sync_industry_akshare, get_stock_count | ~400 行 | universe_repo | get_universe (经 pipeline) |
| G3 turnover | backfill_turnover, backfill_turnover_quotes, rank_by_turnover | ~300 行 | daily_repo | rank_by_turnover (信号侧, 非物化) |
| G4 基本面 | get_financials, get_fundamentals, sync_fundamentals | ~170 行 | universe_repo | get_fundamentals (PIT, test_v307 护航) |
| G5 daily 大组 | update_daily, get_daily, _get_daily_chunk, _analyze_daily_gaps, 7 源 fetcher, sync_adj_factor, _rebase_ex_dividend, _local_qfq_ratio, sync_lhb_data, _norm_row, _log_source_sample, _ensure_adj_factor_tables | ~1100 行 | daily_repo (新建) | get_daily + 晚间链 update_daily |

**迁移顺序与验收** (G1→G5, 风险递增; 每步独立提交):

1. 每步: 方法体物理搬迁, DataStore 同名方法**保留委托** (调用方零改动),
   全套 pytest 绿 + HANDOFF 条目。
2. 委托层注意: universe_repo._query 每次开关连接, 会丢 DataStore 的
   线程局部连接复用 + _query_cache (LRU) — 委托前先把连接复用/cache
   语义下沉到 _base.DatabaseManager, 否则 web/报表路径性能回归。
3. G5 单独窗口做 (晚间链 update_daily 主路径), 做完必须跑
   `run_task.sh daily_data` 冒烟 + 次日晚间链实测。
4. 全部迁完后: 调用方批量改注入 repo, 删 façade — 最后一步才删,
   不与搬迁同提交。

**今晚 (v306) 不做代码搬迁**: 凌晨 ④ factor_cache / ① jq_valuation
会加载 store.py 链路, 搬迁收益不抵验收风险。本 ADR 即 P2-10 的
"界定范围" 交付物, 搬迁从 ④⑤ 验收通过后开始。
