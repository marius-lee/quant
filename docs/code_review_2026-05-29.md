# 代码审查报告 — 2026-05-29

审查人: 幻方量化系统架构视角
范围: /Users/mariusto/project/quant/ 全部代码

## 致命问题 (7)

1. **auto_run.py:52-60** — 因子缓存的 high/low/volume/amount 用 close 伪造
2. **pipeline.py:154** — 训练目标 `returns.shift(-5)` 是单日收益非5日累计
3. **pipeline.py:45-50** — 行业中性化截距+全部哑变量完全共线
4. **ensemble.py:37-39** — 模型常数值预测导致NaN IC污染
5. **event_engine.py:73** — 成交量约束 0.05% 非 5%
6. **data/fundamental.py:82** — BJ股票前缀错用sz
7. **auto_run.py:73** — factors_cache 无主键

## 重要问题 (8)

- auto_run.py:85 — VACUUM 与web server并发冲突
- pipeline.py:147-148 — 全量factors加载OOM风险
- pipeline.py:199 — 预测列缺失无告警
- pipeline.py:233-241 — 回测用训练集非推荐集
- pipeline.py:8 — FundamentalCrossSection导入未使用
- pipeline.py:58-106 — _real_fundamental_factors定义未使用
- web/db.py:28-46 — 无事务保护
- web/app.py:82 — /api/track 无异常处理

## 需实现功能

1. K线图集成
2. 实时行情刷新
3. 推荐变化可视化
4. 通知推送
5. 因子版本管理
