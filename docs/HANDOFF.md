# HANDOFF — 2026-07-06 02:00 (Code Audit Complete)

## 项目概况
A股量化因子评估管道。Python 3.14, SQLite (WAL), 2020-2026 数据。
35 因子 (26 price + 9 fundamental), 1 active (zt_streak).

## 本轮完成

### Part A: 7 个因子管道 Bug
1. `compute_amihud`: min_valid 改为自适应实际数据长度
2. `compute_ma_alignment`: 添加 window 参数修复 dispatch 崩溃
3. `hsgt_flow_5d` / `main_flow_ratio`: 删除死因子
4. 5 个 `*_20d` 重复: 从 map+registry+labels 删除
5. factor_registry: 42→35 因子，与 fn maps 完全一致
6. stats_cache.py 标签清理

### Part B: 全代码反模式清除 (9 处)
**原则**: config.yaml 单一真相源，静默容错必须显式声明理由。

**`_cfg` 默认值 → `_require_cfg` fail-fast (8 处)**:
factor windows (已完成), zscore_min_count, n_symbols, lookback, ic_min_periods, n_days, covariance window/min_periods

**静默 `except Exception` 修复 (4 处)**:
pipeline.py (benchmark), backtest.py (stop-loss), execution/quote.py (交易时段), data/store.py (tushare)

## 因子现状
- Active: zt_streak (IC=+0.0424, t=7.1, Sharpe=1.00)
- Pass Layer 1: dt_streak, vol_price_corr_10d, roa, roe_reported, gap_5d
- 其余 29 个 deprecated

## 下一步
```bash
bash scripts/eval_stepwise.sh
```
预期: 无 import/argument 错误, amihud_250d 非零 IC, ma_alignment_20d 正常计算

## 关键文件
- [factor/compute.py](/Users/mariusto/project/quant/factor/compute.py) — `_require_cfg`, `_PRICE_FN_MAP` (26 items)
- [factor/stats_cache.py](/Users/mariusto/project/quant/factor/stats_cache.py) — eval pipeline, display labels
- [config/config.yaml](/Users/mariusto/project/quant/config/config.yaml) — single source of truth
- [docs/adr/023-bugfix-factor-cleanup.md](/Users/mariusto/project/quant/docs/adr/023-bugfix-factor-cleanup.md) — full change log
- [data/market.db](/Users/mariusto/project/quant/data/market.db) — factor_registry (35 rows)
