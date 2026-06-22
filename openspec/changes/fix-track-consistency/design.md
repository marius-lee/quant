## Context

`intraday_runner.py` 的 `tracks` 字典管理 5 个并行仓位。B1-B7 卖出段已遍历所有 track，但 A1-A5 开盘卖出段仍引用旧的 `positions`/`capital` 变量（仅 chen track）。`kelly_fraction()` 在 `ops/performance.py` 中无先验，数据不足时返回 0；sizer 的 `_effective_stats()` 在 `ops/position_sizers.py` 中有先验 (p=0.55, b=1.5)。

## Goals

1. A1-A5 段遍历 tracks 字典，sizer 持仓获得与 chen 相同的开盘保护
2. `kelly_fraction()` 使用与 sizer 相同的贝叶斯先验

## Design

### A1-A5: 最小改动

在现有 A1-A5 for 循环外包裹 `for tname, track in tracks.items():`，内部将 `positions`/`capital` 替换为 `track["positions"]`/`track["capital"]`。record_trade 加 `strategy=track["strategy"]`。

### Kelly 先验统一

将 `ops/position_sizers.py` 中的 `PRIOR_WINS, PRIOR_LOSSES, PRIOR_AVG_WIN, PRIOR_AVG_LOSS` 改为公开常量（移除下划线，或直接导出）。`ops/performance.py` 的 `kelly_fraction()` import 这些常量，在 `n_trades < 3` 时返回先验驱动的值而非 0。
