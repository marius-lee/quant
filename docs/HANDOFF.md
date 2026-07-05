# HANDOFF — 2026-07-06 02:30 (Audit + Cleanup Complete)

## 项目概况
A股量化因子评估管道。Python 3.14, SQLite (WAL), 2020-2026 数据。
35 因子 (26 price + 9 fundamental), 1 active (zt_streak).

## 本轮完成的工作

### Part A: 7 个因子管道 Bug 修复
1. `compute_amihud`: min_valid 改为自适应实际数据长度
2. `compute_ma_alignment`: 添加 window 参数修复 dispatch 崩溃
3. `hsgt_flow_5d` / `main_flow_ratio`: 删除死因子
4. 5 个 `*_20d` 重复: 从 map+registry+labels 删除
5. factor_registry 42→35, 与 fn maps 完全一致

### Part B: 全代码反模式清除 (9 处)
**原则**: config.yaml 为单一真相源。静默容错必须显式声明理由。

**`_cfg` 默认值 → `_require_cfg` fail-fast (8 处)**:
factor windows, zscore_min_count, n_symbols, lookback, ic_min_periods, n_days, covariance window/min_periods

**静默 `except Exception` 修复 (4 处)**:
pipeline.py (benchmark 可选注释), backtest.py (stop-loss 可选注释), execution/quote.py (加 logger.warning), data/store.py (加 logger.warning)

### Part C: 废弃引用全面清除
活跃代码不允许引用已删除的因子名 (`*_20d`, `momentum_10d`, `hsgt_flow_5d`, `main_flow_ratio`)。
删除 2 个一次性脚本 (`optimize_factors.py`, `register_window_fixes.py`)。
历史文档保留不动。

## 因子现状
- Active: zt_streak (IC=+0.0424, t=7.1)
- Pass Layer 1: dt_streak, vol_price_corr_10d, roa, roe_reported, gap_5d
- 其余 29 个 deprecated

## 下一步
```bash
bash scripts/eval_stepwise.sh
```
预期: 无 import/argument 错误, amihud_250d 非零 IC.

## 关键架构规则
1. **config.yaml 是单一真相源** — 行为参数缺失必须 fail-fast, 不静默 fallback
2. **静默 `except Exception: pass` 必须带注释说明理由**
3. **活跃代码禁止引用已删除的因子/脚本**
4. **factor_registry 与 `_PRICE_FN_MAP` + `_FUNDAMENTAL_FN_MAP` 必须一致**

## 关键文件
- [factor/compute.py](/Users/mariusto/project/quant/factor/compute.py) — `_require_cfg`, `_PRICE_FN_MAP` (26 items)
- [factor/stats_cache.py](/Users/mariusto/project/quant/factor/stats_cache.py) — eval pipeline
- [config/config.yaml](/Users/mariusto/project/quant/config/config.yaml) — single source of truth
- [docs/adr/023-bugfix-factor-cleanup.md](/Users/mariusto/project/quant/docs/adr/023-bugfix-factor-cleanup.md) — full change log
- [data/market.db](/Users/mariusto/project/quant/data/market.db) — factor_registry (35 rows)
