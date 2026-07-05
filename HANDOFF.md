# Handoff: quant 项目状态 — 2026-07-05 18:30 CST

## 最近提交

0398920 P42: eval_stepwise.sh — use _ecfg() instead of bash ${VAR}
4e806bf P42: fix backtest.py — rebalance_dates ordering + line27 P38 artifact + f-string quotes
1446a64 Revert "P42: fix rebalance_dates..."
639d45f P41: fix cfg/_cfg import mismatches in store.py + covariance.py
4da4843 P40: fix cfg -> _cfg in stats_cache.py
f7a96fd P39: config.yaml 生产标准参数
7279f4b P38: 全量参数迁入 config.yaml

## P38-P42 汇总

P38 将 15 个遗漏参数迁入 config.yaml，引入 cfg/_cfg import mismatch、语法错误、变量排序等 6 个回归 bug。P40-P42 全部修复，20/20 测试通过。

## 回测结果 (P42 后验证通过)

| 阶段 | Period | Wealth | Sharpe | Return |
|------|--------|--------|--------|--------|
| 步进 | 2026H1 | 9,357 | 1.64 | +87.1% |
| 生产 | 2023-2026 | 516,392 | 1.00 | +416.4% |

当前 active 因子: 1 个 (zt_streak, IC=+0.0424, IR=+0.65)

## ADR 归档

016 数据源注册表 (新增) — 12 个数据源分析 + 分层策略
015 生产配置标准 (P39)
014 Redis 缓存后端 (P24)
013 daily_basic 弃用 (P24)

## 数据源状态 (ADR 016)

主力: tencent (日线), baostock (基础), akshare (专项), tushare (估值 fallback)
已死: 网易(502), 同花顺(404), 雪球K线(400), 东方财富(IP被封)
trial过期: JQData (自动 fallback 到 tushare)

## 待完成

1. 补齐 2023-2024 历史日线数据 (daily_sync.py)
2. 新因子开发 (当前仅 1 个 active 因子，风险敞口集中)
3. benchmark.py 加 fallback (sina DNS 偶发失败)

## 关键路径

/Users/mariusto/project/quant
Python: .venv (3.14), .venv-tushare (3.12)
Redis: localhost:6379
测试: pytest tests/ -q (20/20)

## P43 — 多因子分仓架构 (2026-07-05 19:00)

### 变更
- config.yaml: alpha.combine_mode (sleeve/composite) + alpha.sleeve 节
- factor/synth.py: sleeve_compose() — 每因子独立选 top N 取并集
- pipeline.py: 按 combine_mode 分支, sleeve 跳过 soft cutoff
- tests/test_synth.py: 6 个 sleeve_compose 测试
- docs/adr/017-sleeve-architecture.md

### 结果
- 26/26 测试通过
- 默认 sleeve 模式保留因子独立信号
- composite 模式向后兼容
