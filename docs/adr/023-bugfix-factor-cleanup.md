# ADR 023 — Bug修复 + 反模式清除 (2026-07-06)

## Part A: 因子管道 Bug 修复

### 1. compute_amihud min_valid 数据窗口自适应
**问题**: `min_valid = max(1, int(250*0.5)) = 125` > eval 可用 ~117天 → 全NaN
**修复**: `effective = min(window, p_slice.shape[0]); min_valid = max(30, int(effective*0.5))`

### 2. compute_ma_alignment 参数签名
**问题**: `fn(data, date)` 缺 window 参数，dispatch `fn(data, date, win)` 报错
**修复**: 添加 `window: int = 20`

### 3-7. 死因子 + 重复因子清理
删除 hsgt_flow_5d, main_flow_ratio, 5个 `*_20d` 重复。42→35 因子。

## Part B: 全代码反模式清除

### 原则: config.yaml 单一真相源，静默容错必须显式声明理由

### 第一类: `_cfg(key, default)` 掩盖 config 缺失
所有 behavioral 参数 (factor windows, eval params, risk params) 改为 `_require_cfg(key)` — 缺失即 fail-fast。
涉及: factor/compute.py, factor/stats_cache.py, factor/marginal.py, risk/covariance.py

### 第二类: `except Exception: pass` 静默吞错
核心计算路径 4 处修复:
- pipeline.py:198 — benchmark 可选 (加注释)
- backtest.py:98 — stop-loss 可选 (加注释)
- execution/quote.py:114 — 交易时段检查 fallback (加 logger.warning)
- data/store.py:354 — tushare 获取失败 (加 logger.warning)

## 变更文件
- [factor/compute.py](/Users/mariusto/project/quant/factor/compute.py) — amihud, ma_alignment, _require_cfg
- [factor/stats_cache.py](/Users/mariusto/project/quant/factor/stats_cache.py) — _require_cfg, 标签清理
- [factor/marginal.py](/Users/mariusto/project/quant/factor/marginal.py) — _require_cfg
- [risk/covariance.py](/Users/mariusto/project/quant/risk/covariance.py) — _require_cfg
- [pipeline.py](/Users/mariusto/project/quant/pipeline.py) — 注释
- [backtest.py](/Users/mariusto/project/quant/backtest.py) — 注释
- [execution/quote.py](/Users/mariusto/project/quant/execution/quote.py) — logger.warning
- [data/store.py](/Users/mariusto/project/quant/data/store.py) — logger.warning
