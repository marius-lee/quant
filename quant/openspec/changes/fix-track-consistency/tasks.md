## Implementation Tasks

### 1. A1-A5 开盘卖出段接入 tracks

- [x] 在 A1-A5 for 循环外包裹 `for tname, track in tracks.items():`
- [x] 替换 `positions` → `track["positions"]`, `capital` → `track["capital"]`
- [x] record_trade 加 `strategy=track["strategy"]`
- [x] trades_list 仅 chen track 记录
- [x] 语法检查 + 重启验证

### 2. Kelly 先验统一

- [x] `ops/position_sizers.py`: `PRIOR_*` 常量移除 `_` 前缀（公开导出）
- [x] `ops/performance.py` `kelly_fraction()`: import 共享先验，`n_trades < 3` 时返回先验估计值
- [x] 语法检查

### 3. 验证

- [x] 重启服务无报错
- [x] `/arena` 页面 5 列数据均为 ¥5,000（非 ¥4,985）
