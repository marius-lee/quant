# ADR-044: 全架构审计 — 业界标准对齐 (2026-08-03)

## 审计范围

Grinold & Kahn 七层架构逐层对标: Qlib / WorldQuant / VN.PY / DolphinDB。

## 审计结果

| 层 | 对齐度 | 状态 |
|----|--------|------|
| 数据 | 60% → 70% | P2a 数据质量门禁 |
| 因子 | 80% → 85% | P1b source_hash 变更检测 |
| Alpha | 80% → 85% | P3a 冗余因子降权 |
| 风控 | 75% | — |
| 优化 | 85% | — |
| 执行 | 50% → 55% | P2b 成本模型接入模拟成交 |
| 监控 | 80% | — |

## 已实施修复 (test-v363)

### P0 — 因子注册签名校验
- 文件: `quant/factor/compute/_registry.py`
- `_validate_factor_signatures()`: 模块加载时检测 fundamental factor 是否访问 `data["close"]` 无 MultiIndex guard
- 防再次出现 v362 类型的静默崩溃

### P1a — task_runs 协议装饰器
- 文件: `quant/scheduler/task_log.py`
- `@task(name, grace_seconds=N)` 装饰器: 消 start/try/finish 样板
- `snapshot.py` 已迁移为示例

### P1b — 物化 source_hash 变更检测
- 文件: `quant/factor/store.py`
- `_compute_factor_source_hash()`: sha256(因子函数源码)
- manifest.json 写入 source_hash, `_get_existing_factors` 不匹配则返回空集合 (强制重算)
- 解决: 修改因子函数后旧缓存值不变的数据一致性问题

### P2a — 数据质量门禁
- 文件: `quant/data/quality.py` (新增)
- `check_daily_quality(date)`: 股票数量波动/涨跌停比例/字段缺失/极端价格
- 集成到 `evening.py` 晚间链, daily_data → quality gate → adj_factor
- error 级别记录日志但不阻断

### P2b — 成本模型接入模拟成交
- 文件: `quant/execution/execution_model.py`
- `BacktestExecutionModel._apply_cost()`: 对买卖订单应用 Almgren-Chriss market impact
- 买入: price × (1 + impact_bps/10000), 卖出: price × (1 - impact_bps/10000)

### P3a — 因子协方差/冗余检测
- 文件: `quant/alpha/model.py`
- `_adjust_for_redundancy()`: 相关系数 > 0.7 的因子对, IC 绝对值较小方折半
- 集成到 `AlphaModel.combine()`, 对 ic_map 自动调整

### P3b — 物化断点续传
- 文件: `quant/factor/store.py`
- `_checkpoint.json`: 每 chunk 完成后写 `{last_date, chunk_done, n_chunks}`
- 重启后可续传 (24h 内有效)
- 物化完成自动清理

### P3c — 因子 golden 测试集
- 文件: `quant/factor/golden_test.py` (新增)
- `generate`: 5日期 × 8股票 生成基线数据集 → `test/golden_factors.json`
- `verify`: 对比当前值与 baseline, tolerance=1e-4
- 可 CI 集成: `PYTHONPATH=. .venv/bin/python3 quant/factor/golden_test.py verify`

## 未实施项 (需实盘接入)

- 真实券商 CTP/XTP 网关
- Level2/Tick 数据源
- 神经网络/Transformer 模型
- 日内多频率回测
- 压力测试/情景分析

## 版本

test-v363, 2026-08-03
