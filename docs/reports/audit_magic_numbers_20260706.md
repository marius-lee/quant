# 硬编码数值参数审计 — 2026-07-06

全量代码审查（67个 .py 文件逐行通读），发现以下随手写入的数值参数。

## 高优先级 — 同值多处重复，易改漏

| # | 文件 | 行 | 硬编码值 | 问题 |
|---|------|----|----------|------|
| 1 | risk/constraints.py | 20-22 | 0.10, 20, 5_000_000, 0.40, 2.0 | RiskLimits dataclass 默认值，但同名函数 filter_by_liquidity(行46)、filter_by_price(行54)、position_limit_check(行88)、sector_exposure_check(行99) 各自又写了一遍同样的默认值 |
| 2 | 4个文件 | 多处 | 5000.0 | monitor/report.py:16、web/app.py:58、web/state_broker.py:93,208、scheduler.py:90 — 种子本金散落在4个文件5个位置 |
| 3 | 3个文件 | 多处 | LOT_SIZE=100 | optimizer/rebalance.py:7、pipeline.py(2处)、backtest.py — 各自定义相同常量 |
| 4 | web/app.py | 326,248 | 0.08, 60 | 止损阈值写死在 /api/quotes，60日滚动窗口写死在 /api/risk — 而 stop_loss_pct 在 config 中也有 |

## 中优先级 — 单点存在，有 fallback 但不够

| # | 文件 | 行 | 硬编码值 | 问题 |
|---|------|----|----------|------|
| 5 | execution/calendar.py | 118,125 | range(30) | 找交易日最多往后推30天，失效后才回退跳过周末 |
| 6 | factor/compute.py | 多处 | 30, 50, 10, 0.5 | Amihud min_valid、turnover fallback 50只、idio_vol len>=10、有效比例0.5 |
| 7 | factor/compute.py | 多处 | .clip(-2,2), (>0&<100), (>-1&<1), (>0&<2) | 极端值过滤边界散布在 high52w_dist、roe_ratio、roe_reported、debt_ratio、accruals 中 |
| 8 | factor/synth.py | 44 | clip=3.0 | z-score 截断阈值 |
| 9 | factor/synth.py | 84-85 | positions_per_factor=8, min_factors=1 | sleeve compose 硬编码默认值 |
| 10 | factor/stats_cache.py | 43,60,61 | 1.5, 30, 1.5 | lookback 放大系数、最少30天有效数据 |
| 11 | risk/neutralize.py | 34,66 | 3, 30, 30 | min_stocks_per_industry=3、industries fallback len(common)<30、size 回归 len(common)<30 |

## 低优先级 — 有合理理由但没注释

| # | 文件 | 行 | 硬编码值 | 问题 |
|---|------|----|----------|------|
| 12 | web/app.py | 167 | limit=10000 | get_trades() 内部硬编码限制 |
| 13 | data/cache.py | 21,22 | 200, 60 | tushare/akshare 速率限制 |
| 14 | data/store.py | 140 | -64000 | WAL cache_size |
| 15 | monitor/attribution.py | 91 | 0.02, 252 | 无风险利率2%、年化252天 |
| 16 | daily_sync.py | 85 | 500 | 批量写入 chunk 大小 |

## 建议处理方式

1. **#1** — 去掉函数签名的重复默认值，只从 RiskLimits 实例取值
2. **#2** — 5000 统一从 TradeRepo.get_initial_capital() 取
3. **#3** — LOT_SIZE 移到 config.yaml，所有模块统一 import
4. **#4** — 止损和风险窗口从 config 读
5. **#5-#11** — 加注释说明依据，部分可 config 化
6. **#12-#16** — 加注释说明原因即可

---

## 状态更新 — 2026-07-07

**全部 16 项已解决：**

| # | 处理方式 | 关联 |
|---|----------|------|
| 1-8,10 | RiskLimits/5000/LOT_SIZE/止损/窗口/因子校准/clip/stats — 全部挪到 config.yaml | P60 |
| 9 | synth.py sleeve_compose 去掉默认值 | P61 |
| 11 | neutralize.py + pipeline.py 统一 min_common_stocks | P61 |
| 12-14 | web/app.py, cache.py, store.py 加注释 | P61 |
| 15 | attribution.py rf/periods → config | P61 |
| 16 | daily_sync.py — 经查不存在此文件 (已合并到 store.py) | — |
