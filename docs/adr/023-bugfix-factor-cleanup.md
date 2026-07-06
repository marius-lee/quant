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

## Part C: 废弃引用全面清除 (2026-07-06)

### 原则: 活跃代码不引用已删除的因子/脚本

### 清理清单
| 文件 | 清理内容 |
|------|---------|
| `factor/compute.py` | 注释块: `momentum_10d/volatility_20d/skewness_20d/amihud_20d/hsgt_flow_5d/idio_vol_20d` → 当前因子名 |
| `CLAUDE.md` | factor registry 描述 + `optimize_factors.py` 引用 → 当前状态 |
| `HANDOFF.md` | `volatility_20d/amihud_20d` → 当前因子名 |
| `docs/FACTOR-ANALYSIS-2026-07-03.json` | `amihud_20d` → `amihud_250d` |
| `scripts/optimize_factors.py` | **删除** (引用已删除的 momentum_10d) |
| `scripts/register_window_fixes.py` | **删除** (一次性迁移, 已执行) |
| `scripts/register_momentum_variants.py` | 删除 momentum_10d deprecation 引用 |
| `scripts/__pycache__/` | 删除 (stale .pyc) |

### 保留不动的历史文档
- `docs/FACTOR-AUDIT-2026-07-03.md` — 评估历史记录
- `docs/ANALYSIS-2026-07-03.md` — 分析历史记录
- `docs/HANDOFF-2026-07-03.md` — 旧 handoff
- `CHANGELOG.md` — 变更日志
- `docs/adr/020-022-*.md` — 设计文档引用旧名属正常历史上下文

## Part D: 关键路径埋点补齐 (2026-07-06)

### 原则: 静默失效必须有日志痕迹

### 补齐的 5 处埋点
| 位置 | 级别 | 内容 | 场景 |
|------|------|------|------|
| `factor/compute.py:_cs_zscore` | DEBUG | 有效值 < min_count → 返回 NaN | 截面标准化静默失效 |
| `factor/compute.py:compute_amihud` | DEBUG | min_valid 过滤全部股票 | 窗口不足时因子静默空值 |
| `factor/compute.py:compute_all_factors` | INFO | N price + N fund = total (N all-NaN) | 每期调仓因子健康度 |
| `pipeline.py:step4` | INFO | N valid / N total factors + post_state | 因子→alpha 可观测 |
| `backtest.py:rebalance` | INFO | +turnover 到调仓日志 | 换手率可观测 |

### 原有埋点（参考，本次未动）
- pipeline.py: [1/7]~[3/7], [5/7]~[7/7] 均有日志
- backtest.py: 起止/止损/汇总/基准对比 均有日志
- stats_cache.py: 评估全流程 (数据加载/IC计算/相关性/快照) 均有日志
- compute.py: 单因子失败已有 WARNING
