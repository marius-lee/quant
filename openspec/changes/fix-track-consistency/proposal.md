## Why

Review 发现两个一致性缺陷: (1) A1-A5 开盘卖出段只处理 chen track，4 个 sizer 的隔夜持仓无法被开盘清仓; (2) chen 实盘 `kelly_fraction()` 与 sizer 使用不同的贝叶斯先验，data_quality=early_stage 时 chen 返回 0 而 sizer 正常交易，导致不公平比较。

## What Changes

- A1-A5 开盘卖出段改为遍历所有 5 个 track，sizer 持仓可获得与 chen 相同的开盘保护
- `ops/performance.py` 的 `kelly_fraction()` 与 `ops/position_sizers.py` 的 `_effective_stats()` 统一使用同一套贝叶斯先验 (p=0.55, b=1.5, n_pseudo=10)
- 提取共享先验常量到 `ops/position_sizers.py`，两处 import 同源

## Capabilities

### New Capabilities
- `track-premarket-sell`: A1-A5 开盘条件卖出覆盖所有 track
- `unified-kelly-prior`: Kelly 先验参数统一来源

### Modified Capabilities
<!-- None - no existing spec requirements change -->

## Impact

- `intraday_runner.py`: A1-A5 段 (+15 行)
- `ops/position_sizers.py`: 导出共享先验常量 (+3 行)
- `ops/performance.py`: kelly_fraction() 改用共享先验 (+5 行)
