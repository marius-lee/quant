# HANDOFF — 2026-07-06 01:30 (Complete Audit)

## 项目概况
A股量化因子评估管道。Python 3.14, SQLite (WAL), 数据 2020-2026。
35 因子 (26 price + 9 fundamental), 1 active (zt_streak).

## 本轮修复 (ADR 023)

### 7 个 Bug 修复
1. **compute_amihud min_valid**: `250*0.5=125` > eval 可用 ~117天 → 全NaN。改为 `min(window, actual)*0.5`
2. **compute_ma_alignment**: 缺 window 参数，dispatch `fn(data, date, win)` 报错。已添加 `window: int=20`
3. **hsgt_flow_5d**: `get_northbound_flow` 不存在。从 registry+map 移除
4. **main_flow_ratio**: registry 有但 fn map 无 → 死因子。从 registry 删除
5. **5 个重复因子名**: volatility_20d/amihud_20d/skewness_20d/idio_vol_20d/momentum_10d — 与变体 (`*_126d/*_60d/*_250d/*_63d`) 使用相同 config 常量，输出完全一致。从 map+registry+stats_cache 标签删除
6. **fallback 默认值**: `_VOLATILITY_WINDOW/_IDIO_VOL_WINDOW` 从 20→126，对齐 config.yaml
7. **stats_cache.py**: 删除旧 `*_20d` 和 `momentum_10d` 标签映射

### 全面审计结论
- **函数签名**: 35/35 全部通过 dispatch 兼容性检查
- **运行时 import**: 全部通过
- **Config 一致性**: 4/4 关键窗口参数与 config.yaml 一致
- **DB 数据覆盖**: 6 个 DB 直查因子 (lhb/margin/fund/analyst) 均有数据，IC=0 是信号稀释问题非 bug
- **因子数**: 42→35，registry ↔ fn maps 完全一致

## 因子现状
### Active (1)
- zt_streak: IC=+0.0424, t=7.1, Sharpe=1.00 (2023-2026)

### Pass Layer 1 t-test but not Layer 3 (4)
- dt_streak: IC=+0.0366, t=7.1
- vol_price_corr_10d: IC=-0.0263, t=3.3
- roa: IC=+0.0238, t=2.6
- roe_reported: IC=+0.0220, t=2.4

### Data coverage issues (not bugs, just limitations)
- fund_hold: 最新 2025-12-31 (eval 是 2026，可能过时)
- analyst_forecast: 仅 1 天数据 (2026-07-03)
- lhb_detail: 覆盖 2025-2026，但上榜股票<200/天 (稀释)

## 下一步
```bash
bash scripts/eval_stepwise.sh
```
预期: 无 import/argument 错误, amihud_250d 产出非0 IC (数据窗口自适应后)

## 关键文件
- [factor/compute.py](/Users/mariusto/project/quant/factor/compute.py) — _PRICE_FN_MAP (26 entries), 所有 compute 函数
- [factor/stats_cache.py](/Users/mariusto/project/quant/factor/stats_cache.py) — 评估管道, display/category/source 标签
- [config/config.yaml](/Users/mariusto/project/quant/config/config.yaml) — 单一真相源
- [docs/adr/023-bugfix-factor-cleanup.md](/Users/mariusto/project/quant/docs/adr/023-bugfix-factor-cleanup.md) — 本轮修复文档
- [data/market.db](/Users/mariusto/project/quant/data/market.db) — SQLite, factor_registry (35 rows)
