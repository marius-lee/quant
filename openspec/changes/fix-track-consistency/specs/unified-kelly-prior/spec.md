# unified-kelly-prior

Kelly 先验参数统一来源，chen 实盘与 sizer 使用同一套贝叶斯先验。

## Requirements

- `ops/position_sizers.py` 导出 `PRIOR_WINS, PRIOR_LOSSES, PRIOR_AVG_WIN, PRIOR_AVG_LOSS`
- `ops/performance.py` 的 `kelly_fraction()` import 共享先验
- 数据不足 (<3 trades) 时返回先验驱动的保守估计值而非 0
